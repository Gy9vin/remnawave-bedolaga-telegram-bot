"""
Юнит-тесты для freeze_subscription / unfreeze_subscription / _validate_freeze_preconditions.
Задача 4 — ядро фичи заморозки подписки.
"""
import pytest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_user(email: str | None = 'user@example.com', email_verified: bool = True):
    user = MagicMock()
    user.email = email
    user.email_verified = email_verified
    return user


def make_subscription(
    status: str = 'active',
    is_trial: bool = False,
    days_left: int = 10,
    is_frozen: bool = False,
    is_daily_paused: bool = False,
    grace_candidate_at=None,
    remnawave_id: int = 12345,
):
    sub = MagicMock()
    sub.status = status
    sub.is_trial = is_trial
    sub.days_left = days_left
    sub.is_frozen = is_frozen
    sub.is_daily_paused = is_daily_paused
    sub.grace_candidate_at = grace_candidate_at
    sub.remnawave_id = remnawave_id
    sub.end_date = datetime(2026, 12, 31, tzinfo=UTC)
    sub.frozen_at = None
    sub.frozen_days_banked = None
    sub.frozen_auto_unfreeze_at = None
    return sub


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_settings():
    """Глобальный патч settings для всех тестов этого модуля."""
    with patch('app.services.subscription_service.settings') as mock_settings:
        mock_settings.FREEZE_SUBSCRIPTIONS_ENABLED = True
        mock_settings.FREEZE_MAX_DAYS = 60
        mock_settings.FREEZE_MIN_DAYS_REMAINING = 3
        yield mock_settings


@pytest.fixture(autouse=True)
def patch_nds():
    """Мокаем notification_delivery_service глобально."""
    with patch(
        'app.services.notification_delivery_service.notification_delivery_service'
    ) as mock_nds:
        mock_nds.notify_subscription_frozen = AsyncMock(return_value=True)
        mock_nds.notify_subscription_unfrozen = AsyncMock(return_value=True)
        yield mock_nds


# ---------------------------------------------------------------------------
# freeze_subscription — успешный сценарий
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_freeze_success():
    from app.services.subscription_service import SubscriptionService

    user = make_user()
    sub = make_subscription(days_left=10)
    db = AsyncMock()

    service = SubscriptionService()
    with patch.object(service, 'disable_remnawave_user', new_callable=AsyncMock, return_value=True):
        await service.freeze_subscription(user=user, subscription=sub, db=db)

    assert sub.is_frozen is True
    assert sub.status == 'disabled'
    assert sub.frozen_days_banked == 10
    assert sub.frozen_at is not None
    assert sub.frozen_auto_unfreeze_at is not None
    db.flush.assert_awaited()


# ---------------------------------------------------------------------------
# freeze_subscription — предусловия (должны бросать FreezeNotAllowedError)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_freeze_disabled(patch_settings):
    from app.services.subscription_service import FreezeNotAllowedError, SubscriptionService

    patch_settings.FREEZE_SUBSCRIPTIONS_ENABLED = False
    service = SubscriptionService()
    user = make_user()
    sub = make_subscription()
    db = AsyncMock()

    with pytest.raises(FreezeNotAllowedError) as exc_info:
        await service.freeze_subscription(user=user, subscription=sub, db=db)
    assert exc_info.value.reason == 'freeze_disabled'


@pytest.mark.asyncio
async def test_freeze_invalid_status():
    from app.services.subscription_service import FreezeNotAllowedError, SubscriptionService

    service = SubscriptionService()
    user = make_user()
    sub = make_subscription(status='expired')
    db = AsyncMock()

    with pytest.raises(FreezeNotAllowedError) as exc_info:
        await service.freeze_subscription(user=user, subscription=sub, db=db)
    assert exc_info.value.reason == 'invalid_status'


