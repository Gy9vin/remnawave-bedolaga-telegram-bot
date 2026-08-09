"""Эндпоинты кабинета «оплатить подписку за другого человека».

Резолвим получателя дважды — на lookup и заново на pay: клиенту нельзя
доверять id получателя (можно подменить на чужой) и заранее посчитанную
цену (могла устареть). Ответ lookup намеренно узкий: тариф, дата окончания,
число устройств, telegram id и email получателя — чужие данные, их в кабинет
плательщика отдавать нельзя, наружу идёт только отображаемое имя и цены.

Стиль — как в tests/cabinet/test_admin_devices_lazy_panel_identity.py и
test_branding_public_endpoints_db_outage.py: хендлеры дергаются напрямую,
db — AsyncMock, юзер — SimpleNamespace, БД не поднимается.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes import sponsored
from app.services.recipient_lookup import RecipientIsPayerError
from app.services.sponsored_payment_service import InsufficientBalanceError, SponsoredQuote


PAYER_ID = 1
RECIPIENT_ID = 2


def _payer(**overrides) -> SimpleNamespace:
    base = {'id': PAYER_ID, 'balance_kopeks': 100_000}
    base.update(overrides)
    return SimpleNamespace(**base)


def _recipient(**overrides) -> SimpleNamespace:
    base = {'id': RECIPIENT_ID, 'full_name': 'Иван И.'}
    base.update(overrides)
    return SimpleNamespace(**base)


def _quote(**overrides) -> SponsoredQuote:
    base = dict(
        recipient_id=RECIPIENT_ID,
        recipient_display_name='Иван И.',
        subscription_id=42,
        options=[(30, 10_000), (90, 25_000)],
    )
    base.update(overrides)
    return SponsoredQuote(**base)


# --- lookup -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_rate_limited():
    with patch.object(sponsored.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as exc_info:
            await sponsored.lookup_recipient(
                body=sponsored.SponsoredLookupRequest(query='@someone'),
                user=_payer(),
                db=AsyncMock(),
            )
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_lookup_recipient_not_found():
    with (
        patch.object(sponsored.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(sponsored, 'resolve_recipient', AsyncMock(return_value=None)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await sponsored.lookup_recipient(
                body=sponsored.SponsoredLookupRequest(query='@ghost'),
                user=_payer(),
                db=AsyncMock(),
            )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == {'code': 'recipient_not_found'}


@pytest.mark.asyncio
async def test_lookup_recipient_is_self():
    with (
        patch.object(sponsored.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(sponsored, 'resolve_recipient', AsyncMock(side_effect=RecipientIsPayerError)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await sponsored.lookup_recipient(
                body=sponsored.SponsoredLookupRequest(query='myself'),
                user=_payer(),
                db=AsyncMock(),
            )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {'code': 'recipient_is_self'}


@pytest.mark.asyncio
async def test_lookup_success_hides_private_recipient_data():
    """Ответ не должен содержать тариф, срок, устройства, telegram id, email —
    это данные получателя, а не плательщика."""
    recipient = _recipient(
        telegram_id=555,
        email='ivan@example.com',
        subscription_end_date='2099-01-01',
        device_limit=5,
        tariff_name='VIP',
    )
    quote = _quote()
    with (
        patch.object(sponsored.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(sponsored, 'resolve_recipient', AsyncMock(return_value=recipient)),
        patch.object(sponsored, 'quote_for_recipient', AsyncMock(return_value=quote)),
    ):
        response = await sponsored.lookup_recipient(
            body=sponsored.SponsoredLookupRequest(query='@ivan'),
            user=_payer(balance_kopeks=77_000),
            db=AsyncMock(),
        )

    payload = response.model_dump()
    assert payload == {
        'recipient_display_name': 'Иван И.',
        'subscription_id': 42,
        'options': [
            {'period_days': 30, 'price_kopeks': 10_000, 'price_lines': []},
            {'period_days': 90, 'price_kopeks': 25_000, 'price_lines': []},
        ],
        'payer_balance_kopeks': 77_000,
    }
    dumped = str(payload)
    for leaked in ('VIP', '555', 'ivan@example.com', '2099-01-01'):
        assert leaked not in dumped


# --- pay ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_pay_period_unavailable():
    recipient = _recipient()
    quote = _quote()
    with (
        patch.object(sponsored, 'resolve_recipient', AsyncMock(return_value=recipient)),
        patch.object(sponsored, 'quote_for_recipient', AsyncMock(return_value=quote)),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await sponsored.pay_for_recipient(
                body=sponsored.SponsoredPayRequest(query='@ivan', period_days=365),
                user=_payer(),
                db=AsyncMock(),
            )
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {'code': 'period_unavailable'}


@pytest.mark.asyncio
async def test_pay_insufficient_balance():
    recipient = _recipient()
    quote = _quote()
    payer = _payer(balance_kopeks=1_000)
    subscription = SimpleNamespace(id=42)
    with (
        patch.object(sponsored, 'resolve_recipient', AsyncMock(return_value=recipient)),
        patch.object(sponsored, 'quote_for_recipient', AsyncMock(return_value=quote)),
        patch.object(sponsored, '_get_subscription_or_404', AsyncMock(return_value=subscription)),
        patch.object(
            sponsored,
            'pay_from_balance',
            AsyncMock(side_effect=InsufficientBalanceError('not enough')),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await sponsored.pay_for_recipient(
                body=sponsored.SponsoredPayRequest(query='@ivan', period_days=30),
                user=payer,
                db=AsyncMock(),
            )
    assert exc_info.value.status_code == 402
    assert exc_info.value.detail == {'code': 'insufficient_balance', 'missing_kopeks': 9_000}


@pytest.mark.asyncio
async def test_pay_success_resolves_recipient_again_and_pays():
    """id и цена с клиента не принимаются на веру — получателя резолвим заново
    и платим по цене, посчитанной прямо сейчас, а не переданной телом запроса."""
    recipient = _recipient()
    quote = _quote()
    payer = _payer()
    subscription = SimpleNamespace(id=42)
    pay_mock = AsyncMock(return_value=SimpleNamespace(id=999))

    with (
        patch.object(sponsored, 'resolve_recipient', AsyncMock(return_value=recipient)) as resolve_mock,
        patch.object(sponsored, 'quote_for_recipient', AsyncMock(return_value=quote)),
        patch.object(sponsored, '_get_subscription_or_404', AsyncMock(return_value=subscription)),
        patch.object(sponsored, 'pay_from_balance', pay_mock),
    ):
        response = await sponsored.pay_for_recipient(
            body=sponsored.SponsoredPayRequest(query='@ivan', period_days=90),
            user=payer,
            db=AsyncMock(),
        )

    resolve_mock.assert_awaited_once()
    _, resolve_kwargs = resolve_mock.call_args
    assert resolve_kwargs.get('payer_id') == PAYER_ID or resolve_mock.call_args.args[1] == '@ivan'

    pay_mock.assert_awaited_once()
    _, pay_kwargs = pay_mock.call_args
    assert pay_kwargs['recipient'] is recipient
    assert pay_kwargs['payer'] is payer
    assert pay_kwargs['period_days'] == 90
    assert pay_kwargs['price_kopeks'] == 25_000

    assert response.status == 'applied'
    assert response.recipient_display_name == 'Иван И.'
    assert response.period_days == 90
    assert response.amount_kopeks == 25_000
