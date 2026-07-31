"""Тесты: бот отправляет nudge после оплаты подписки."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _user_single(telegram_id=111222333):
    return SimpleNamespace(
        id=1, telegram_id=telegram_id,
        email=None, password_hash=None,
        yandex_id=None, google_id=None, discord_id=None, vk_id=None,
    )


def _user_two(telegram_id=111222333):
    return SimpleNamespace(
        id=1, telegram_id=telegram_id,
        email='u@example.com', password_hash='hash',
        yandex_id=None, google_id=None, discord_id=None, vk_id=None,
    )


@pytest.mark.asyncio
async def test_nudge_sent_when_single_method():
    """Один метод входа → send_message вызывается."""
    from app.handlers.subscription.backup_login_nudge import send_backup_login_nudge

    bot = AsyncMock()
    bot.send_message = AsyncMock()

    with patch(
        'app.handlers.subscription.backup_login_nudge.settings._normalized_cabinet_url',
        return_value='https://example.com/cabinet',
    ):
        await send_backup_login_nudge(bot, _user_single())

    bot.send_message.assert_awaited_once()
    call_kwargs = bot.send_message.call_args
    assert call_kwargs.kwargs['chat_id'] == 111222333
    assert '/profile/accounts' in call_kwargs.kwargs['reply_markup'].inline_keyboard[0][0].url


@pytest.mark.asyncio
async def test_nudge_not_sent_when_two_methods():
    """Два метода → send_message НЕ вызывается."""
    from app.handlers.subscription.backup_login_nudge import send_backup_login_nudge

    bot = AsyncMock()
    bot.send_message = AsyncMock()

    with patch(
        'app.handlers.subscription.backup_login_nudge.settings._normalized_cabinet_url',
        return_value='https://example.com/cabinet',
    ):
        await send_backup_login_nudge(bot, _user_two())

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_nudge_not_sent_when_no_cabinet_url():
    """CABINET_URL не настроен → send_message НЕ вызывается."""
    from app.handlers.subscription.backup_login_nudge import send_backup_login_nudge

    bot = AsyncMock()
    bot.send_message = AsyncMock()

    with patch(
        'app.handlers.subscription.backup_login_nudge.settings._normalized_cabinet_url',
        return_value=None,
    ):
        await send_backup_login_nudge(bot, _user_single())

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_nudge_not_sent_when_no_telegram_id():
    """Нет telegram_id → send_message НЕ вызывается."""
    from app.handlers.subscription.backup_login_nudge import send_backup_login_nudge

    bot = AsyncMock()
    bot.send_message = AsyncMock()
    user_no_tg = _user_single(telegram_id=None)

    with patch(
        'app.handlers.subscription.backup_login_nudge.settings._normalized_cabinet_url',
        return_value='https://example.com/cabinet',
    ):
        await send_backup_login_nudge(bot, user_no_tg)

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_nudge_failure_does_not_raise():
    """Ошибка send_message не пробрасывается наружу (best-effort)."""
    from app.handlers.subscription.backup_login_nudge import send_backup_login_nudge

    bot = AsyncMock()
    bot.send_message = AsyncMock(side_effect=Exception('Telegram API error'))

    with patch(
        'app.handlers.subscription.backup_login_nudge.settings._normalized_cabinet_url',
        return_value='https://example.com/cabinet',
    ):
        # Не должно выбрасывать исключение
        await send_backup_login_nudge(bot, _user_single())

    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_nudge_preflight_exception_does_not_raise():
    """Исключение в needs_backup_login (до try) не пробрасывается — best-effort."""
    from app.handlers.subscription.backup_login_nudge import send_backup_login_nudge

    bot = AsyncMock()
    bot.send_message = AsyncMock()

    with patch(
        'app.handlers.subscription.backup_login_nudge.needs_backup_login',
        side_effect=AttributeError('unexpected user shape'),
    ):
        # Не должно выбрасывать исключение — вся функция best-effort
        await send_backup_login_nudge(bot, _user_single())

    bot.send_message.assert_not_awaited()
