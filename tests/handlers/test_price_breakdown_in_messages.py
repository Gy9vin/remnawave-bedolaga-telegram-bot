"""Расшифровка цены (app.services.price_breakdown) в сообщениях бота.

Проверяем только то, что в местах, куда её встроили, реально вызывается
``format_price_lines`` и что итоговый текст объясняет ежемесячную природу
цены допустройств («в месяц») — а не сам расчёт цены (он тестируется в
price_breakdown и pricing_engine отдельно). БД не поднимаем, сервисный слой
мокается — стиль как в tests/handlers/test_sponsored_payment_handler.py.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.handlers.sponsored_payment as sponsored_handler_mod
import app.handlers.subscription.purchase as purchase_mod
from app.services import price_breakdown
from app.services.pricing_engine import RenewalPricing
from app.services.sponsored_payment_service import SponsoredQuote


def _pricing_with_extra_devices(*, final_total=16000, extra_devices=2, months=3) -> RenewalPricing:
    """RenewalPricing с допустройствами — чтобы hint про «в месяц» точно появился."""
    return RenewalPricing(
        base_price=10000,
        servers_price=0,
        traffic_price=0,
        devices_price=6000,
        promo_group_discount=0,
        promo_offer_discount=0,
        final_total=final_total,
        period_days=90,
        is_tariff_mode=False,
        breakdown={'extra_devices': extra_devices, 'months_in_period': months},
    )


# --- Место 1: продление своей подписки (confirm_extend_subscription) -------


def _callback(data):
    cb = MagicMock()
    cb.data = data
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    cb.bot = AsyncMock()
    return cb


@pytest.mark.asyncio
async def test_confirm_extend_subscription_shows_price_breakdown_with_monthly_hint():
    """После продления в success-сообщении есть расшифровка (format_price_lines
    вызван) и явное объяснение, что цена допустройств — ежемесячная."""
    pricing = _pricing_with_extra_devices()
    db_user = SimpleNamespace(
        id=1,
        telegram_id=111,
        language='ru',
        balance_kopeks=100_000,
        subscription=SimpleNamespace(id=5, device_limit=5, traffic_limit_gb=100, connected_squads=[]),
    )
    callback = _callback('confirm_extend_90')
    db = MagicMock()
    db.refresh = AsyncMock()

    finalize_result = SimpleNamespace(
        subscription=SimpleNamespace(end_date=datetime(2026, 12, 1, tzinfo=UTC), traffic_limit_gb=100)
    )
    renewal_service_instance = MagicMock()
    renewal_service_instance.finalize = AsyncMock(return_value=finalize_result)

    with (
        patch('app.config.Settings.is_tariffs_mode', return_value=False),
        patch('app.config.Settings.get_available_renewal_periods', return_value=[30, 90]),
        patch('app.config.Settings.is_multi_tariff_enabled', return_value=False),
        patch('app.config.Settings.is_traffic_fixed', return_value=False),
        patch('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=db_user)),
        patch(
            'app.services.pricing_engine.pricing_engine.calculate_renewal_price',
            AsyncMock(return_value=pricing),
        ),
        patch(
            'app.services.subscription_renewal_service.SubscriptionRenewalService',
            MagicMock(return_value=renewal_service_instance),
        ),
        patch.object(purchase_mod, 'get_back_keyboard', MagicMock(return_value='KB')),
        patch.object(purchase_mod, 'format_price_lines', wraps=price_breakdown.format_price_lines) as format_spy,
    ):
        await purchase_mod.confirm_extend_subscription(callback, db_user, db, state=None)

    format_spy.assert_called_once()

    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.await_args.args[0]

    assert 'Из чего сложилась цена' in text
    assert 'в месяц' in text
    assert 'Итого' in text


# --- Место 3: оплата подписки за другого (sponsored_payment) ---------------


def _message(text='@friend'):
    msg = MagicMock()
    msg.text = text
    msg.answer = AsyncMock()
    return msg


def _state(data=None):
    state = MagicMock()
    state.get_data = AsyncMock(return_value=data or {})
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()
    state.clear = AsyncMock()
    return state


def _recipient(user_id=2, telegram_id=222):
    return SimpleNamespace(id=user_id, telegram_id=telegram_id, language='ru', full_name='Получатель Имя')


@pytest.mark.asyncio
async def test_sponsored_payment_card_step_computes_breakdown_via_format_price_lines():
    """На шаге показа карточки периодов расшифровка уже считается (через
    format_price_lines) из quote.price_lines_by_period и кладётся в FSM."""
    message, state = _message(), _state()
    payer = SimpleNamespace(id=1, telegram_id=111, language='ru', balance_kopeks=100_00, full_name='Плательщик')
    db = MagicMock()
    recipient = _recipient()

    pricing = _pricing_with_extra_devices(final_total=16000)
    price_lines = price_breakdown.build_price_lines(pricing)
    assert price_lines  # sanity: fixture actually produces lines

    quote = SponsoredQuote(
        recipient_id=recipient.id,
        recipient_display_name=recipient.full_name,
        subscription_id=99,
        options=[(90, 16000)],
        price_lines_by_period={90: price_lines},
    )

    with (
        patch.object(sponsored_handler_mod, 'resolve_recipient', AsyncMock(return_value=recipient)),
        patch.object(sponsored_handler_mod, 'quote_for_recipient', AsyncMock(return_value=quote)),
        patch.object(
            sponsored_handler_mod, 'format_price_lines', wraps=price_breakdown.format_price_lines
        ) as format_spy,
    ):
        await sponsored_handler_mod.process_recipient_query(message, payer, db, state)

    format_spy.assert_called_once()

    state.update_data.assert_awaited_once()
    saved = state.update_data.await_args.kwargs
    breakdown = saved[sponsored_handler_mod._DATA_BREAKDOWN]
    assert 'в месяц' in breakdown['90']
    assert 'Итого' in breakdown['90']

    # Карточка периодов сама по себе остаётся скупой — расшифровка в ней не
    # показывается (полностью повторяет test_sponsored_payment_handler.py).
    card_text = message.answer.await_args.args[0]
    assert 'в месяц' not in card_text


@pytest.mark.asyncio
async def test_sponsored_payment_success_message_shows_stored_breakdown():
    """После оплаты (шаг выбора периода) в сообщении о результате видна
    зафиксированная на шаге карточки расшифровка с пояснением про «в месяц»."""
    payer = SimpleNamespace(id=1, telegram_id=111, language='ru', balance_kopeks=100_000, full_name='Плательщик')
    recipient = _recipient()
    callback = _callback('sponsored_pay_period:90')
    breakdown_text = 'Доп. устройства (2 шт.) — 60,00 ₽\n<i>10,00 ₽ в месяц за устройство × 2 шт. × 3 мес.</i>\n\n<b>Итого:</b> 160,00 ₽'
    state = _state(
        {
            sponsored_handler_mod._DATA_RECIPIENT_ID: recipient.id,
            sponsored_handler_mod._DATA_SUBSCRIPTION_ID: 99,
            sponsored_handler_mod._DATA_OPTIONS: {'90': 16000},
            sponsored_handler_mod._DATA_BREAKDOWN: {'90': breakdown_text},
        }
    )
    db = MagicMock()
    subscription = SimpleNamespace(id=99)

    with (
        patch.object(sponsored_handler_mod, 'get_user_by_id', AsyncMock(return_value=recipient)),
        patch.object(sponsored_handler_mod, 'get_subscription_by_user_id', AsyncMock(return_value=subscription)),
        patch.object(sponsored_handler_mod, 'pay_from_balance', AsyncMock(return_value=MagicMock())),
    ):
        await sponsored_handler_mod.handle_period_selection(callback, payer, db, state)

    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.await_args.args[0]
    assert 'Из чего сложилась цена' in text
    assert 'в месяц' in text
    assert 'Итого' in text
