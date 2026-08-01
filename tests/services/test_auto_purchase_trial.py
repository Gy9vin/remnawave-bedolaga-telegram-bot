"""Тесты для _auto_purchase_trial и регистрации в _process_single_cart.

TDD: тесты написаны ДО реализации и должны быть RED до добавления
_auto_purchase_trial в subscription_auto_purchase_service.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.config import settings


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------


def _make_user(
    user_id: int = 1,
    balance_kopeks: int = 1000,
    trial_used: bool = False,
    telegram_id: int | None = 100001,
) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.telegram_id = telegram_id
    user.balance_kopeks = balance_kopeks
    user.is_trial_already_used = MagicMock(return_value=trial_used)
    user.language = 'ru'
    user.email = None
    return user


def _make_subscription(sub_id: int = 42) -> MagicMock:
    sub = MagicMock()
    sub.id = sub_id
    sub.user_id = 1
    sub.end_date = None
    return sub


def _make_db() -> AsyncMock:
    db = AsyncMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# Общие моки для тестов _auto_purchase_trial
# ---------------------------------------------------------------------------


def _patch_auto_purchase_trial_deps(
    monkeypatch,
    *,
    subtract_success: bool = True,
    subtract_raises: Exception | None = None,
    activate_raises: Exception | None = None,
    subscription: MagicMock | None = None,
    active_subs: list | None = None,
):
    """Накладывает стандартный набор заглушек для _auto_purchase_trial."""
    _sub = subscription or _make_subscription()

    # subtract_user_balance
    if subtract_raises:
        mock_subtract = AsyncMock(side_effect=subtract_raises)
    else:
        mock_subtract = AsyncMock(return_value=subtract_success)
    monkeypatch.setattr(
        'app.services.subscription_auto_purchase_service.subtract_user_balance',
        mock_subtract,
    )

    # activate_paid_trial_core (lazy import внутри функции → патчим в source-модуле)
    if activate_raises:
        mock_activate = AsyncMock(side_effect=activate_raises)
    else:
        mock_activate = AsyncMock(return_value=_sub)

    monkeypatch.setattr(
        'app.services.trial_activation_service.activate_paid_trial_core',
        mock_activate,
    )

    # add_user_balance (для компенсирующего возврата)
    mock_add = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.database.crud.user.add_user_balance',
        mock_add,
    )

    # create_transaction
    mock_tx = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(
        'app.services.subscription_auto_purchase_service.create_transaction',
        mock_tx,
    )

    # lock_user_for_pricing — возвращает тот же user (уже залочен)
    mock_lock = AsyncMock(side_effect=lambda db, uid: _make_user(uid))
    monkeypatch.setattr(
        'app.database.crud.user.lock_user_for_pricing',
        mock_lock,
    )

    # get_active_subscriptions_by_user_id
    mock_active = AsyncMock(return_value=active_subs if active_subs is not None else [])
    monkeypatch.setattr(
        'app.database.crud.subscription.get_active_subscriptions_by_user_id',
        mock_active,
    )

    # _delete_cart_for_subscription и clear_subscription_checkout_draft
    monkeypatch.setattr(
        'app.services.subscription_auto_purchase_service._delete_cart_for_subscription',
        AsyncMock(),
    )
    monkeypatch.setattr(
        'app.services.subscription_auto_purchase_service.clear_subscription_checkout_draft',
        AsyncMock(),
    )

    # notify_user_subscription_activated (WS)
    monkeypatch.setattr(
        'app.cabinet.routes.websocket.notify_user_subscription_activated',
        AsyncMock(),
    )

    # with_admin_notification_service
    monkeypatch.setattr(
        'app.services.subscription_renewal_service.with_admin_notification_service',
        AsyncMock(),
    )

    # _notify_email_user_auto_purchase
    monkeypatch.setattr(
        'app.services.subscription_auto_purchase_service._notify_email_user_auto_purchase',
        AsyncMock(),
    )

    return {
        'subtract': mock_subtract,
        'activate': mock_activate,
        'add_balance': mock_add,
        'create_tx': mock_tx,
        'lock': mock_lock,
        'subscription': _sub,
    }


# ---------------------------------------------------------------------------
# Тест 1: успешная автоактивация триала при достаточном балансе
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trial_activated_when_affordable(monkeypatch):
    """TRIAL_PAYMENT_ENABLED=True, balance==price, trial not used, no active sub → True.

    subtract_user_balance вызван ровно 1 раз с ценой,
    activate_paid_trial_core вызван ровно 1 раз.
    """
    monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 1000)

    user = _make_user(balance_kopeks=1000, trial_used=False)
    db = _make_db()
    cart_data = {'cart_mode': 'trial_purchase'}

    mocks = _patch_auto_purchase_trial_deps(monkeypatch, active_subs=[])

    # Перекрываем lock, чтобы он возвращал наш user с нужным балансом
    monkeypatch.setattr(
        'app.database.crud.user.lock_user_for_pricing',
        AsyncMock(return_value=user),
    )

    from app.services.subscription_auto_purchase_service import _auto_purchase_trial

    result = await _auto_purchase_trial(db, user, cart_data, bot=None)

    assert result is True
    mocks['subtract'].assert_called_once()
    call_args = mocks['subtract'].call_args
    # Первый позиционный аргумент — db, второй — user, третий — сумма (price)
    amount_arg = call_args.args[2] if len(call_args.args) >= 3 else call_args.kwargs.get('amount')
    assert amount_arg == 1000, f'Ожидали списание 1000 коп, получили {amount_arg}'

    mocks['activate'].assert_called_once()


# ---------------------------------------------------------------------------
# Тест 2: пропуск, если триал уже использован
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_when_trial_already_used(monkeypatch):
    """is_trial_already_used()=True → возвращает False без списания."""
    monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 1000)

    user = _make_user(balance_kopeks=5000, trial_used=True)
    db = _make_db()
    cart_data = {'cart_mode': 'trial_purchase'}

    mocks = _patch_auto_purchase_trial_deps(monkeypatch)

    from app.services.subscription_auto_purchase_service import _auto_purchase_trial

    result = await _auto_purchase_trial(db, user, cart_data, bot=None)

    assert result is False
    mocks['subtract'].assert_not_called()


# ---------------------------------------------------------------------------
# Тест 3: пропуск при недостаточном балансе
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_when_insufficient_balance(monkeypatch):
    """balance < TRIAL_ACTIVATION_PRICE → возвращает False без списания."""
    monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 1000)

    user = _make_user(balance_kopeks=999, trial_used=False)
    db = _make_db()
    cart_data = {'cart_mode': 'trial_purchase'}

    mocks = _patch_auto_purchase_trial_deps(monkeypatch, active_subs=[])

    # lock возвращает того же user с балансом 999
    monkeypatch.setattr(
        'app.database.crud.user.lock_user_for_pricing',
        AsyncMock(return_value=user),
    )

    from app.services.subscription_auto_purchase_service import _auto_purchase_trial

    result = await _auto_purchase_trial(db, user, cart_data, bot=None)

    assert result is False
    mocks['subtract'].assert_not_called()


# ---------------------------------------------------------------------------
# Тест 4: компенсирующий возврат при сбое activate_paid_trial_core
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refund_on_activation_failure(monkeypatch):
    """activate_paid_trial_core raises → False, компенсирующий add_user_balance вызван."""
    monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 1000)

    user = _make_user(balance_kopeks=1000, trial_used=False)
    db = _make_db()
    cart_data = {'cart_mode': 'trial_purchase'}

    mocks = _patch_auto_purchase_trial_deps(
        monkeypatch,
        active_subs=[],
        activate_raises=RuntimeError('panel down'),
    )

    # lock возвращает того же user с нужным балансом
    monkeypatch.setattr(
        'app.database.crud.user.lock_user_for_pricing',
        AsyncMock(return_value=user),
    )

    # add_user_balance для рефанда
    mock_add_balance = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.database.crud.user.add_user_balance',
        mock_add_balance,
    )

    from app.services.subscription_auto_purchase_service import _auto_purchase_trial

    result = await _auto_purchase_trial(db, user, cart_data, bot=None)

    assert result is False
    # subtract должен был вызваться (списание прошло до ошибки активации)
    mocks['subtract'].assert_called_once()
    # rollback должен был вызваться
    db.rollback.assert_called()
    # компенсирующий возврат должен был вызваться
    mock_add_balance.assert_called()
    # проверяем, что возврат был на ту же сумму
    refund_call_args = mock_add_balance.call_args
    refund_amount = (
        refund_call_args.args[2]
        if len(refund_call_args.args) >= 3
        else refund_call_args.kwargs.get('amount')
    )
    assert refund_amount == 1000, f'Ожидали возврат 1000 коп, получили {refund_amount}'


# ---------------------------------------------------------------------------
# Тест 5: пропуск при TRIAL_PAYMENT_ENABLED=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_when_trial_payment_disabled(monkeypatch):
    """TRIAL_PAYMENT_ENABLED=False → возвращает False без списания."""
    monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', False)
    monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 1000)

    user = _make_user(balance_kopeks=5000, trial_used=False)
    db = _make_db()
    cart_data = {'cart_mode': 'trial_purchase'}

    mocks = _patch_auto_purchase_trial_deps(monkeypatch)

    from app.services.subscription_auto_purchase_service import _auto_purchase_trial

    result = await _auto_purchase_trial(db, user, cart_data, bot=None)

    assert result is False
    mocks['subtract'].assert_not_called()


# ---------------------------------------------------------------------------
# Тест 6: диспетчер _process_single_cart маршрутизирует trial_purchase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_routes_trial_purchase(monkeypatch):
    """_process_single_cart с cart_mode='trial_purchase' вызывает _auto_purchase_trial."""
    monkeypatch.setattr(settings, 'TRIAL_PAYMENT_ENABLED', True)
    monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 1000)

    user = _make_user(balance_kopeks=1000, trial_used=False)
    db = _make_db()
    cart_data = {'cart_mode': 'trial_purchase'}

    mock_auto_trial = AsyncMock(return_value=True)
    monkeypatch.setattr(
        'app.services.subscription_auto_purchase_service._auto_purchase_trial',
        mock_auto_trial,
    )

    # Guard: _is_subscription_disabled
    monkeypatch.setattr(
        'app.services.subscription_auto_purchase_service._is_subscription_disabled',
        AsyncMock(return_value=False),
    )

    # get_user_transactions
    monkeypatch.setattr(
        'app.database.crud.transaction.get_user_transactions',
        AsyncMock(return_value=[]),
    )

    from app.services.subscription_auto_purchase_service import _process_single_cart

    result = await _process_single_cart(db, user, cart_data, bot=None)

    assert result is True
    mock_auto_trial.assert_called_once_with(db, user, cart_data, bot=None)
