"""Tests for the bot nudge card shown after the channel gate clears."""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


@pytest.mark.asyncio
async def test_nudge_card_sent_when_new_post_unseen(monkeypatch):
    """After gate passes and user hasn't seen latest post, the nudge card is sent."""
    from app.config import settings
    monkeypatch.setattr(settings, 'CHANNEL_SOFT_MODE', True)

    from app.middlewares.channel_checker import _send_channel_post_nudge

    bot = AsyncMock()
    user_id = 55555
    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'title': 'Тест-канал',
        'last_post_message_id': 42,
        'last_post_link': 'https://t.me/testchan/42',
        'last_post_title': 'Привет!',
    }

    db_user = types.SimpleNamespace(id=10, last_seen_channel_post_id=None)

    with patch('app.middlewares.channel_checker.update_user_last_seen_post', AsyncMock()) as mock_update:
        await _send_channel_post_nudge(bot, user_id, db_user, main_channel, db=AsyncMock())

    # Bot should have sent a message with an inline URL button
    bot.send_message.assert_awaited_once()
    call_args = bot.send_message.call_args
    assert call_args.args[0] == user_id
    assert '🆕' in call_args.args[1]
    assert 'Привет!' in call_args.args[1]
    # InlineKeyboardMarkup should contain URL button
    reply_markup = call_args.kwargs.get('reply_markup')
    if reply_markup is None and len(call_args.args) > 2:
        reply_markup = call_args.args[2]
    assert reply_markup is not None

    # last_seen should be updated
    mock_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_nudge_card_not_sent_when_already_seen(monkeypatch):
    """If user already saw this post, don't send the card again."""
    from app.middlewares.channel_checker import _send_channel_post_nudge

    bot = AsyncMock()
    db_user = types.SimpleNamespace(id=10, last_seen_channel_post_id=42)
    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'last_post_message_id': 42,
        'last_post_link': 'https://t.me/testchan/42',
        'last_post_title': 'Привет!',
    }

    await _send_channel_post_nudge(bot, 55555, db_user, main_channel, db=AsyncMock())
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_nudge_card_not_sent_when_no_post():
    """If main channel has no post yet, nudge is skipped."""
    from app.middlewares.channel_checker import _send_channel_post_nudge

    bot = AsyncMock()
    db_user = types.SimpleNamespace(id=10, last_seen_channel_post_id=None)
    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'last_post_message_id': None,
        'last_post_link': None,
        'last_post_title': None,
    }

    await _send_channel_post_nudge(bot, 55555, db_user, main_channel, db=AsyncMock())
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_nudge_card_send_failure_does_not_raise():
    """If bot.send_message raises, the nudge swallows the error and does not propagate."""
    from app.middlewares.channel_checker import _send_channel_post_nudge

    bot = AsyncMock()
    bot.send_message.side_effect = Exception('Telegram API error')
    db_user = types.SimpleNamespace(id=10, last_seen_channel_post_id=None)
    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'last_post_message_id': 7,
        'last_post_link': 'https://t.me/testchan/7',
        'last_post_title': 'Сбой!',
    }

    with patch('app.middlewares.channel_checker.update_user_last_seen_post', AsyncMock()):
        # Must not raise
        await _send_channel_post_nudge(bot, 55555, db_user, main_channel, db=AsyncMock())
