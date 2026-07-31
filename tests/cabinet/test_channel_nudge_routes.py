# tests/cabinet/test_channel_nudge_routes.py
"""Tests for /cabinet/channel-nudge endpoints."""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest


def test_channel_nudge_routes_registered():
    from app.cabinet.routes import router
    paths = {r.path: r.methods for r in router.routes if hasattr(r, 'methods')}
    assert '/cabinet/channel-nudge' in paths
    assert 'GET' in paths['/cabinet/channel-nudge']
    assert '/cabinet/channel-nudge/seen' in paths
    assert 'POST' in paths['/cabinet/channel-nudge/seen']


@pytest.mark.asyncio
async def test_nudge_subscribed_user_no_post():
    """Telegram user subscribed to main channel, no latest post → needs_subscribe=False, show_post=False."""
    from app.cabinet.routes import channel_nudge as mod

    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'title': 'Тест',
        'channel_link': 'https://t.me/testchan',
        'last_post_message_id': None,
        'last_post_link': None,
        'last_post_title': None,
    }
    db_user = types.SimpleNamespace(
        id=10,
        telegram_id=12345,
        last_seen_channel_post_id=None,
    )
    db = AsyncMock()

    with (
        patch.object(mod.channel_subscription_service, 'get_main_channel', AsyncMock(return_value=main_channel)),
        patch.object(
            mod.channel_subscription_service,
            'check_user_subscriptions',
            AsyncMock(return_value={'-100111': True}),
        ),
    ):
        resp = await mod.get_channel_nudge(current_user=db_user, db=db)

    assert resp.needs_subscribe is False
    assert resp.show_post is False
    assert resp.latest_post is None


@pytest.mark.asyncio
async def test_nudge_email_only_user_always_needs_subscribe():
    """Email-only user (no telegram_id) → needs_subscribe=True, no API call."""
    from app.cabinet.routes import channel_nudge as mod

    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'title': 'Тест',
        'channel_link': 'https://t.me/testchan',
        'last_post_message_id': 77,
        'last_post_link': 'https://t.me/testchan/77',
        'last_post_title': 'Hello',
    }
    db_user = types.SimpleNamespace(
        id=20,
        telegram_id=None,  # email-only
        last_seen_channel_post_id=None,
    )
    db = AsyncMock()

    with patch.object(mod.channel_subscription_service, 'get_main_channel', AsyncMock(return_value=main_channel)):
        resp = await mod.get_channel_nudge(current_user=db_user, db=db)

    assert resp.needs_subscribe is True
    assert resp.show_post is True
    assert resp.latest_post is not None
    assert resp.latest_post.id == 77


@pytest.mark.asyncio
async def test_nudge_show_post_false_when_already_seen():
    """User has seen the latest post → show_post=False even if needs_subscribe."""
    from app.cabinet.routes import channel_nudge as mod

    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'title': 'Тест',
        'channel_link': 'https://t.me/testchan',
        'last_post_message_id': 77,
        'last_post_link': 'https://t.me/testchan/77',
        'last_post_title': 'Hello',
    }
    db_user = types.SimpleNamespace(
        id=20,
        telegram_id=None,
        last_seen_channel_post_id=77,  # already seen
    )
    db = AsyncMock()

    with patch.object(mod.channel_subscription_service, 'get_main_channel', AsyncMock(return_value=main_channel)):
        resp = await mod.get_channel_nudge(current_user=db_user, db=db)

    assert resp.show_post is False


@pytest.mark.asyncio
async def test_nudge_seen_endpoint_updates_user():
    """POST /seen updates last_seen_channel_post_id."""
    from app.cabinet.routes import channel_nudge as mod

    db_user = types.SimpleNamespace(id=30, telegram_id=99999, last_seen_channel_post_id=None)
    db = AsyncMock()

    with patch('app.cabinet.routes.channel_nudge.update_user_last_seen_post', AsyncMock()) as mock_update:
        resp = await mod.mark_channel_nudge_seen(
            body=mod.MarkSeenRequest(post_id=42),
            current_user=db_user,
            db=db,
        )

    mock_update.assert_awaited_once_with(db, db_user.id, 42)
    assert resp == {'ok': True}


@pytest.mark.asyncio
async def test_nudge_no_500_on_telegram_error():
    """If Telegram membership check raises, endpoint returns needs_subscribe=True, no 500."""
    from app.cabinet.routes import channel_nudge as mod

    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'title': 'Тест',
        'channel_link': 'https://t.me/testchan',
        'last_post_message_id': None,
        'last_post_link': None,
        'last_post_title': None,
    }
    db_user = types.SimpleNamespace(id=10, telegram_id=12345, last_seen_channel_post_id=None)
    db = AsyncMock()

    with (
        patch.object(mod.channel_subscription_service, 'get_main_channel', AsyncMock(return_value=main_channel)),
        patch.object(
            mod.channel_subscription_service,
            'check_user_subscriptions',
            AsyncMock(side_effect=Exception('Telegram API down')),
        ),
    ):
        resp = await mod.get_channel_nudge(current_user=db_user, db=db)

    assert resp.needs_subscribe is True  # degraded gracefully
