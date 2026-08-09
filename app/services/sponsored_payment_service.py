"""Оплата подписки за другого человека.

Плательщик находит получателя (``app.services.recipient_lookup``) и оплачивает
ему продление — по цене ГЕТ получателя, а не своей: скидка промо-группы,
промо-оффер и допустройства у каждого свои, и подставлять сюда цену плательщика
значило бы дарить чужую скидку или наоборот считать по завышенной ставке.

Два пути денег:

- с баланса плательщика — сразу, ``pay_from_balance`` создаёт запись уже в
  статусе ``applied``;
- через внешнюю платёжку — запись создаётся заявкой в другом месте (вне этого
  файла) и применяется по вебхуку через ``apply_payment``, когда деньги
  реально пришли.

Цена фиксируется один раз — на шаге ``quote_for_recipient`` — и дальше просто
переносится в запись и в списание. Пересчитывать её при оплате или при
применении платежа нельзя: получатель мог успеть докупить устройства или
потерять скидку, а плательщик уже согласился на конкретную сумму (или она уже
списана внешней платёжкой).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.subscription import get_subscription_by_user_id
from app.database.models import (
    PaymentMethod,
    SponsoredPayment,
    SponsoredPaymentStatus,
    Subscription,
    User,
)
from app.services.pricing_engine import RenewalPricing, pricing_engine
from app.services.subscription_renewal_service import SubscriptionRenewalService


logger = structlog.get_logger(__name__)


class InsufficientBalanceError(Exception):
    """У плательщика не хватает баланса — платить нечем, запись не создаётся."""


@dataclass
class SponsoredQuote:
    """Предложение цены для оплаты подписки другого человека."""

    recipient_id: int
    # Только имя — без тарифа, срока и устройств: это подпись под кнопкой
    # оплаты, а не карточка подписки.
    recipient_display_name: str
    # None — у получателя ещё нет подписки, сначала нужно выбрать тариф
    # (отдельный сценарий, вне этого модуля).
    subscription_id: int | None
    options: list[tuple[int, int]]


async def quote_for_recipient(db: AsyncSession, recipient: User) -> SponsoredQuote:
    """Посчитать цены продления получателя по всем доступным периодам.

    Периоды и сама формула цены зеркалят
    ``app/cabinet/routes/subscription_modules/renewal.py``: набор периодов
    берётся из ``period_prices`` активного тарифа подписки, иначе — из
    глобальной настройки. Принципиальное отличие — цена всегда считается по
    подписке ПОЛУЧАТЕЛЯ (``user=recipient``), а не по плательщику: скидки и
    допустройства принадлежат тому, кому продлевают, а не тому, кто платит.
    """
    recipient_display_name = recipient.full_name

    subscription = await get_subscription_by_user_id(db, recipient.id)
    if subscription is None:
        return SponsoredQuote(
            recipient_id=recipient.id,
            recipient_display_name=recipient_display_name,
            subscription_id=None,
            options=[],
        )

    if (
        subscription.tariff_id
        and subscription.tariff
        and subscription.tariff.is_active
        and subscription.tariff.period_prices
    ):
        periods = sorted(int(key) for key in subscription.tariff.period_prices.keys())
    else:
        periods = settings.get_available_renewal_periods()

    options: list[tuple[int, int]] = []
    for period in periods:
        pricing = await pricing_engine.calculate_renewal_price(db, subscription, period, user=recipient)
        if pricing.final_total <= 0 and pricing.original_total <= 0:
            # Ничего не стоящий период (например, отключённая цена) — нечего
            # предлагать плательщику.
            continue
        options.append((period, pricing.final_total))

    return SponsoredQuote(
        recipient_id=recipient.id,
        recipient_display_name=recipient_display_name,
        subscription_id=subscription.id,
        options=options,
    )


def _fixed_pricing(price_kopeks: int, period_days: int, *, is_tariff_mode: bool) -> RenewalPricing:
    """Обернуть уже посчитанную и зафиксированную цену в объект для ``finalize``.

    ``finalize`` принимает результат ``calculate_renewal_price``, но у нас цена
    к этому моменту уже известна и пересчитывать её не нужно (см. докстринг
    модуля) — поэтому скидки и разбивка по компонентам здесь нулевые, а
    ``final_total`` равен зафиксированной сумме.
    """
    return RenewalPricing(
        base_price=price_kopeks,
        servers_price=0,
        traffic_price=0,
        devices_price=0,
        promo_group_discount=0,
        promo_offer_discount=0,
        final_total=price_kopeks,
        period_days=period_days,
        is_tariff_mode=is_tariff_mode,
    )


async def pay_from_balance(
    db: AsyncSession,
    *,
    payer: User,
    recipient: User,
    subscription: Subscription,
    period_days: int,
    price_kopeks: int,
) -> SponsoredPayment:
    """Оплатить продление получателя с баланса плательщика — сразу, без внешней платёжки.

    Проверка баланса стоит первой строкой: запись ``SponsoredPayment`` — это
    история совершённого платежа, а не заявка на него, поэтому при нехватке
    денег до создания записи и до списания дело не должно доходить.
    """
    if payer.balance_kopeks < price_kopeks:
        raise InsufficientBalanceError(
            f'У плательщика {payer.id} не хватает баланса: '
            f'есть {payer.balance_kopeks}, нужно {price_kopeks}'
        )

    recipient_display_name = recipient.full_name
    pricing = _fixed_pricing(price_kopeks, period_days, is_tariff_mode=bool(subscription.tariff_id))

    renewal_service = SubscriptionRenewalService()
    await renewal_service.finalize(
        db,
        recipient,
        subscription,
        pricing,
        payer=payer,
        description=f'Оплата подписки для {recipient_display_name}',
        payment_method=PaymentMethod.BALANCE,
    )

    payment = SponsoredPayment(
        payer_user_id=payer.id,
        recipient_user_id=recipient.id,
        subscription_id=subscription.id,
        tariff_id=subscription.tariff_id,
        period_days=period_days,
        amount_kopeks=price_kopeks,
        status=SponsoredPaymentStatus.APPLIED.value,
        payment_method=PaymentMethod.BALANCE.value,
        applied_at=datetime.now(UTC),
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)
    return payment


async def apply_payment(db: AsyncSession, payment_id: str) -> bool:
    """Применить платёж, пришедший через внешнюю платёжку (по вебхуку).

    Идемпотентно: повторный вызов на уже применённой записи ничего не делает
    и возвращает ``True`` — вебхуки платёжек любят дублироваться, а повторное
    продление на тот же платёж недопустимо.

    Сумма и период не пересчитываются — они зафиксированы при создании записи
    (см. докстринг модуля). ``charge_balance_amount=0`` — деньги уже получены
    внешней платёжкой, с баланса получателя списывать нечего.
    """
    result = await db.execute(select(SponsoredPayment).where(SponsoredPayment.payment_id == payment_id))
    payment = result.scalars().first()
    if payment is None:
        logger.warning('Платёж для применения не найден', payment_id=payment_id)
        return False

    if payment.status == SponsoredPaymentStatus.APPLIED.value:
        return True

    try:
        recipient = payment.recipient
        pricing = _fixed_pricing(
            payment.amount_kopeks,
            payment.period_days,
            is_tariff_mode=bool(payment.tariff_id),
        )

        renewal_service = SubscriptionRenewalService()
        await renewal_service.finalize(
            db,
            recipient,
            payment.subscription,
            pricing,
            charge_balance_amount=0,
            description=f'Оплата подписки для {recipient.full_name}',
            payment_method=PaymentMethod(payment.payment_method) if payment.payment_method else None,
            payer=payment.payer,
        )
    except Exception as error:
        logger.error(
            'Не удалось применить спонсорский платёж',
            payment_id=payment_id,
            error=str(error)[:200],
        )
        payment.status = SponsoredPaymentStatus.FAILED.value
        await db.commit()
        return False

    payment.status = SponsoredPaymentStatus.APPLIED.value
    payment.applied_at = datetime.now(UTC)
    await db.commit()
    return True
