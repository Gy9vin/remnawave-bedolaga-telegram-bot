"""Тесты для activate_paid_trial_core из trial_activation_service.

Проверяют, что ядро активации создаёт триал-подписку с корректными
duration/traffic/device, взятыми из настроек и тарифа, без дублирования
логики проверок и списания (они остаются в вызывателях).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings


def _make_user(user_id: int = 1) -> MagicMock:
    user = MagicMock()
    user.id = user_id
    user.telegram_id = 100_000 + user_id
    user.balance_kopeks = 999_999
    user.auth_type = 'telegram'
    return user


def _make_subscription(sub_id: int = 42) -> MagicMock:
    sub = MagicMock()
    sub.id = sub_id
    sub.user_id = 1
    return sub


@pytest.mark.asyncio
async def test_activate_paid_trial_core_creates_subscription(monkeypatch):
    """Основной сценарий: core создаёт подписку с duration/traffic/device из settings."""
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 7)
    monkeypatch.setattr(settings, 'TRIAL_TRAFFIC_LIMIT_GB', 10)
    monkeypatch.setattr(settings, 'TRIAL_DEVICE_LIMIT', 1)
    monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 0)

    user = _make_user()
    subscription = _make_subscription()
    db = AsyncMock()

    # Нет тарифа → используем параметры из settings
    monkeypatch.setattr(
        'app.database.crud.tariff.get_trial_tariff',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        'app.database.crud.server_squad.get_random_trial_squad_uuid',
        AsyncMock(return_value='squad-uuid-1'),
    )
    mock_create_trial_sub = AsyncMock(return_value=subscription)
    monkeypatch.setattr(
        'app.database.crud.subscription.create_trial_subscription',
        mock_create_trial_sub,
    )

    # Remnawave и уведомления — заглушки
    mock_sub_service = MagicMock()
    mock_sub_service.is_configured = False
    with patch('app.services.trial_activation_service.SubscriptionService', return_value=mock_sub_service):
        with patch('app.services.trial_activation_service.dispatch_generic_admin_notification_bg'):
            from app.services.trial_activation_service import activate_paid_trial_core

            result = await activate_paid_trial_core(db, user)

    assert result is subscription
    mock_create_trial_sub.assert_called_once()
    call_kwargs = mock_create_trial_sub.call_args.kwargs
    assert call_kwargs['duration_days'] == 7
    assert call_kwargs['traffic_limit_gb'] == 10
    assert call_kwargs['device_limit'] == 1
    assert call_kwargs['user_id'] == user.id


@pytest.mark.asyncio
async def test_activate_paid_trial_core_uses_tariff_params(monkeypatch):
    """Если есть триал-тариф, duration/traffic/device берутся из него."""
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 3)
    monkeypatch.setattr(settings, 'TRIAL_TRAFFIC_LIMIT_GB', 5)
    monkeypatch.setattr(settings, 'TRIAL_DEVICE_LIMIT', 1)
    monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 0)

    user = _make_user()
    subscription = _make_subscription()
    db = AsyncMock()

    trial_tariff = SimpleNamespace(
        id=10,
        name='Триал',
        traffic_limit_gb=20,
        device_limit=3,
        trial_duration_days=14,
        allowed_squads=['squad-a'],
    )
    monkeypatch.setattr(
        'app.database.crud.tariff.get_trial_tariff',
        AsyncMock(return_value=trial_tariff),
    )
    monkeypatch.setattr(
        'app.database.crud.server_squad.get_effective_tariff_squad_uuids',
        AsyncMock(return_value=['squad-uuid-a']),
    )
    mock_create_trial_sub = AsyncMock(return_value=subscription)
    monkeypatch.setattr(
        'app.database.crud.subscription.create_trial_subscription',
        mock_create_trial_sub,
    )

    mock_sub_service = MagicMock()
    mock_sub_service.is_configured = False
    with patch('app.services.trial_activation_service.SubscriptionService', return_value=mock_sub_service):
        with patch('app.services.trial_activation_service.dispatch_generic_admin_notification_bg'):
            from app.services.trial_activation_service import activate_paid_trial_core

            result = await activate_paid_trial_core(db, user)

    assert result is subscription
    call_kwargs = mock_create_trial_sub.call_args.kwargs
    assert call_kwargs['duration_days'] == 14  # из тарифа
    assert call_kwargs['traffic_limit_gb'] == 20  # из тарифа
    assert call_kwargs['device_limit'] == 3  # из тарифа
    assert call_kwargs['tariff_id'] == 10


@pytest.mark.asyncio
async def test_activate_paid_trial_core_remnawave_created(monkeypatch):
    """Когда Remnawave сконфигурирован, create_remnawave_user вызывается."""
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 7)
    monkeypatch.setattr(settings, 'TRIAL_TRAFFIC_LIMIT_GB', 10)
    monkeypatch.setattr(settings, 'TRIAL_DEVICE_LIMIT', 1)
    monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 0)

    user = _make_user()
    subscription = _make_subscription()
    db = AsyncMock()

    monkeypatch.setattr('app.database.crud.tariff.get_trial_tariff', AsyncMock(return_value=None))
    monkeypatch.setattr(
        'app.database.crud.server_squad.get_random_trial_squad_uuid',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        'app.database.crud.subscription.create_trial_subscription',
        AsyncMock(return_value=subscription),
    )

    panel_user = MagicMock()
    mock_sub_service = MagicMock()
    mock_sub_service.is_configured = True
    mock_sub_service.create_remnawave_user = AsyncMock(return_value=panel_user)

    with patch('app.services.trial_activation_service.SubscriptionService', return_value=mock_sub_service):
        with patch('app.services.trial_activation_service.dispatch_generic_admin_notification_bg'):
            from app.services.trial_activation_service import activate_paid_trial_core

            result = await activate_paid_trial_core(db, user)

    assert result is subscription
    mock_sub_service.create_remnawave_user.assert_called_once_with(db, subscription)


@pytest.mark.asyncio
async def test_activate_paid_trial_core_returns_subscription_on_remnawave_fail(monkeypatch):
    """Даже если Remnawave недоступен, core возвращает подписку (не бросает)."""
    monkeypatch.setattr(settings, 'TRIAL_DURATION_DAYS', 7)
    monkeypatch.setattr(settings, 'TRIAL_TRAFFIC_LIMIT_GB', 10)
    monkeypatch.setattr(settings, 'TRIAL_DEVICE_LIMIT', 1)
    monkeypatch.setattr(settings, 'TRIAL_ACTIVATION_PRICE', 0)

    user = _make_user()
    subscription = _make_subscription()
    db = AsyncMock()

    monkeypatch.setattr('app.database.crud.tariff.get_trial_tariff', AsyncMock(return_value=None))
    monkeypatch.setattr(
        'app.database.crud.server_squad.get_random_trial_squad_uuid',
        AsyncMock(return_value='squad-uuid-1'),
    )
    monkeypatch.setattr(
        'app.database.crud.subscription.create_trial_subscription',
        AsyncMock(return_value=subscription),
    )

    mock_sub_service = MagicMock()
    mock_sub_service.is_configured = True
    mock_sub_service.create_remnawave_user = AsyncMock(return_value=None)  # симулируем сбой

    mock_retry_queue = MagicMock()
    mock_retry_queue.enqueue = MagicMock()

    with patch('app.services.trial_activation_service.SubscriptionService', return_value=mock_sub_service):
        with patch('app.services.trial_activation_service.dispatch_generic_admin_notification_bg'):
            with patch('app.services.remnawave_retry_queue.remnawave_retry_queue', mock_retry_queue):
                from app.services.trial_activation_service import activate_paid_trial_core

                result = await activate_paid_trial_core(db, user)

    assert result is subscription
