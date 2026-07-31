"""Мягкое предложение привязать резервный метод входа после оплаты подписки."""

import structlog
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.cabinet.routes.account_linking import needs_backup_login
from app.config import settings
from app.database.models import User

logger = structlog.get_logger(__name__)

_NUDGE_TEXT = (
    '🔐 Привяжи вход по почте или Яндексу — сможешь заходить на сайт '
    'и продлевать подписку в любой момент '
    '(и не потеряешь аккаунт при смене Telegram).'
)
_NUDGE_BUTTON_TEXT = '🔗 Привязать вход на сайте'


async def send_backup_login_nudge(bot: Bot, user: User) -> None:
    """Отправить мягкое предложение привязать резервный метод входа.

    Best-effort: любая ошибка логируется и игнорируется.
    Не отправляем, если:
    - у пользователя ≥ 2 методов входа
    - нет telegram_id (не можем отправить ЛС)
    - CABINET_URL не настроен
    """
    if not needs_backup_login(user):
        return
    if not user.telegram_id:
        return
    cabinet_url = settings._normalized_cabinet_url()
    if not cabinet_url:
        return

    linking_url = f'{cabinet_url}/profile/accounts'
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_NUDGE_BUTTON_TEXT, url=linking_url)]
        ]
    )

    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=_NUDGE_TEXT,
            reply_markup=keyboard,
        )
        logger.info(
            'Отправлено предложение резервного входа',
            user_id=user.id,
            telegram_id=user.telegram_id,
        )
    except Exception as exc:
        logger.warning(
            'Не удалось отправить предложение резервного входа (non-fatal)',
            user_id=user.id,
            error=str(exc),
        )
