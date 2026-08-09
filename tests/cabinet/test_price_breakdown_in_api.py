"""Расшифровка цены (``price_lines``) в ответах кабинета.

ЗАЧЕМ: человек в кабинете видит итоговую сумму и не понимает, из чего она
сложилась — особенно про докупленные устройства: думает, что платит разово,
а платит каждый месяц. ``build_price_lines`` (app.services.price_breakdown)
уже собирает расшифровку и явно проговаривает в hint, что цена устройства —
ежемесячная; эти тесты проверяют, что расшифровка реально доезжает до
ответов API там, где называется цена: опции продления и lookup оплаты за
другого.

Стиль — как в tests/cabinet/test_sponsored_routes.py: хендлеры дергаются
напрямую, db — AsyncMock, БД не поднимается.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.cabinet.routes import sponsored
from app.cabinet.routes.subscription_modules import helpers as subscription_helpers, renewal
from app.services.price_breakdown import build_price_lines
from app.services.pricing_engine import RenewalPricing
from app.services.sponsored_payment_service import SponsoredQuote


PAYER_ID = 1
RECIPIENT_ID = 2


def _renewal_pricing_with_devices() -> RenewalPricing:
    """Цена продления с 2 доп. устройствами на периоде дольше месяца —
    именно тот случай, где hint обязан проговаривать «в месяц»."""
    return RenewalPricing(
        base_price=30_000,
        servers_price=0,
        traffic_price=0,
        devices_price=12_000,
        promo_group_discount=0,
        promo_offer_discount=0,
        final_total=42_000,
        period_days=60,
        is_tariff_mode=False,
        breakdown={'extra_devices': 2, 'months_in_period': 2},
    )


# --- renewal-options ----------------------------------------------------


@pytest.mark.asyncio
async def test_renewal_options_include_price_lines_with_device_monthly_hint():
    user = SimpleNamespace(id=PAYER_ID, restriction_subscription=False)
    # Периоды берём из тарифа (а не из settings.get_available_renewal_periods,
    # который на pydantic-модели Settings не патчится через patch.object) —
    # достаточно, чтобы дойти до цикла calculate_renewal_price по одному периоду.
    subscription = SimpleNamespace(
        tariff_id=1,
        tariff=SimpleNamespace(is_active=True, period_prices={'60': 30_000}),
        status='active',
        actual_status='active',
    )

    with (
        patch.object(subscription_helpers, 'resolve_subscription', AsyncMock(return_value=subscription)),
        patch.object(
            renewal.pricing_engine,
            'calculate_renewal_price',
            AsyncMock(return_value=_renewal_pricing_with_devices()),
        ),
    ):
        options = await renewal.get_renewal_options(user=user, db=AsyncMock(), subscription_id=None)

    assert len(options) == 1
    option = options[0]
    assert option.price_lines  # непустая расшифровка

    device_lines = [line for line in option.price_lines if 'устройств' in line.label.lower()]
    assert device_lines, 'нет строки про доп. устройства'
    assert any(line.hint and 'в месяц' in line.hint for line in device_lines), (
        'hint у доп. устройств обязан явно проговаривать ежемесячную оплату'
    )


# --- sponsored lookup -----------------------------------------------------


def _payer(**overrides) -> SimpleNamespace:
    base = {'id': PAYER_ID, 'balance_kopeks': 100_000}
    base.update(overrides)
    return SimpleNamespace(**base)


def _recipient(**overrides) -> SimpleNamespace:
    base = {'id': RECIPIENT_ID, 'full_name': 'Иван И.'}
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_sponsored_lookup_includes_price_lines():
    recipient = _recipient()
    pricing = _renewal_pricing_with_devices()
    quote = SponsoredQuote(
        recipient_id=RECIPIENT_ID,
        recipient_display_name='Иван И.',
        subscription_id=42,
        options=[(60, pricing.final_total)],
        price_lines_by_period={60: build_price_lines(pricing)},
    )

    with (
        patch.object(sponsored.RateLimitCache, 'is_rate_limited', AsyncMock(return_value=False)),
        patch.object(sponsored, 'resolve_recipient', AsyncMock(return_value=recipient)),
        patch.object(sponsored, 'quote_for_recipient', AsyncMock(return_value=quote)),
    ):
        response = await sponsored.lookup_recipient(
            body=sponsored.SponsoredLookupRequest(query='@ivan'),
            user=_payer(),
            db=AsyncMock(),
        )

    assert len(response.options) == 1
    option = response.options[0]
    assert option.price_lines
    assert any('устройств' in line.label.lower() for line in option.price_lines)