@pytest.mark.asyncio
async def test_freeze_trial_not_allowed():
    from app.services.subscription_service import FreezeNotAllowedError, SubscriptionService

    service = SubscriptionService()
    user = make_user()
    sub = make_subscription(status='active', is_trial=True)
    db = AsyncMock()

    with pytest.raises(FreezeNotAllowedError) as exc_info:
        await service.freeze_subscription(user=user, subscription=sub, db=db)
    assert exc_info.value.reason == 'trial_not_allowed'


@pytest.mark.asyncio
async def test_freeze_too_few_days(patch_settings):
    from app.services.subscription_service import FreezeNotAllowedError, SubscriptionService

    patch_settings.FREEZE_MIN_DAYS_REMAINING = 3
    service = SubscriptionService()
    user = make_user()
    sub = make_subscription(days_left=2)
    db = AsyncMock()

    with pytest.raises(FreezeNotAllowedError) as exc_info:
        await service.freeze_subscription(user=user, subscription=sub, db=db)
    assert exc_info.value.reason == 'too_few_days'


@pytest.mark.asyncio
async def test_freeze_already_frozen():
    from app.services.subscription_service import FreezeNotAllowedError, SubscriptionService

    service = SubscriptionService()
    user = make_user()
    sub = make_subscription(is_frozen=True)
    db = AsyncMock()

    with pytest.raises(FreezeNotAllowedError) as exc_info:
        await service.freeze_subscription(user=user, subscription=sub, db=db)
    assert exc_info.value.reason == 'already_frozen'


@pytest.mark.asyncio
async def test_freeze_daily_paused():
    from app.services.subscription_service import FreezeNotAllowedError, SubscriptionService

    service = SubscriptionService()
    user = make_user()
    sub = make_subscription(is_daily_paused=True)
    db = AsyncMock()

    with pytest.raises(FreezeNotAllowedError) as exc_info:
        await service.freeze_subscription(user=user, subscription=sub, db=db)
    assert exc_info.value.reason == 'daily_paused'


@pytest.mark.asyncio
async def test_freeze_in_grace():
    from app.services.subscription_service import FreezeNotAllowedError, SubscriptionService

    service = SubscriptionService()
    user = make_user()
    sub = make_subscription(grace_candidate_at=datetime(2026, 8, 20, tzinfo=UTC))
    db = AsyncMock()

    with pytest.raises(FreezeNotAllowedError) as exc_info:
        await service.freeze_subscription(user=user, subscription=sub, db=db)
    assert exc_info.value.reason == 'in_grace'


@pytest.mark.asyncio
async def test_freeze_email_not_verified():
    from app.services.subscription_service import FreezeNotAllowedError, SubscriptionService

    service = SubscriptionService()
    user = make_user(email_verified=False)
    sub = make_subscription()
    db = AsyncMock()

    with pytest.raises(FreezeNotAllowedError) as exc_info:
        await service.freeze_subscription(user=user, subscription=sub, db=db)
    assert exc_info.value.reason == 'email_not_verified'


@pytest.mark.asyncio
async def test_freeze_email_none():
    """Пользователь вообще без почты — тоже email_not_verified."""
    from app.services.subscription_service import FreezeNotAllowedError, SubscriptionService

    service = SubscriptionService()
    user = make_user(email=None)
    sub = make_subscription()
    db = AsyncMock()

    with pytest.raises(FreezeNotAllowedError) as exc_info:
        await service.freeze_subscription(user=user, subscription=sub, db=db)
    assert exc_info.value.reason == 'email_not_verified'


