"""Middleware: автоматически возвращает пользователя из штрафного сквада,
если он снова начал писать боту (= разблокировал бота).

Логика: если получено любое сообщение/callback от юзера с is_penalized=True,
значит он не блокирует бота — снимаем штраф и восстанавливаем сквады.
"""

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, TelegramObject

from app.config import settings


logger = structlog.get_logger(__name__)


class PenaltyAutoRestoreMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Фича может быть отключена
        if not settings.PENALTY_SQUAD_ENABLED:
            return await handler(event, data)

        if not isinstance(event, (Message, CallbackQuery, PreCheckoutQuery)):
            return await handler(event, data)

        db_user = data.get('db_user')
        db = data.get('db')
        if not db_user or not db or not getattr(db_user, 'is_penalized', False):
            return await handler(event, data)

        try:
            from app.services.penalty_squad_service import restore_user

            ok = await restore_user(db, db_user)
            if ok:
                logger.info(
                    '♻️ Авто-возврат из штрафного сквада: юзер снова пишет в бота',
                    user_id=db_user.id,
                    telegram_id=getattr(db_user, 'telegram_id', None),
                )
            else:
                logger.warning(
                    'Авто-возврат из штрафного: restore_user вернул False',
                    user_id=db_user.id,
                )
        except Exception as e:
            logger.error('Ошибка авто-возврата из штрафного сквада', user_id=db_user.id, error=e)

        return await handler(event, data)
