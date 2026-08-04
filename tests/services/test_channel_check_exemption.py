"""Персональное исключение из обязательной подписки на канал.

Иногда конкретному человеку проверку снимают руками. Список telegram id
хранится в настройке `CHANNEL_EXCLUDED_USER_IDS` — она живёт в system_settings,
так что правится из админки без перезапуска бота.

Исключение вкручено в `_check_user_subscriptions_for_channels` — единственную
точку, через которую ходят ВСЕ гейты (мидлварь бота, кабинет, miniapp,
support-ws, ночной обход мониторинга). Проверять его в каждом гейте по
отдельности — гарантированно забыть один из шести.
"""

import pytest

from app.config import settings
from app.services.channel_subscription_service import ChannelSubscriptionService


CHANNELS = [
    {'channel_id': '-1001', 'channel_link': 'https://t.me/a', 'title': 'A'},
    {'channel_id': '-1002', 'channel_link': 'https://t.me/b', 'title': 'B'},
]

EXEMPT_ID = 555000111
REGULAR_ID = 999888777


class TestParsing:
    def test_parses_comma_separated_ids(self, monkeypatch):
        monkeypatch.setattr(settings, 'CHANNEL_EXCLUDED_USER_IDS', '111, 222,333')

        assert settings.get_channel_excluded_user_ids() == [111, 222, 333]

    def test_empty_setting_means_nobody_is_exempt(self, monkeypatch):
        monkeypatch.setattr(settings, 'CHANNEL_EXCLUDED_USER_IDS', '')

        assert settings.get_channel_excluded_user_ids() == []
        assert settings.is_channel_check_exempt(REGULAR_ID) is False

    def test_ignores_trailing_comment_and_junk(self, monkeypatch):
        # Оператор правит значение руками — мусор не должен ронять проверку.
        monkeypatch.setattr(settings, 'CHANNEL_EXCLUDED_USER_IDS', '111, @vasya, , 222  # временно')

        assert settings.get_channel_excluded_user_ids() == [111, 222]

    def test_is_exempt_matches_only_listed_ids(self, monkeypatch):
        monkeypatch.setattr(settings, 'CHANNEL_EXCLUDED_USER_IDS', str(EXEMPT_ID))

        assert settings.is_channel_check_exempt(EXEMPT_ID) is True
        assert settings.is_channel_check_exempt(REGULAR_ID) is False
        assert settings.is_channel_check_exempt(None) is False


class TestServiceShortCircuit:
    @pytest.mark.asyncio
    async def test_exempt_user_counts_as_subscribed_everywhere(self, monkeypatch):
        monkeypatch.setattr(settings, 'CHANNEL_EXCLUDED_USER_IDS', str(EXEMPT_ID))
        service = ChannelSubscriptionService()

        result = await service._check_user_subscriptions_for_channels(EXEMPT_ID, CHANNELS)

        assert result == {'-1001': True, '-1002': True}

    @pytest.mark.asyncio
    async def test_exempt_user_never_touches_cache_db_or_telegram(self, monkeypatch):
        """Исключённый не должен стоить ни одного запроса — ни в Redis, ни в API."""
        monkeypatch.setattr(settings, 'CHANNEL_EXCLUDED_USER_IDS', str(EXEMPT_ID))

        async def explode(*args, **kwargs):
            raise AssertionError('исключённый пользователь не должен проверяться')

        monkeypatch.setattr(
            'app.services.channel_subscription_service.ChannelSubCache.get_sub_statuses', explode
        )
        service = ChannelSubscriptionService()
        monkeypatch.setattr(service, '_rate_limited_check', explode)

        assert await service._check_user_subscriptions_for_channels(EXEMPT_ID, CHANNELS)

    @pytest.mark.asyncio
    async def test_regular_user_is_still_checked(self, monkeypatch):
        """Исключение не должно снимать гейт со всех остальных."""
        monkeypatch.setattr(settings, 'CHANNEL_EXCLUDED_USER_IDS', str(EXEMPT_ID))
        checked = []

        async def fake_statuses(telegram_id, channel_ids):
            checked.append(telegram_id)
            return dict.fromkeys(channel_ids, False)

        monkeypatch.setattr(
            'app.services.channel_subscription_service.ChannelSubCache.get_sub_statuses', fake_statuses
        )

        result = await ChannelSubscriptionService()._check_user_subscriptions_for_channels(
            REGULAR_ID, CHANNELS
        )

        assert checked == [REGULAR_ID]
        assert result == {'-1001': False, '-1002': False}

    @pytest.mark.asyncio
    async def test_exempt_user_is_subscribed_to_all(self, monkeypatch):
        monkeypatch.setattr(settings, 'CHANNEL_EXCLUDED_USER_IDS', str(EXEMPT_ID))
        service = ChannelSubscriptionService()

        async def fake_channels():
            return CHANNELS

        monkeypatch.setattr(service, 'get_required_channels', fake_channels)

        assert await service.is_user_subscribed_to_all(EXEMPT_ID) is True
        assert await service.get_unsubscribed_channels(EXEMPT_ID) == []

        with_status = await service.get_channels_with_status(EXEMPT_ID)
        assert all(ch['is_subscribed'] for ch in with_status)