# ---------------------------------------------------------------------------
# unfreeze_subscription
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unfreeze_manual():
    from app.services.subscription_service import SubscriptionService

    service = SubscriptionService()
    user = make_user()
    frozen_at = datetime.now(UTC) - timedelta(days=5)
    sub = make_subscription(is_frozen=True)
    sub.frozen_at = frozen_at
    sub.end_date = datetime(2026, 12, 31, tzinfo=UTC)
    original_end_date = sub.end_date
    db = AsyncMock()

    with patch.object(service, 'enable_remnawave_user', new_callable=AsyncMock, return_value=True):
        await service.unfreeze_subscription(user=user, subscription=sub, db=db, reason='manual')

    assert sub.is_frozen is False
    assert sub.status == 'active'
    assert sub.frozen_at is None
    assert sub.frozen_days_banked is None
    assert sub.frozen_auto_unfreeze_at is None
    # end_date должна сдвинуться вперёд (на ~5 дней)
    assert sub.end_date > original_end_date
    db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_unfreeze_auto():
    from app.services.subscription_service import SubscriptionService

    service = SubscriptionService()
    user = make_user()
    frozen_at = datetime.now(UTC) - timedelta(days=60)
    sub = make_subscription(is_frozen=True)
    sub.frozen_at = frozen_at
    sub.end_date = datetime(2026, 12, 31, tzinfo=UTC)
    original_end_date = sub.end_date
    db = AsyncMock()

    with patch.object(service, 'enable_remnawave_user', new_callable=AsyncMock, return_value=True):
        await service.unfreeze_subscription(user=user, subscription=sub, db=db, reason='auto')

    assert sub.is_frozen is False
    assert sub.status == 'active'
    assert sub.end_date > original_end_date


@pytest.mark.asyncio
async def test_unfreeze_idempotent():
    """При is_frozen=False — ранний возврат, никаких изменений, нет ошибок."""
    from app.services.subscription_service import SubscriptionService

    service = SubscriptionService()
    user = make_user()
    sub = make_subscription(is_frozen=False)
    sub.end_date = datetime(2026, 12, 31, tzinfo=UTC)
    original_end_date = sub.end_date
    db = AsyncMock()

    with patch.object(service, 'enable_remnawave_user', new_callable=AsyncMock) as mock_enable:
        await service.unfreeze_subscription(user=user, subscription=sub, db=db)
        mock_enable.assert_not_called()

    # end_date не изменилась
    assert sub.end_date == original_end_date
    # flush не вызывался
    db.flush.assert_not_awaited()


# ---------------------------------------------------------------------------
# Задача 5 — пропуск автопродления при заморозке
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_extend_skipped_when_frozen():
    """_auto_extend_subscription возвращает False без списания при is_frozen=True."""
    from app.services.subscription_auto_purchase_service import _auto_extend_subscription

    user = make_user()
    frozen_sub = make_subscription(is_frozen=True, status='disabled')
    db = AsyncMock()

    # cart_data без subscription_id → стандартный путь get_subscription_by_user_id
    cart_data = {'period_days': '30'}

    with (
        patch(
            'app.services.subscription_auto_purchase_service.settings'
        ) as mock_settings,
        patch(
            'app.database.crud.subscription.get_subscription_by_user_id',
            new_callable=AsyncMock,
            return_value=frozen_sub,
        ),
        patch(
            'app.services.subscription_auto_purchase_service.subtract_user_balance',
            new_callable=AsyncMock,
        ) as mock_charge,
    ):
        mock_settings.is_multi_tariff_enabled.return_value = False
        mock_settings.is_tariffs_mode.return_value = False

        result = await _auto_extend_subscription(db=db, user=user, cart_data=cart_data)

    assert result is False
    mock_charge.assert_not_awaited()


@pytest.mark.asyncio
async def test_topup_extend_skipped_when_frozen():
    """try_auto_extend_expired_after_topup возвращает False при is_frozen=True."""
    from app.services.subscription_auto_purchase_service import try_auto_extend_expired_after_topup

    user = make_user()
    frozen_sub = make_subscription(is_frozen=True, status='expired')
    frozen_sub.autopay_enabled = True
    db = AsyncMock()

    with (
        patch(
            'app.services.subscription_auto_purchase_service.settings'
        ) as mock_settings,
        patch(
            'app.database.crud.subscription.get_subscription_by_user_id',
            new_callable=AsyncMock,
            return_value=frozen_sub,
        ),
        patch(
            'app.services.subscription_auto_purchase_service.subtract_user_balance',
            new_callable=AsyncMock,
        ) as mock_charge,
    ):
        mock_settings.is_multi_tariff_enabled.return_value = False

        result = await try_auto_extend_expired_after_topup(db=db, user=user)

    assert result is False
    mock_charge.assert_not_awaited()


