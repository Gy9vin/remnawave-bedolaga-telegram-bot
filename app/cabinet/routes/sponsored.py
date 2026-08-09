"""Оплата подписки за другого человека из кабинета.

Плательщик находит получателя по @нику/telegram id/email (``lookup``) и
оплачивает ему продление с баланса (``pay``). Вся бизнес-логика — поиск и
расчёт цены, списание — уже в ``app.services.recipient_lookup`` и
``app.services.sponsored_payment_service``; этот модуль только раскладывает
их по HTTP.

Два момента, из-за которых легко ошибиться:

- ``lookup`` отдаёт узкий ответ: отображаемое имя, id подписки и цены по
  периодам. Тариф, дата окончания, число устройств, telegram id и email
  получателя — чужие данные, плательщику их видно быть не должно (это не его
  аккаунт, ему нужна только кнопка «оплатить»).
- ``pay`` НЕ принимает id получателя и цену с клиента. Получателя резолвим
  заново по ``query`` (иначе можно подменить чужого получателя, зная только
  его внутренний id) и заново считаем цену через ``quote_for_recipient``
  (иначе можно оплатить по цене, которая успела устареть, например после
  того как получатель потерял скидку промогруппы).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.subscription import get_subscription_by_user_id
from app.database.models import Subscription, User
from app.services.recipient_lookup import RecipientIsPayerError, resolve_recipient
from app.services.sponsored_payment_service import (
    InsufficientBalanceError,
    SponsoredQuote,
    pay_from_balance,
    quote_for_recipient,
)
from app.utils.cache import RateLimitCache

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.sponsored import (
    SponsoredLookupRequest,
    SponsoredLookupResponse,
    SponsoredPayRequest,
    SponsoredPayResponse,
    SponsoredPeriodOption,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/sponsored', tags=['Cabinet Sponsored Payment'])


async def _get_subscription_or_404(db: AsyncSession, recipient: User, quote: SponsoredQuote) -> Subscription:
    """Перечитать подписку получателя перед списанием — ``quote`` её id уже
    проверил, но объект нужен свежий, для ``pay_from_balance``."""
    subscription = await get_subscription_by_user_id(db, recipient.id)
    if subscription is None or quote.subscription_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'code': 'period_unavailable'},
        )
    return subscription


@router.post('/lookup', response_model=SponsoredLookupResponse)
async def lookup_recipient(
    body: SponsoredLookupRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SponsoredLookupResponse:
    """Найти получателя по @нику/telegram id/email и показать цены его продления."""
    is_limited = await RateLimitCache.is_rate_limited(user.id, 'sponsored_lookup', limit=10, window=60)
    if is_limited:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail='Too many requests')

    try:
        recipient = await resolve_recipient(db, body.query, payer_id=user.id)
    except RecipientIsPayerError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'code': 'recipient_is_self'},
        ) from None

    if recipient is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={'code': 'recipient_not_found'},
        )

    quote = await quote_for_recipient(db, recipient)

    # Отдаём только то, что нужно для выбора периода — никаких чужих данных
    # получателя (см. докстринг модуля).
    return SponsoredLookupResponse(
        recipient_display_name=quote.recipient_display_name,
        subscription_id=quote.subscription_id,
        options=[
            SponsoredPeriodOption(
                period_days=period_days,
                price_kopeks=price_kopeks,
                price_lines=quote.price_lines_by_period.get(period_days, []),
            )
            for period_days, price_kopeks in quote.options
        ],
        payer_balance_kopeks=user.balance_kopeks,
    )


@router.post('/pay', response_model=SponsoredPayResponse)
async def pay_for_recipient(
    body: SponsoredPayRequest,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> SponsoredPayResponse:
    """Оплатить продление получателя с баланса плательщика.

    Получателя и цену резолвим заново по ``query``/``period_days`` — клиенту
    в этом не доверяем (см. докстринг модуля).
    """
    try:
        recipient = await resolve_recipient(db, body.query, payer_id=user.id)
    except RecipientIsPayerError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'code': 'recipient_is_self'},
        ) from None

    if recipient is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={'code': 'recipient_not_found'},
        )

    quote = await quote_for_recipient(db, recipient)
    price_kopeks = next(
        (price for period_days, price in quote.options if period_days == body.period_days),
        None,
    )
    if price_kopeks is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={'code': 'period_unavailable'},
        )

    subscription = await _get_subscription_or_404(db, recipient, quote)

    try:
        await pay_from_balance(
            db,
            payer=user,
            recipient=recipient,
            subscription=subscription,
            period_days=body.period_days,
            price_kopeks=price_kopeks,
        )
    except InsufficientBalanceError:
        missing_kopeks = price_kopeks - user.balance_kopeks
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={'code': 'insufficient_balance', 'missing_kopeks': missing_kopeks},
        ) from None

    return SponsoredPayResponse(
        status='applied',
        recipient_display_name=quote.recipient_display_name,
        period_days=body.period_days,
        amount_kopeks=price_kopeks,
    )
