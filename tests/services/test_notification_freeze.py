"""
Юнит-тесты для notify_subscription_frozen / notify_subscription_unfrozen
и соответствующих значений NotificationType.SUBSCRIPTION_FROZEN / SUBSCRIPTION_UNFROZEN.

Задача 8 — NotificationType + методы уведомления о заморозке подписки.
"""
import pytest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_user(telegram_id: int | None = 12345):
    user = MagicMock()
    user.id = 1
    user.telegram_id = telegram_id
    user.email = 'user@example.com'
    user.email_verified = True
    user.status = 'active'
    return user


def make_subscription(
    frozen_days_banked: int = 10,
    frozen_auto_unfreeze_at=None,
    end_date=None,
):
    sub = MagicMock()
    sub.frozen_days_banked = frozen_days_banked
    sub.frozen_auto_unfreeze_at = frozen_auto_unfreeze_at or (
        datetime.now(UTC) + timedelta(days=60)
    )
    sub.end_date = end_date or datetime(2026, 12, 31, tzinfo=UTC)
    return sub


# ---------------------------------------------------------------------------
# NotificationType values
# ---------------------------------------------------------------------------

def test_notification_type_frozen_value():
    from app.services.notification_delivery_service import NotificationType
    assert NotificationType.SUBSCRIPTION_FROZEN.value == 'subscription_frozen'


def test_notification_type_unfrozen_value():
    from app.services.notification_delivery_service import NotificationType
    assert NotificationType.SUBSCRIPTION_UNFROZEN.value == 'subscription_unfrozen'


def test_notification_type_both_present():
    from app.services.notification_delivery_service import NotificationType
    values = [e.value for e in NotificationType]
    assert 'subscription_frozen' in values
    assert 'subscription_unfrozen' in values


# ---------------------------------------------------------------------------
# notify_subscription_frozen
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_subscription_frozen_calls_send_notification():
    """notify_subscription_frozen вызывает send_notification с SUBSCRIPTION_FROZEN и корректным context."""
    from app.services.notification_delivery_service import (
        NotificationDeliveryService,
        NotificationType,
    )

    service = NotificationDeliveryService()
    user = make_user()
    sub = make_subscription(frozen_days_banked=7)
    expected_auto_unfreeze = sub.frozen_auto_unfreeze_at

    mock_send = AsyncMock(return_value=True)

    with (
        patch.object(service, 'send_notification', mock_send),
        patch(
            'app.services.notification_delivery_service.format_email_datetime',
            side_effect=lambda dt: f'formatted:{dt}',
        ),
        patch(
            'app.services.notification_delivery_service.settings'
        ) as mock_settings,
    ):
        mock_settings.FREEZE_MAX_DAYS = 60
        result = await service.notify_subscription_frozen(user=user, subscription=sub)

    assert result is True
    mock_send.assert_awaited_once()
    call_kwargs = mock_send.await_args.kwargs
    assert call_kwargs['notification_type'] == NotificationType.SUBSCRIPTION_FROZEN
    assert call_kwargs['context']['frozen_days_banked'] == 7
    assert call_kwargs['context']['freeze_max_days'] == 60
    assert call_kwargs['context']['auto_unfreeze_at'] == f'formatted:{expected_auto_unfreeze}'


@pytest.mark.asyncio
async def test_notify_subscription_frozen_passes_bot_and_markup():
    """bot, telegram_message, telegram_markup пробрасываются в send_notification."""
    from app.services.notification_delivery_service import NotificationDeliveryService

    service = NotificationDeliveryService()
    user = make_user()
    sub = make_subscription()
    fake_bot = MagicMock()
    mock_send = AsyncMock(return_value=True)

    with (
        patch.object(service, 'send_notification', mock_send),
        patch('app.services.notification_delivery_service.format_email_datetime', return_value='dt'),
        patch('app.services.notification_delivery_service.settings') as mock_settings,
    ):
        mock_settings.FREEZE_MAX_DAYS = 60
        await service.notify_subscription_frozen(
            user=user,
            subscription=sub,
            bot=fake_bot,
            telegram_message='msg',
            telegram_markup='markup',
        )

    call_kwargs = mock_send.await_args.kwargs
    assert call_kwargs['bot'] is fake_bot
    assert call_kwargs['telegram_message'] == 'msg'
    assert call_kwargs['telegram_markup'] == 'markup'


# ---------------------------------------------------------------------------
# notify_subscription_unfrozen
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_subscription_unfrozen_manual():
    """notify_subscription_unfrozen с reason='manual' → SUBSCRIPTION_UNFROZEN + корректный context."""
    from app.services.notification_delivery_service import (
        NotificationDeliveryService,
        NotificationType,
    )

    service = NotificationDeliveryService()
    user = make_user()
    sub = make_subscription()
    expected_end_date = sub.end_date

    mock_send = AsyncMock(return_value=True)

    with (
        patch.object(service, 'send_notification', mock_send),
        patch(
            'app.services.notification_delivery_service.format_email_datetime',
            side_effect=lambda dt: f'formatted:{dt}',
        ),
    ):
        result = await service.notify_subscription_unfrozen(
            user=user, subscription=sub, reason='manual'
        )

    assert result is True
    mock_send.assert_awaited_once()
    call_kwargs = mock_send.await_args.kwargs
    assert call_kwargs['notification_type'] == NotificationType.SUBSCRIPTION_UNFROZEN
    assert call_kwargs['context']['reason'] == 'manual'
    assert call_kwargs['context']['new_end_date'] == f'formatted:{expected_end_date}'


@pytest.mark.asyncio
async def test_notify_subscription_unfrozen_auto():
    """reason='auto' передаётся в context."""
    from app.services.notification_delivery_service import NotificationDeliveryService

    service = NotificationDeliveryService()
    user = make_user()
    sub = make_subscription()

    mock_send = AsyncMock(return_value=True)

    with (
        patch.object(service, 'send_notification', mock_send),
        patch('app.services.notification_delivery_service.format_email_datetime', return_value='dt'),
    ):
        await service.notify_subscription_unfrozen(user=user, subscription=sub, reason='auto')

    assert mock_send.await_args.kwargs['context']['reason'] == 'auto'


@pytest.mark.asyncio
async def test_notify_subscription_unfrozen_admin():
    """reason='admin' передаётся в context."""
    from app.services.notification_delivery_service import NotificationDeliveryService

    service = NotificationDeliveryService()
    user = make_user()
    sub = make_subscription()

    mock_send = AsyncMock(return_value=True)

    with (
        patch.object(service, 'send_notification', mock_send),
        patch('app.services.notification_delivery_service.format_email_datetime', return_value='dt'),
    ):
        await service.notify_subscription_unfrozen(user=user, subscription=sub, reason='admin')

    assert mock_send.await_args.kwargs['context']['reason'] == 'admin'


@pytest.mark.asyncio
async def test_notify_subscription_unfrozen_default_reason():
    """По умолчанию reason='manual'."""
    from app.services.notification_delivery_service import NotificationDeliveryService

    service = NotificationDeliveryService()
    user = make_user()
    sub = make_subscription()

    mock_send = AsyncMock(return_value=True)

    with (
        patch.object(service, 'send_notification', mock_send),
        patch('app.services.notification_delivery_service.format_email_datetime', return_value='dt'),
    ):
        await service.notify_subscription_unfrozen(user=user, subscription=sub)

    assert mock_send.await_args.kwargs['context']['reason'] == 'manual'
