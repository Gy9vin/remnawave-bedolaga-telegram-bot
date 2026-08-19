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


# ---------------------------------------------------------------------------
# Default Telegram text — непустые сообщения с ключевыми фразами
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_frozen_telegram_text_contains_key_phrases():
    """notify_subscription_frozen строит непустой telegram_message с ключевыми фразами."""
    from app.services.notification_delivery_service import NotificationDeliveryService

    service = NotificationDeliveryService()
    user = make_user()
    sub = make_subscription(frozen_days_banked=7)

    captured_kwargs = {}

    async def capture_send(**kwargs):
        captured_kwargs.update(kwargs)
        return True

    with (
        patch.object(service, 'send_notification', side_effect=capture_send),
        patch(
            'app.services.notification_delivery_service.format_email_datetime',
            return_value='01.01.2027 00:00',
        ),
        patch('app.services.notification_delivery_service.settings') as mock_settings,
    ):
        mock_settings.FREEZE_MAX_DAYS = 60
        await service.notify_subscription_frozen(user=user, subscription=sub)

    msg = captured_kwargs.get('telegram_message', '')
    assert msg, 'telegram_message не должен быть пустым'
    assert 'заморожена' in msg
    assert 'Сохранено дней' in msg
    assert '7' in msg  # frozen_days_banked


@pytest.mark.asyncio
async def test_unfrozen_telegram_text_auto_reason():
    """notify_subscription_unfrozen(reason='auto') строит текст с фразой об авто-разморозке."""
    from app.services.notification_delivery_service import NotificationDeliveryService

    service = NotificationDeliveryService()
    user = make_user()
    sub = make_subscription()

    captured_kwargs = {}

    async def capture_send(**kwargs):
        captured_kwargs.update(kwargs)
        return True

    with (
        patch.object(service, 'send_notification', side_effect=capture_send),
        patch('app.services.notification_delivery_service.format_email_datetime', return_value='31.12.2026 00:00'),
    ):
        await service.notify_subscription_unfrozen(user=user, subscription=sub, reason='auto')

    msg = captured_kwargs.get('telegram_message', '')
    assert msg, 'telegram_message не должен быть пустым'
    assert 'автоматически разморожена' in msg
    assert 'VPN снова активен' in msg


@pytest.mark.asyncio
async def test_unfrozen_telegram_text_manual_reason():
    """notify_subscription_unfrozen(reason='manual') строит текст с «VPN снова активен»."""
    from app.services.notification_delivery_service import NotificationDeliveryService

    service = NotificationDeliveryService()
    user = make_user()
    sub = make_subscription()

    captured_kwargs = {}

    async def capture_send(**kwargs):
        captured_kwargs.update(kwargs)
        return True

    with (
        patch.object(service, 'send_notification', side_effect=capture_send),
        patch('app.services.notification_delivery_service.format_email_datetime', return_value='31.12.2026 00:00'),
    ):
        await service.notify_subscription_unfrozen(user=user, subscription=sub, reason='manual')

    msg = captured_kwargs.get('telegram_message', '')
    assert msg
    assert 'VPN снова активен' in msg
    assert 'автоматически' not in msg  # manual reason — нет упоминания авто


# ---------------------------------------------------------------------------
# Email templates — непустой рендер с ключевыми фразами
# ---------------------------------------------------------------------------

def test_email_template_frozen_renders_nonempty():
    """EmailNotificationTemplates рендерит непустой шаблон SUBSCRIPTION_FROZEN."""
    from app.cabinet.services.email_templates import EmailNotificationTemplates
    from app.services.notification_delivery_service import NotificationType

    with patch('app.cabinet.services.email_templates.settings') as mock_settings:
        mock_settings.SMTP_FROM_NAME = 'TestVPN'
        mock_settings.CABINET_URL = 'https://example.com'
        templates = EmailNotificationTemplates()

    context = {
        'frozen_days_banked': 14,
        'auto_unfreeze_at': '19.10.2026 00:00',
        'freeze_max_days': 60,
    }
    result = templates.get_template(NotificationType.SUBSCRIPTION_FROZEN, 'ru', context)

    assert result is not None
    assert result.get('subject'), 'subject не должен быть пустым'
    body = result.get('body_html', '')
    assert body, 'body_html не должен быть пустым'
    assert 'заморожена' in body.lower() or 'заморожен' in body.lower()
    assert 'Сохранено дней' in body
    assert '14' in body


def test_email_template_unfrozen_auto_renders_nonempty():
    """EmailNotificationTemplates рендерит шаблон SUBSCRIPTION_UNFROZEN для reason=auto."""
    from app.cabinet.services.email_templates import EmailNotificationTemplates
    from app.services.notification_delivery_service import NotificationType

    with patch('app.cabinet.services.email_templates.settings') as mock_settings:
        mock_settings.SMTP_FROM_NAME = 'TestVPN'
        mock_settings.CABINET_URL = 'https://example.com'
        templates = EmailNotificationTemplates()

    context = {
        'reason': 'auto',
        'new_end_date': '31.12.2026 00:00',
    }
    result = templates.get_template(NotificationType.SUBSCRIPTION_UNFROZEN, 'ru', context)

    assert result is not None
    body = result.get('body_html', '')
    assert 'автоматически разморожена' in body or 'автоматически' in body
    assert 'VPN снова активен' in body


def test_email_template_unfrozen_manual_renders_nonempty():
    """EmailNotificationTemplates рендерит шаблон SUBSCRIPTION_UNFROZEN для reason=manual."""
    from app.cabinet.services.email_templates import EmailNotificationTemplates
    from app.services.notification_delivery_service import NotificationType

    with patch('app.cabinet.services.email_templates.settings') as mock_settings:
        mock_settings.SMTP_FROM_NAME = 'TestVPN'
        mock_settings.CABINET_URL = 'https://example.com'
        templates = EmailNotificationTemplates()

    context = {
        'reason': 'manual',
        'new_end_date': '31.12.2026 00:00',
    }
    result = templates.get_template(NotificationType.SUBSCRIPTION_UNFROZEN, 'ru', context)

    assert result is not None
    body = result.get('body_html', '')
    assert 'VPN снова активен' in body
    assert '31.12.2026' in body
