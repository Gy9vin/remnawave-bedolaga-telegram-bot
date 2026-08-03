"""TDD tests for get_panel_user_ref helper (T5).

3.0.0 полностью выпилил панельный uuid — идентичность пользователя чисто
числовая (``remnawave_id``), поэтому v2/uuid-ветка удалена вместе с ней
(RemnaWaveAPI.resolve_user_id/get_user_by_telegram_id тоже не существуют в
3.0.0 — актуальные имена ``resolve_user``/``find_users_by_telegram_id``).

Проверяет сценарии:
- user without remnawave_id but with subscription.remnawave_short_uuid
  → resolve_user called, user.remnawave_id persisted and committed, returns (None, id)
- user already has remnawave_id
  → resolve_user NOT called, returns (None, existing_id) immediately
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(
    *,
    telegram_id: int = 111,
    remnawave_id: int | None = None,
    subscriptions: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        telegram_id=telegram_id,
        remnawave_id=remnawave_id,
        subscriptions=subscriptions or [],
    )


def _make_subscription(*, remnawave_short_uuid: str | None = 'shortXYZ') -> SimpleNamespace:
    return SimpleNamespace(remnawave_short_uuid=remnawave_short_uuid)


def _make_client(*, resolved_id: int | None = 42) -> MagicMock:
    """Create a mock API client."""
    client = MagicMock()
    client.resolve_user = AsyncMock(
        return_value=({'id': resolved_id} if resolved_id is not None else None)
    )
    # find_users_by_telegram_id returns list with user that has .id
    mock_panel_user = SimpleNamespace(id=resolved_id)
    client.find_users_by_telegram_id = AsyncMock(return_value=[mock_panel_user])
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
# T5-1: user without remnawave_id but with subscription.remnawave_short_uuid
#        → resolve_user called, user.remnawave_id persisted and committed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolves_via_short_uuid_and_persists():
    """Нет remnawave_id у user, есть short_uuid в subscription → resolve и сохранить."""
    user = _make_user(remnawave_id=None)
    sub = _make_subscription(remnawave_short_uuid='shortXYZ')
    client = _make_client(resolved_id=42)
    db = _make_db()

    result = await get_panel_user_ref(client, db, user=user, subscription=sub)

    # resolve_user must have been called with the short_uuid
    client.resolve_user.assert_awaited_once_with(short_uuid='shortXYZ')
    # remnawave_id must be persisted
    assert user.remnawave_id == 42
    # db.commit must have been called
    db.commit.assert_awaited_once()
    # returns (None, id)
    assert result == (None, 42)


# ---------------------------------------------------------------------------
# T5-2: user already has remnawave_id → NO resolve, returns immediately
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_already_has_remna_id_no_resolve():
    """remnawave_id уже есть → resolve_user не вызывается."""
    user = _make_user(remnawave_id=99)
    sub = _make_subscription(remnawave_short_uuid='shortXYZ')
    client = _make_client(resolved_id=42)
    db = _make_db()

    result = await get_panel_user_ref(client, db, user=user, subscription=sub)

    # resolve_user must NOT be called
    client.resolve_user.assert_not_awaited()
    # no commit needed
    db.commit.assert_not_awaited()
    # returns (None, existing_id) immediately
    assert result == (None, 99)


# ---------------------------------------------------------------------------
# T5-4: no subscription short_uuid, but user has subscriptions with one
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolves_via_user_subscription_short_uuid():
    """Нет прямого sub short_uuid, берём из user.subscriptions[0]."""
    inner_sub = SimpleNamespace(remnawave_short_uuid='fromUserSub')
    user = _make_user(remnawave_id=None, subscriptions=[inner_sub])
    # No subscription kwarg
    client = _make_client(resolved_id=77)
    db = _make_db()

    result = await get_panel_user_ref(client, db, user=user)

    client.resolve_user.assert_awaited_once_with(short_uuid='fromUserSub')
    assert user.remnawave_id == 77
    db.commit.assert_awaited_once()
    assert result == (None, 77)


# ---------------------------------------------------------------------------
# T5-5: no short_uuid anywhere → fallback to find_users_by_telegram_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fallback_to_telegram_id_lookup():
    """Нет short_uuid нигде → fallback на find_users_by_telegram_id."""
    user = _make_user(remnawave_id=None, subscriptions=[])
    # No subscription kwarg
    client = _make_client(resolved_id=55)
    db = _make_db()

    result = await get_panel_user_ref(client, db, user=user)

    # resolve_user not called (no short_uuid), fallback to tg lookup
    client.resolve_user.assert_not_awaited()
    client.find_users_by_telegram_id.assert_awaited_once_with(user.telegram_id)
    assert user.remnawave_id == 55
    db.commit.assert_awaited_once()
    assert result == (None, 55)


# ---------------------------------------------------------------------------
# T5-6: resolve returns None → fallback to telegram lookup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_none_fallback_telegram():
    """resolve_user вернул None → fallback на find_users_by_telegram_id."""
    user = _make_user(remnawave_id=None, subscriptions=[])
    sub = _make_subscription(remnawave_short_uuid='badShortUuid')
    client = _make_client(resolved_id=33)
    client.resolve_user = AsyncMock(return_value=None)  # simulate no result
    db = _make_db()

    result = await get_panel_user_ref(client, db, user=user, subscription=sub)

    # resolve_user was called first
    client.resolve_user.assert_awaited_once_with(short_uuid='badShortUuid')
    # then fallback
    client.find_users_by_telegram_id.assert_awaited_once()
    assert user.remnawave_id == 33
    assert result == (None, 33)
