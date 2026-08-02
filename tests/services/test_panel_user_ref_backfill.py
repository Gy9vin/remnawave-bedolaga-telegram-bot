"""TDD tests for get_panel_user_ref helper (T5).

Проверяет три сценария:
- v3 + user without remnawave_id but with subscription.remnawave_short_uuid
  → resolve_user_id called, user.remnawave_id persisted and committed, returns (None, id)
- v3 + user already has remnawave_id
  → resolve_user_id NOT called, returns (None, existing_id) immediately
- v2
  → returns (user.remnawave_uuid, None), no resolve call at all
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings, settings as _settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(
    *,
    telegram_id: int = 111,
    remnawave_uuid: str | None = 'aaaa-bbbb-cccc',
    remnawave_id: int | None = None,
    subscriptions: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        telegram_id=telegram_id,
        remnawave_uuid=remnawave_uuid,
        remnawave_id=remnawave_id,
        subscriptions=subscriptions or [],
    )


def _make_subscription(*, remnawave_short_uuid: str | None = 'shortXYZ') -> SimpleNamespace:
    return SimpleNamespace(remnawave_short_uuid=remnawave_short_uuid)


def _make_client(*, api_version: int = 3, resolved_id: int | None = 42) -> MagicMock:
    """Create a mock API client."""
    client = MagicMock()
    client.get_api_version = AsyncMock(return_value=api_version)
    client.resolve_user_id = AsyncMock(return_value=resolved_id)
    # get_user_by_telegram_id returns list with user that has .id
    mock_panel_user = SimpleNamespace(id=resolved_id)
    client.get_user_by_telegram_id = AsyncMock(return_value=[mock_panel_user])
    return client


def _make_db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# Import the function under test
# ---------------------------------------------------------------------------

from app.services.remnawave_service import get_panel_user_ref  # noqa: E402


# ---------------------------------------------------------------------------
# T5-1: v3 + user without remnawave_id but with subscription.remnawave_short_uuid
#        → resolve_user_id called, user.remnawave_id persisted and committed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v3_resolves_via_short_uuid_and_persists():
    """v3: нет remnawave_id у user, есть short_uuid в subscription → resolve и сохранить."""
    user = _make_user(remnawave_id=None)
    sub = _make_subscription(remnawave_short_uuid='shortXYZ')
    client = _make_client(api_version=3, resolved_id=42)
    db = _make_db()

    result = await get_panel_user_ref(client, db, user=user, subscription=sub)

    # resolve_user_id must have been called with the short_uuid
    client.resolve_user_id.assert_awaited_once_with(short_uuid='shortXYZ')
    # remnawave_id must be persisted
    assert user.remnawave_id == 42
    # db.commit must have been called
    db.commit.assert_awaited_once()
    # returns (None, id)
    assert result == (None, 42)


# ---------------------------------------------------------------------------
# T5-2: v3 + user already has remnawave_id → NO resolve, returns immediately
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v3_already_has_remna_id_no_resolve():
    """v3: remnawave_id уже есть → resolve_user_id не вызывается."""
    user = _make_user(remnawave_id=99)
    sub = _make_subscription(remnawave_short_uuid='shortXYZ')
    client = _make_client(api_version=3, resolved_id=42)
    db = _make_db()

    result = await get_panel_user_ref(client, db, user=user, subscription=sub)

    # resolve_user_id must NOT be called
    client.resolve_user_id.assert_not_awaited()
    # no commit needed
    db.commit.assert_not_awaited()
    # returns (None, existing_id) immediately
    assert result == (None, 99)


# ---------------------------------------------------------------------------
# T5-3: v2 → returns (user.remnawave_uuid, None), no resolve call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v2_returns_uuid_no_resolve():
    """v2: возвращает (user.remnawave_uuid, None), resolve не вызывается."""
    user = _make_user(remnawave_uuid='aaaa-bbbb-cccc', remnawave_id=None)
    sub = _make_subscription(remnawave_short_uuid='shortXYZ')
    client = _make_client(api_version=2)
    db = _make_db()

    result = await get_panel_user_ref(client, db, user=user, subscription=sub)

    # resolve_user_id must NOT be called
    client.resolve_user_id.assert_not_awaited()
    # no commit
    db.commit.assert_not_awaited()
    # returns (uuid, None)
    assert result == ('aaaa-bbbb-cccc', None)


# ---------------------------------------------------------------------------
# T5-4: v3 + no subscription short_uuid, but user has subscriptions with one
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v3_resolves_via_user_subscription_short_uuid():
    """v3: нет прямого sub short_uuid, берём из user.subscriptions[0]."""
    inner_sub = SimpleNamespace(remnawave_short_uuid='fromUserSub')
    user = _make_user(remnawave_id=None, subscriptions=[inner_sub])
    # No subscription kwarg
    client = _make_client(api_version=3, resolved_id=77)
    db = _make_db()

    result = await get_panel_user_ref(client, db, user=user)

    client.resolve_user_id.assert_awaited_once_with(short_uuid='fromUserSub')
    assert user.remnawave_id == 77
    db.commit.assert_awaited_once()
    assert result == (None, 77)


# ---------------------------------------------------------------------------
# T5-5: v3 + no short_uuid anywhere → fallback to get_user_by_telegram_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v3_fallback_to_telegram_id_lookup():
    """v3: нет short_uuid нигде → fallback на get_user_by_telegram_id."""
    user = _make_user(remnawave_id=None, subscriptions=[])
    # No subscription kwarg
    client = _make_client(api_version=3, resolved_id=55)
    client.resolve_user_id = AsyncMock(return_value=None)  # no short_uuid path
    db = _make_db()

    result = await get_panel_user_ref(client, db, user=user)

    # resolve_user_id not called (no short_uuid), fallback to tg lookup
    client.resolve_user_id.assert_not_awaited()
    client.get_user_by_telegram_id.assert_awaited_once_with(user.telegram_id)
    assert user.remnawave_id == 55
    db.commit.assert_awaited_once()
    assert result == (None, 55)


# ---------------------------------------------------------------------------
# T5-6: v3 + resolve returns None → fallback to telegram lookup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v3_resolve_none_fallback_telegram():
    """v3: resolve вернул None → fallback на get_user_by_telegram_id."""
    user = _make_user(remnawave_id=None, subscriptions=[])
    sub = _make_subscription(remnawave_short_uuid='badShortUuid')
    client = _make_client(api_version=3, resolved_id=33)
    client.resolve_user_id = AsyncMock(return_value=None)  # simulate no result
    db = _make_db()

    result = await get_panel_user_ref(client, db, user=user, subscription=sub)

    # resolve_user_id was called first
    client.resolve_user_id.assert_awaited_once_with(short_uuid='badShortUuid')
    # then fallback
    client.get_user_by_telegram_id.assert_awaited_once()
    assert user.remnawave_id == 33
    assert result == (None, 33)


# ---------------------------------------------------------------------------
# T5-7: v2 single-tariff with subscription kwarg — still returns user uuid, not sub uuid
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v2_with_subscription_still_returns_uuid():
    """v2 single-tariff: даже с subscription возвращает user.remnawave_uuid, не id."""
    with patch.object(Settings, 'is_multi_tariff_enabled', return_value=False):
        user = _make_user(remnawave_uuid='v2-uuid-xxxx', remnawave_id=None)
        sub = SimpleNamespace(remnawave_short_uuid='someShort', remnawave_uuid='sub-uuid-yyyy')
        client = _make_client(api_version=2)
        db = _make_db()

        result = await get_panel_user_ref(client, db, user=user, subscription=sub)

    assert result == ('v2-uuid-xxxx', None)
    client.resolve_user_id.assert_not_awaited()


# ---------------------------------------------------------------------------
# T5-8: v2 multi-tariff + subscription → returns subscription.remnawave_uuid
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v2_multi_tariff_returns_subscription_uuid():
    """v2 multi-tariff: subscription = отдельный панель-юзер → берём subscription.remnawave_uuid."""
    with patch.object(Settings, 'is_multi_tariff_enabled', return_value=True):
        user = _make_user(remnawave_uuid='user-uuid-aaaa', remnawave_id=None)
        sub = SimpleNamespace(remnawave_short_uuid='someShort', remnawave_uuid='sub-uuid-bbbb')
        client = _make_client(api_version=2)
        db = _make_db()

        result = await get_panel_user_ref(client, db, user=user, subscription=sub)

    # В multi-tariff v2 должен вернуть sub uuid, а не user uuid
    assert result == ('sub-uuid-bbbb', None)
    client.resolve_user_id.assert_not_awaited()
    db.commit.assert_not_awaited()
