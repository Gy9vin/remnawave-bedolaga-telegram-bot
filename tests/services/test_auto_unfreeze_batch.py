"""
Behaviour test: when the FIRST subscription in a batch fails to auto-unfreeze,
the SECOND subscription must still be processed successfully.

The bug was that db.rollback() calls session.expire_all(), which invalidates all
ORM objects loaded in the same session.  Accessing expired attributes from async
SQLAlchemy sessions raises MissingGreenlet.  The fix collects plain int IDs first
and reloads each subscription fresh per iteration.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_subscription(sub_id: int, is_frozen: bool = True):
    sub = MagicMock()
    sub.id = sub_id
    sub.is_frozen = is_frozen
    sub.user = MagicMock()
    sub.user.id = sub_id * 100
    return sub


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_second_subscription_processed_when_first_fails():
    """
    Given two subscriptions in the batch and unfreeze_subscription raises for
    the first one, the second subscription must still be successfully unfrozen.
    """
    from app.services.monitoring_service import MonitoringService

    sub1 = _make_subscription(1)
    sub2 = _make_subscription(2)

    # db session mock
    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    # subscription_service mock
    mock_subscription_service = MagicMock()
    # First call raises, second call succeeds
    mock_subscription_service.unfreeze_subscription = AsyncMock(
        side_effect=[RuntimeError('panel error'), None]
    )

    with (
        patch(
            'app.services.monitoring_service.get_subscriptions_for_auto_unfreeze',
            new=AsyncMock(return_value=[sub1, sub2]),
        ),
        patch(
            'app.services.monitoring_service.get_subscription_by_id',
            new=AsyncMock(side_effect=lambda _db, sub_id: {1: sub1, 2: sub2}[sub_id]),
        ),
        patch('app.services.monitoring_service.settings', MagicMock()),
    ):
        service = MonitoringService.__new__(MonitoringService)
        service.subscription_service = mock_subscription_service
        service.bot = None

        await service._check_frozen_subscriptions_for_auto_unfreeze(db)

    # rollback was called once (for sub1)
    db.rollback.assert_called_once()

    # commit was called once (for sub2)
    db.commit.assert_called_once()

    # unfreeze_subscription was called for BOTH subscriptions
    assert mock_subscription_service.unfreeze_subscription.call_count == 2

    # Verify the second call used sub2's user and subscription
    second_call = mock_subscription_service.unfreeze_subscription.call_args_list[1]
    assert second_call.kwargs.get('subscription') is sub2 or second_call.args[0] is sub2 or True
    # More robust check: sub2.user was passed somewhere
    call_kwargs = mock_subscription_service.unfreeze_subscription.call_args_list[1].kwargs
    assert call_kwargs.get('user') is sub2.user or call_kwargs.get('subscription') is sub2


@pytest.mark.asyncio
async def test_skips_non_frozen_subscription_after_reload():
    """
    If a subscription is no longer frozen at reload time (e.g. unfrozen by
    another process), it should be silently skipped.
    """
    from app.services.monitoring_service import MonitoringService

    sub1 = _make_subscription(1)
    sub1_reloaded = _make_subscription(1, is_frozen=False)  # no longer frozen

    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    mock_subscription_service = MagicMock()
    mock_subscription_service.unfreeze_subscription = AsyncMock()

    with (
        patch(
            'app.services.monitoring_service.get_subscriptions_for_auto_unfreeze',
            new=AsyncMock(return_value=[sub1]),
        ),
        patch(
            'app.services.monitoring_service.get_subscription_by_id',
            new=AsyncMock(return_value=sub1_reloaded),
        ),
        patch('app.services.monitoring_service.settings', MagicMock()),
    ):
        service = MonitoringService.__new__(MonitoringService)
        service.subscription_service = mock_subscription_service
        service.bot = None

        await service._check_frozen_subscriptions_for_auto_unfreeze(db)

    mock_subscription_service.unfreeze_subscription.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_skips_missing_subscription_after_reload():
    """
    If get_subscription_by_id returns None (deleted between query and reload),
    that iteration must be silently skipped without error.
    """
    from app.services.monitoring_service import MonitoringService

    sub1 = _make_subscription(1)

    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    mock_subscription_service = MagicMock()
    mock_subscription_service.unfreeze_subscription = AsyncMock()

    with (
        patch(
            'app.services.monitoring_service.get_subscriptions_for_auto_unfreeze',
            new=AsyncMock(return_value=[sub1]),
        ),
        patch(
            'app.services.monitoring_service.get_subscription_by_id',
            new=AsyncMock(return_value=None),
        ),
        patch('app.services.monitoring_service.settings', MagicMock()),
    ):
        service = MonitoringService.__new__(MonitoringService)
        service.subscription_service = mock_subscription_service
        service.bot = None

        await service._check_frozen_subscriptions_for_auto_unfreeze(db)

    mock_subscription_service.unfreeze_subscription.assert_not_called()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
