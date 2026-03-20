"""Factory for creating Bot instances with optional custom Telegram API server."""

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer


def create_bot(token: str, **kwargs) -> Bot:
    """Create Bot instance, optionally using a custom Telegram Bot API server.

    If settings.TELEGRAM_BOT_API_URL is set, all API calls will go through
    that server instead of api.telegram.org.
    """
    from app.config import settings

    api_url = getattr(settings, 'TELEGRAM_BOT_API_URL', None)
    if api_url:
        api_server = TelegramAPIServer.from_base(api_url, is_local=True)
        session = AiohttpSession(api=api_server)
        return Bot(token=token, session=session, **kwargs)

    return Bot(token=token, **kwargs)
