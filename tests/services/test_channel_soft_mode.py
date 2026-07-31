"""Tests for CHANNEL_SOFT_MODE soft-mode flag.

In soft mode:
- should_disable_subscription always returns False (no VPN kill)
- _deactivate_subscription_on_unsubscribe is a no-op
- channel leave event does not deactivate subscriptions
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.channel_subscription_service import ChannelSubscriptionService


def test_should_disable_returns_false_in_soft_mode(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'CHANNEL_SOFT_MODE', True)
    svc = ChannelSubscriptionService()
    # Even with per-channel flags requesting deactivation, soft mode wins
    channel_trial = {'disable_trial_on_leave': True, 'disable_paid_on_leave': True}
    assert ChannelSubscriptionService.should_disable_subscription(channel_trial, is_trial=True) is False
    assert ChannelSubscriptionService.should_disable_subscription(channel_trial, is_trial=False) is False


def test_should_disable_respects_per_channel_when_soft_mode_off(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'CHANNEL_SOFT_MODE', False)
    monkeypatch.setattr(settings, 'CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE', True)
    channel = {'disable_trial_on_leave': True, 'disable_paid_on_leave': False}
    assert ChannelSubscriptionService.should_disable_subscription(channel, is_trial=True) is True
    assert ChannelSubscriptionService.should_disable_subscription(channel, is_trial=False) is False


@pytest.mark.asyncio
async def test_deactivate_is_noop_in_soft_mode(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'CHANNEL_SOFT_MODE', True)

    from app.middlewares.channel_checker import ChannelCheckerMiddleware
    middleware = ChannelCheckerMiddleware()
    bot = AsyncMock()
    channels = [{'channel_id': '-100111', 'is_subscribed': False, 'disable_paid_on_leave': True}]

    # No DB calls should be made in soft mode
    with patch('app.middlewares.channel_checker.AsyncSessionLocal') as mock_session:
        await middleware._deactivate_subscription_on_unsubscribe(12345, bot, channels)
        mock_session.assert_not_called()


@pytest.mark.asyncio
async def test_channel_leave_does_not_deactivate_in_soft_mode(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'CHANNEL_SOFT_MODE', True)

    from app.handlers import channel_member as mod
    import types

    # Fake ChatMemberUpdated event
    user_ns = types.SimpleNamespace(id=99999)
    event = types.SimpleNamespace(
        old_chat_member=types.SimpleNamespace(user=user_ns),
        chat=types.SimpleNamespace(id=-100111),
    )
    bot = AsyncMock()

    with (
        patch.object(mod.channel_subscription_service, 'get_required_channel_ids', AsyncMock(return_value={'-100111'})),
        patch.object(mod.channel_subscription_service, 'on_user_left', AsyncMock()),
        patch('app.handlers.channel_member.AsyncSessionLocal') as mock_session,
    ):
        await mod.on_user_left_channel(event, bot)
        mock_session.assert_not_called()
