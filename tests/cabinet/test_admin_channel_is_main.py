"""Tests for is_main enforcement: exactly one main channel."""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest


def test_set_main_route_registered():
    from app.cabinet.routes import router
    paths = {r.path: r.methods for r in router.routes if hasattr(r, 'methods')}
    assert '/cabinet/admin/channel-subscriptions/{channel_db_id}/set-main' in paths
    assert 'POST' in paths['/cabinet/admin/channel-subscriptions/{channel_db_id}/set-main']


@pytest.mark.asyncio
async def test_set_main_clears_others_and_returns_channel():
    from app.cabinet.routes import admin_channels as mod

    result_channel = types.SimpleNamespace(
        id=2,
        channel_id='-100999',
        channel_link='https://t.me/main',
        title='Main',
        is_active=True,
        is_main=True,
        sort_order=0,
        disable_trial_on_leave=True,
        disable_paid_on_leave=False,
        last_post_message_id=None,
        last_post_link=None,
        last_post_title=None,
        last_post_at=None,
    )
    db = AsyncMock()

    with (
        patch('app.cabinet.routes.admin_channels.set_main_channel', AsyncMock(return_value=result_channel)) as mock_set,
        patch.object(mod.channel_subscription_service, 'invalidate_channels_cache', AsyncMock()),
    ):
        resp = await mod.set_main_channel_endpoint(
            channel_db_id=2,
            db=db,
            _admin=types.SimpleNamespace(id=1),
        )

    mock_set.assert_awaited_once_with(db, 2)
    assert resp.is_main is True
    assert resp.id == 2


@pytest.mark.asyncio
async def test_set_main_404_when_channel_not_found():
    from fastapi import HTTPException
    from app.cabinet.routes import admin_channels as mod

    db = AsyncMock()

    with (
        patch('app.cabinet.routes.admin_channels.set_main_channel', AsyncMock(return_value=None)),
        patch.object(mod.channel_subscription_service, 'invalidate_channels_cache', AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await mod.set_main_channel_endpoint(
                channel_db_id=999,
                db=db,
                _admin=types.SimpleNamespace(id=1),
            )
    assert exc_info.value.status_code == 404
