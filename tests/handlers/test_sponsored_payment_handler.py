"""Оплата подписки за другого человека: хендлер app/handlers/sponsored_payment.py.

БД не поднимаем — сервисный слой (resolve_recipient / quote_for_recipient /
pay_from_balance) мокается, проверяем только логику хендлера: переходы FSM,
тексты и то, что при недостатке денег/успехе вызываются нужные сервисы.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.handlers.sponsored_payment as handler_mod
from app.services.recipient_lookup import RecipientIsPayerError
from app.services.sponsored_payment_service import InsufficientBalanceError, SponsoredQuote


def _payer(balance_kopeks=100_00):
    return SimpleNamespace(id=1, telegram_id=111, language='ru', balance_kopeks=balance_kopeks, full_name='Плательщик')


def _recipient(user_id=2, telegram_id=222):
    return SimpleNamespace(id=user_id, telegram_id=telegram_id, language='ru', full_name='Получатель Имя')


def _message(text='@someone'):
    msg = MagicMock()
    msg.text = text
    msg.answer = AsyncMock()
    return msg


def _callback(data=''):
    cb = MagicMock()
    cb.data = data
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    cb.bot = AsyncMock()
    return cb


def _state(data=None):
    state = MagicMock()
    state.get_data = AsyncMock(return_value=data or {})
    state.update_data = AsyncMock()
    state.set_state = AsyncMock()
    state.clear = AsyncMock()
    return state


@pytest.mark.asyncio
async def test_recipient_not_found_stays_in_input_state():
    """Не найден -> сообщение и НЕ выходим из состояния ввода (можно ввести снова)."""
    message, payer, state = _message('@ghost'), _payer(), _state()
    db = MagicMock()

    with patch.object(handler_mod, 'resolve_recipient', AsyncMock(return_value=None)):
        await handler_mod.process_recipient_query(message, payer, db, state)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert 'найден' in text.lower()
    state.clear.assert_not_awaited()
    state.set_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_recipient_is_payer_shows_message_and_exits():
    """Сам себе -> понятное сообщение, FSM очищается (выходим из сценария)."""
    message, payer, state = _message('@me'), _payer(), _state()
    db = MagicMock()

    with patch.object(handler_mod, 'resolve_recipient', AsyncMock(side_effect=RecipientIsPayerError)):
        await handler_mod.process_recipient_query(message, payer, db, state)

    message.answer.assert_awaited_once()
    text = message.answer.await_args.args[0]
    assert 'это вы' in text.lower()
    state.clear.assert_awaited_once()


@pytest.mark.asyncio
async def test_card_omits_tariff_name_and_expiry_date():
    """Карточка содержит только имя получателя и цены периодов — не тариф и не дату."""
    message, payer, state = _message('@friend'), _payer(), _state()
    db = MagicMock()
    recipient = _recipient()

    quote = SponsoredQuote(
        recipient_id=recipient.id,
        recipient_display_name=recipient.full_name,
        subscription_id=99,
        options=[(30, 15000), (90, 40000)],
    )

    with (
        patch.object(handler_mod, 'resolve_recipient', AsyncMock(return_value=recipient)),
        patch.object(handler_mod, 'quote_for_recipient', AsyncMock(return_value=quote)),
    ):
        await handler_mod.process_recipient_query(message, payer, db, state)

    message.answer.assert_awaited_once()
    _, kwargs = message.answer.await_args
    card_text = message.answer.await_args.args[0]
    keyboard = kwargs['reply_markup']

    assert recipient.full_name in card_text
    forbidden = ('тариф', 'заканчива', 'истека', 'устройств', 'до 20', 'подписка действ')
    assert not any(word in card_text.lower() for word in forbidden)

    button_texts = [button.text for row in keyboard.inline_keyboard for button in row]
    assert any('30 дн.' in text for text in button_texts)
    assert any('90 дн.' in text for text in button_texts)
    for text in button_texts:
        assert not any(word in text.lower() for word in forbidden)

    # FSM переходит к ожиданию выбора периода, а цены зафиксированы в данных состояния.
    state.set_state.assert_awaited_once_with(handler_mod.SponsoredPaymentStates.waiting_for_period)
    state.update_data.assert_awaited_once()
    saved = state.update_data.await_args.kwargs
    assert saved[handler_mod._DATA_OPTIONS] == {'30': 15000, '90': 40000}


@pytest.mark.asyncio
async def test_insufficient_balance_shows_missing_amount():
    """Не хватает денег -> InsufficientBalanceError -> в тексте видна недостающая сумма."""
    payer = _payer(balance_kopeks=5000)
    recipient = _recipient()
    callback = _callback('sponsored_pay_period:30')
    state = _state(
        {
            handler_mod._DATA_RECIPIENT_ID: recipient.id,
            handler_mod._DATA_SUBSCRIPTION_ID: 99,
            handler_mod._DATA_OPTIONS: {'30': 15000},
        }
    )
    db = MagicMock()
    subscription = SimpleNamespace(id=99)

    with (
        patch.object(handler_mod, 'get_user_by_id', AsyncMock(return_value=recipient)),
        patch.object(handler_mod, 'get_subscription_by_user_id', AsyncMock(return_value=subscription)),
        patch.object(handler_mod, 'pay_from_balance', AsyncMock(side_effect=InsufficientBalanceError)),
        patch.object(handler_mod, 'get_insufficient_balance_keyboard', MagicMock(return_value='KB')),
    ):
        await handler_mod.handle_period_selection(callback, payer, db, state)

    callback.message.edit_text.assert_awaited_once()
    text = callback.message.edit_text.await_args.args[0]
    # 15000 - 5000 = 10000 копеек = 100 руб. не хватает.
    assert '100' in text
    state.clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_payment_calls_pay_from_balance_and_notifies_recipient():
    """Успешная оплата -> вызван pay_from_balance и отправлено уведомление получателю."""
    payer = _payer(balance_kopeks=100_000)
    recipient = _recipient()
    callback = _callback('sponsored_pay_period:30')
    state = _state(
        {
            handler_mod._DATA_RECIPIENT_ID: recipient.id,
            handler_mod._DATA_SUBSCRIPTION_ID: 99,
            handler_mod._DATA_OPTIONS: {'30': 15000},
        }
    )
    db = MagicMock()
    subscription = SimpleNamespace(id=99)
    pay_mock = AsyncMock(return_value=MagicMock())

    with (
        patch.object(handler_mod, 'get_user_by_id', AsyncMock(return_value=recipient)),
        patch.object(handler_mod, 'get_subscription_by_user_id', AsyncMock(return_value=subscription)),
        patch.object(handler_mod, 'pay_from_balance', pay_mock),
    ):
        await handler_mod.handle_period_selection(callback, payer, db, state)

    pay_mock.assert_awaited_once()
    _, pay_kwargs = pay_mock.await_args
    assert pay_kwargs['payer'] is payer
    assert pay_kwargs['recipient'] is recipient
    assert pay_kwargs['subscription'] is subscription
    assert pay_kwargs['period_days'] == 30
    assert pay_kwargs['price_kopeks'] == 15000

    state.clear.assert_awaited_once()
    callback.message.edit_text.assert_awaited_once()

    callback.bot.send_message.assert_awaited_once()
    _, notify_kwargs = callback.bot.send_message.await_args
    assert notify_kwargs['chat_id'] == recipient.telegram_id
    assert payer.full_name in notify_kwargs['text']
    assert '30' in notify_kwargs['text']


@pytest.mark.asyncio
async def test_notification_failure_does_not_break_payment():
    """Уведомление получателю упало -> оплата всё равно применена, исключение не всплывает."""
    payer = _payer(balance_kopeks=100_000)
    recipient = _recipient()
    callback = _callback('sponsored_pay_period:30')
    state = _state(
        {
            handler_mod._DATA_RECIPIENT_ID: recipient.id,
            handler_mod._DATA_SUBSCRIPTION_ID: 99,
            handler_mod._DATA_OPTIONS: {'30': 15000},
        }
    )
    db = MagicMock()
    subscription = SimpleNamespace(id=99)
    pay_mock = AsyncMock(return_value=MagicMock())
    callback.bot.send_message = AsyncMock(side_effect=Exception('Forbidden: bot was blocked by the user'))

    with (
        patch.object(handler_mod, 'get_user_by_id', AsyncMock(return_value=recipient)),
        patch.object(handler_mod, 'get_subscription_by_user_id', AsyncMock(return_value=subscription)),
        patch.object(handler_mod, 'pay_from_balance', pay_mock),
    ):
        # Не должно бросать исключение наружу.
        await handler_mod.handle_period_selection(callback, payer, db, state)

    pay_mock.assert_awaited_once()
    state.clear.assert_awaited_once()
    callback.message.edit_text.assert_awaited_once()
    callback.bot.send_message.assert_awaited_once()


def test_register_handlers_wires_router():
    """Регистрация не падает и вешает по одному хендлеру на сообщение и оба callback'а."""
    dp = MagicMock()
    handler_mod.register_handlers(dp)

    assert dp.message.register.call_count == 1
    assert dp.callback_query.register.call_count == 2
    registered_callbacks = [call.args[0] for call in dp.callback_query.register.call_args_list]
    assert handler_mod.start_sponsored_payment in registered_callbacks
    assert handler_mod.handle_period_selection in registered_callbacks
    assert dp.message.register.call_args.args[0] is handler_mod.process_recipient_query
