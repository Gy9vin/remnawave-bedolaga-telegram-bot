"""Tests for POST /cabinet/subscription/freeze and /cabinet/subscription/unfreeze endpoints.

Pattern: call handler directly with mock db and mock service — same as
tests/cabinet/test_admin_merge_keep_subscription_route.py.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.webapi.routes.miniapp import freeze_subscription_endpoint, unfreeze_subscription_endpoint
from app.webapi.schemas.miniapp import MiniAppSubscriptionFreezeRequest


UTC = timezone.utc

_FUTURE = datetime(2027, 1, 1, tzinfo=UTC)
_END_DATE = datetime(2026, 12, 31, tzinfo=UTC)


def _make_sub(is_frozen=False, status='active'):
    return SimpleNamespace(
        id=1,
        status=status,
        is_frozen=is_frozen,
        frozen_days_banked=0 if not is_frozen else 15,
        frozen_auto_unfreeze_at=_FUTURE if is_frozen else None,
        end_date=_END_DATE,
    )


def _make_user(sub=None):
    subs = [sub] if sub else []
    return SimpleNamespace(id=42, subscriptions=subs, balance_kopeks=0)


def _fake_db():
    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _freeze_payload():
    return MiniAppSubscriptionFreezeRequest(init_data='test_init_data')


# ---------------------------------------------------------------------------
# freeze endpoint — success
# ---------------------------------------------------------------------------

async def test_subscription_data_freeze_fields_present():
    """SubscriptionData содержит freeze_subscriptions_enabled из settings."""
    from unittest.mock import patch as mock_patch
    from app.cabinet.schemas.subscription import SubscriptionData
    from datetime import date

    data = SubscriptionData(
        id=1,
        status='active',
        is_trial=False,
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2027, 1, 1, tzinfo=UTC),
        days_left=365,
        hours_left=0,
        minutes_left=0,
        time_left_display='365d',
        traffic_limit_gb=100,
        traffic_used_gb=0.0,
        traffic_used_percent=0.0,
        device_limit=3,
        connected_squads=[],
        servers=[],
        autopay_enabled=False,
        autopay_days_before=3,
        is_active=True,
        is_expired=False,
        freeze_subscriptions_enabled=True,
    )
    assert data.freeze_subscriptions_enabled is True
    assert data.is_frozen is False
    assert data.frozen_days_banked is None
    assert data.frozen_auto_unfreeze_at is None


async def test_freeze_endpoint_200():
    """Успешная заморозка: status 200, is_frozen=True, frozen_days_banked возвращён."""
    sub = _make_sub(is_frozen=False)
    user = _make_user(sub)
    db = _fake_db()

    # После вызова freeze_subscription сервис выставляет is_frozen=True
    async def fake_freeze(user, subscription, db):
        subscription.is_frozen = True
        subscription.frozen_days_banked = 15
        subscription.frozen_auto_unfreeze_at = _FUTURE

    with patch(
        'app.webapi.routes.miniapp._authorize_miniapp_user',
        new_callable=AsyncMock,
        return_value=user,
    ), patch(
        'app.services.subscription_service.SubscriptionService.freeze_subscription',
        side_effect=fake_freeze,
    ):
        response = await freeze_subscription_endpoint(payload=_freeze_payload(), db=db)

    assert response.success is True
    assert response.is_frozen is True
    assert response.frozen_days_banked == 15
    assert response.frozen_auto_unfreeze_at == _FUTURE
    assert response.new_end_date == _END_DATE
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# freeze endpoint — 422 (already frozen)
# ---------------------------------------------------------------------------

async def test_freeze_endpoint_422_already_frozen():
    """Повторная заморозка (status='active', is_frozen=True): FreezeNotAllowedError → HTTP 422."""
    from app.services.subscription_service import FreezeNotAllowedError

    sub = _make_sub(is_frozen=True)
    user = _make_user(sub)
    db = _fake_db()

    async def raise_already_frozen(user, subscription, db):
        raise FreezeNotAllowedError('already_frozen')

    with patch(
        'app.webapi.routes.miniapp._authorize_miniapp_user',
        new_callable=AsyncMock,
        return_value=user,
    ), patch(
        'app.services.subscription_service.SubscriptionService.freeze_subscription',
        side_effect=raise_already_frozen,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await freeze_subscription_endpoint(payload=_freeze_payload(), db=db)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail['error_code'] == 'already_frozen'


async def test_freeze_endpoint_422_already_frozen_disabled_status():
    """Повторный freeze уже замороженной (status='disabled') подписки → 422 already_frozen.

    Реалистичный сценарий: после freeze сервис выставляет status='disabled'.
    Раньше эндпоинт возвращал 404, так как фильтровал только active/trial.
    """
    from app.services.subscription_service import FreezeNotAllowedError

    # Реалистичное состояние замороженной подписки: status='disabled', is_frozen=True
    sub = _make_sub(is_frozen=True, status='disabled')
    user = _make_user(sub)
    db = _fake_db()

    async def raise_already_frozen(user, subscription, db):
        raise FreezeNotAllowedError('already_frozen')

    with patch(
        'app.webapi.routes.miniapp._authorize_miniapp_user',
        new_callable=AsyncMock,
        return_value=user,
    ), patch(
        'app.services.subscription_service.SubscriptionService.freeze_subscription',
        side_effect=raise_already_frozen,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await freeze_subscription_endpoint(payload=_freeze_payload(), db=db)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail['error_code'] == 'already_frozen'


# ---------------------------------------------------------------------------
# freeze endpoint — 422 (freeze disabled)
# ---------------------------------------------------------------------------

async def test_freeze_endpoint_422_freeze_disabled():
    """FreezeNotAllowedError с reason='freeze_disabled' → HTTP 422."""
    from app.services.subscription_service import FreezeNotAllowedError

    sub = _make_sub(is_frozen=False)
    user = _make_user(sub)
    db = _fake_db()

    async def raise_disabled(user, subscription, db):
        raise FreezeNotAllowedError('freeze_disabled')

    with patch(
        'app.webapi.routes.miniapp._authorize_miniapp_user',
        new_callable=AsyncMock,
        return_value=user,
    ), patch(
        'app.services.subscription_service.SubscriptionService.freeze_subscription',
        side_effect=raise_disabled,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await freeze_subscription_endpoint(payload=_freeze_payload(), db=db)

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail['error_code'] == 'freeze_disabled'


# ---------------------------------------------------------------------------
# freeze endpoint — 404 (no subscription)
# ---------------------------------------------------------------------------

async def test_freeze_endpoint_404_no_subscription():
    """Пользователь без активной подписки → HTTP 404."""
    user = _make_user(sub=None)
    db = _fake_db()

    with patch(
        'app.webapi.routes.miniapp._authorize_miniapp_user',
        new_callable=AsyncMock,
        return_value=user,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await freeze_subscription_endpoint(payload=_freeze_payload(), db=db)

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# unfreeze endpoint — success
# ---------------------------------------------------------------------------

async def test_unfreeze_endpoint_200():
    """Успешная разморозка: status 200, is_frozen=False, new_end_date сдвинут."""
    sub = _make_sub(is_frozen=True)
    user = _make_user(sub)
    db = _fake_db()

    _SHIFTED = datetime(2027, 2, 14, tzinfo=UTC)

    async def fake_unfreeze(user, subscription, db, reason='manual'):
        subscription.is_frozen = False
        subscription.frozen_days_banked = 0
        subscription.frozen_auto_unfreeze_at = None
        subscription.end_date = _SHIFTED

    with patch(
        'app.webapi.routes.miniapp._authorize_miniapp_user',
        new_callable=AsyncMock,
        return_value=user,
    ), patch(
        'app.services.subscription_service.SubscriptionService.unfreeze_subscription',
        side_effect=fake_unfreeze,
    ):
        response = await unfreeze_subscription_endpoint(payload=_freeze_payload(), db=db)

    assert response.success is True
    assert response.is_frozen is False
    assert response.new_end_date == _SHIFTED
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# unfreeze endpoint — 404 (no frozen subscription)
# ---------------------------------------------------------------------------

async def test_unfreeze_endpoint_404_not_frozen():
    """Нет замороженной подписки → HTTP 404."""
    sub = _make_sub(is_frozen=False)
    user = _make_user(sub)
    db = _fake_db()

    with patch(
        'app.webapi.routes.miniapp._authorize_miniapp_user',
        new_callable=AsyncMock,
        return_value=user,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await unfreeze_subscription_endpoint(payload=_freeze_payload(), db=db)

    assert exc_info.value.status_code == 404