# ---------------------------------------------------------------------------
# _check_frozen_subscriptions_for_auto_unfreeze (MonitoringService cron)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_unfreeze_cron_triggers():
    """Подписка с frozen_auto_unfreeze_at в прошлом → unfreeze_subscription вызван с reason='auto'."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.services.monitoring_service import MonitoringService

    now = datetime.now(UTC)
    sub = make_subscription(is_frozen=True)
    sub.frozen_at = now - timedelta(days=30)
    sub.frozen_auto_unfreeze_at = now - timedelta(seconds=1)  # в прошлом
    user = make_user()
    sub.user = user

    db = AsyncMock()
    mock_unfreeze = AsyncMock()

    with (
        patch(
            'app.services.monitoring_service.get_subscriptions_for_auto_unfreeze',
            new_callable=AsyncMock,
            return_value=[sub],
        ),
        patch.object(
            MonitoringService,
            '__init__',
            lambda self: None,
        ),
    ):
        service = MonitoringService.__new__(MonitoringService)
        service.subscription_service = MagicMock()
        service.subscription_service.unfreeze_subscription = mock_unfreeze

        await service._check_frozen_subscriptions_for_auto_unfreeze(db)

    mock_unfreeze.assert_awaited_once_with(
        user=user, subscription=sub, db=db, reason='auto'
    )
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_unfreeze_cron_skips_future():
    """CRUD вернул пустой список (будущая дата) → unfreeze не вызван."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.services.monitoring_service import MonitoringService

    db = AsyncMock()
    mock_unfreeze = AsyncMock()

    with (
        patch(
            'app.services.monitoring_service.get_subscriptions_for_auto_unfreeze',
            new_callable=AsyncMock,
            return_value=[],  # CRUD уже отфильтровал будущие даты
        ),
        patch.object(
            MonitoringService,
            '__init__',
            lambda self: None,
        ),
    ):
        service = MonitoringService.__new__(MonitoringService)
        service.subscription_service = MagicMock()
        service.subscription_service.unfreeze_subscription = mock_unfreeze

        await service._check_frozen_subscriptions_for_auto_unfreeze(db)

    mock_unfreeze.assert_not_awaited()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_unfreeze_cron_rollback_on_error():
    """При ошибке unfreeze_subscription → rollback; вторая подписка всё равно обрабатывается."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.services.monitoring_service import MonitoringService

    now = datetime.now(UTC)
    sub1 = make_subscription(is_frozen=True)
    sub1.frozen_at = now - timedelta(days=30)
    sub1.frozen_auto_unfreeze_at = now - timedelta(seconds=1)
    sub1.user = make_user()

    sub2 = make_subscription(is_frozen=True)
    sub2.frozen_at = now - timedelta(days=30)
    sub2.frozen_auto_unfreeze_at = now - timedelta(seconds=1)
    sub2.user = make_user()

    db = AsyncMock()
    # Первый вызов падает, второй — успешен
    mock_unfreeze = AsyncMock(side_effect=[Exception('panel error'), None])

    with (
        patch(
            'app.services.monitoring_service.get_subscriptions_for_auto_unfreeze',
            new_callable=AsyncMock,
            return_value=[sub1, sub2],
        ),
        patch.object(
            MonitoringService,
            '__init__',
            lambda self: None,
        ),
    ):
        service = MonitoringService.__new__(MonitoringService)
        service.subscription_service = MagicMock()
        service.subscription_service.unfreeze_subscription = mock_unfreeze

        await service._check_frozen_subscriptions_for_auto_unfreeze(db)

    assert mock_unfreeze.await_count == 2
    db.rollback.assert_awaited_once()
    db.commit.assert_awaited_once()
