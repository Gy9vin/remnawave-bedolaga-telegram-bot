"""Tests for channel_post handler: captures new main-channel posts."""
from __future__ import annotations

import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_channel_post_updates_last_post_for_main_channel():
    """A channel_post event for the main channel updates last_post_* fields."""
    from app.handlers.channel_post import on_channel_post

    main_channel_id = '-100999888'
    main_channel_db_id = 7

    # Fake main channel dict (as returned from cache)
    main_channel_dict = {
        'id': main_channel_db_id,
        'channel_id': main_channel_id,
        'is_main': True,
        'title': 'Главный канал',
        'channel_link': 'https://t.me/mainchan',
    }

    # Fake aiogram Message with text
    message = types.SimpleNamespace(
        message_id=42,
        chat=types.SimpleNamespace(id=int(main_channel_id), username='mainchan'),
        text='Привет! Это свежий пост.',
        caption=None,
        date=datetime(2026, 7, 31, 10, 0, 0, tzinfo=UTC),
    )

    with (
        patch(
            'app.handlers.channel_post.channel_subscription_service.get_main_channel',
            AsyncMock(return_value=main_channel_dict),
        ),
        patch('app.handlers.channel_post.update_channel_post', AsyncMock()) as mock_update,
        patch('app.handlers.channel_post.AsyncSessionLocal') as mock_session,
    ):
        mock_db = AsyncMock()
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=None)

        await on_channel_post(message)

    mock_update.assert_awaited_once()
    call_kwargs = mock_update.call_args
    assert call_kwargs.kwargs['message_id'] == 42
    assert 'mainchan' in call_kwargs.kwargs['link']
    assert '42' in call_kwargs.kwargs['link']
    assert call_kwargs.kwargs['title'] == 'Привет! Это свежий пост.'[:120]


@pytest.mark.asyncio
async def test_channel_post_ignores_non_main_channel():
    """A channel_post event for a non-main channel is silently ignored."""
    from app.handlers.channel_post import on_channel_post

    message = types.SimpleNamespace(
        message_id=99,
        chat=types.SimpleNamespace(id=-100111222, username='otherchan'),
        text='Пост из другого канала',
        caption=None,
        date=datetime(2026, 7, 31, tzinfo=UTC),
    )

    with (
        patch(
            'app.handlers.channel_post.channel_subscription_service.get_main_channel',
            AsyncMock(return_value={'id': 7, 'channel_id': '-100999888', 'is_main': True}),
        ),
        patch('app.handlers.channel_post.update_channel_post', AsyncMock()) as mock_update,
    ):
        await on_channel_post(message)

    mock_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_post_graceful_when_no_main_channel():
    """No main channel configured → no crash, update not called."""
    from app.handlers.channel_post import on_channel_post

    message = types.SimpleNamespace(
        message_id=5,
        chat=types.SimpleNamespace(id=-100000001, username='chan'),
        text='Пост',
        caption=None,
        date=datetime(2026, 7, 31, tzinfo=UTC),
    )

    with (
        patch(
            'app.handlers.channel_post.channel_subscription_service.get_main_channel',
            AsyncMock(return_value=None),
        ),
        patch('app.handlers.channel_post.update_channel_post', AsyncMock()) as mock_update,
    ):
        await on_channel_post(message)

    mock_update.assert_not_awaited()
