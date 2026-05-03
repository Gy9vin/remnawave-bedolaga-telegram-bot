"""Сервис штрафного сквада: перемещение нарушителей и авто-возврат после разблокировки."""

from __future__ import annotations

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription, SubscriptionStatus, User, UserStatus


logger = structlog.get_logger(__name__)


async def _patch_remnawave_squad(remnawave_uuid: str, squad_uuid: str | None) -> bool:
    """Меняет externalSquadUuid у пользователя в Remnawave.

    Верифицирует результат: после PATCH перечитывает юзера и сверяет externalSquadUuid.
    Это страховка от глобального retry в API-клиенте, который при A039 (FK violation)
    делает повтор без externalSquadUuid — успешно, но сквад не применяется.
    """
    from app.services.remnawave_service import remnawave_service
    try:
        async with remnawave_service.get_api_client() as api:
            await api.update_user(uuid=remnawave_uuid, external_squad_uuid=squad_uuid)
            # Верификация
            updated = await api.get_user_by_uuid(remnawave_uuid)
        if updated is None:
            logger.error('Не удалось перечитать юзера после смены сквада', remnawave_uuid=remnawave_uuid)
            return False
        actual_squad = getattr(updated, 'external_squad_uuid', None)
        if actual_squad != squad_uuid:
            logger.error(
                'Смена сквада не применилась (вероятно UUID не существует в панели)',
                remnawave_uuid=remnawave_uuid,
                expected=squad_uuid,
                actual=actual_squad,
            )
            return False
        return True
    except Exception as exc:
        logger.error('Ошибка смены сквада в Remnawave', remnawave_uuid=remnawave_uuid, squad_uuid=squad_uuid, exc=str(exc))
        return False


async def penalize_user(db: AsyncSession, user: User) -> bool:
    """Перемещает пользователя в штрафной сквад и помечает is_penalized=True.

    Возвращает True при успехе.
    """
    if not settings.PENALTY_SQUAD_ENABLED:
        logger.warning('Штрафной сквад отключён в настройках')
        return False

    penalty_uuid = settings.PENALTY_SQUAD_UUID
    if not penalty_uuid:
        logger.error('PENALTY_SQUAD_UUID не задан')
        return False

    if not user.remnawave_uuid:
        logger.warning('У пользователя нет remnawave_uuid', user_id=user.id)
        return False

    if user.is_penalized:
        logger.debug('Пользователь уже в штрафном скваде', user_id=user.id)
        return True

    ok = await _patch_remnawave_squad(user.remnawave_uuid, penalty_uuid)
    if not ok:
        return False

    await db.execute(
        update(User).where(User.id == user.id).values(is_penalized=True)
    )
    await db.commit()

    logger.info('Пользователь перемещён в штрафной сквад', user_id=user.id, telegram_id=user.telegram_id, penalty_squad=penalty_uuid)
    return True


async def restore_user(db: AsyncSession, user: User) -> bool:
    """Возвращает пользователя из штрафного сквада в обычный и снимает is_penalized.

    Возвращает True при успехе.
    """
    if not user.is_penalized:
        return True

    if not user.remnawave_uuid:
        await db.execute(update(User).where(User.id == user.id).values(is_penalized=False))
        await db.commit()
        return True

    default_uuid = settings.DEFAULT_SQUAD_UUID or None
    ok = await _patch_remnawave_squad(user.remnawave_uuid, default_uuid)
    if not ok:
        return False

    await db.execute(
        update(User).where(User.id == user.id).values(is_penalized=False)
    )
    await db.commit()

    logger.info('Пользователь возвращён из штрафного сквада', user_id=user.id, telegram_id=user.telegram_id, default_squad=default_uuid)
    return True


async def auto_scan_and_penalize(db: AsyncSession) -> dict:
    """Сканирует активных подписчиков и штрафует новых нарушителей.

    Использует send_chat_action для проверки — не блокирующий для пользователей.
    Возвращает словарь со статистикой.
    """
    from datetime import UTC, datetime

    from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

    from app.services.blocked_users_service import BlockedUsersService

    if not settings.PENALTY_SQUAD_ENABLED or not settings.PENALTY_SQUAD_UUID:
        return {'skipped': True, 'reason': 'feature disabled or squad not configured'}

    # Берём только активных подписчиков не помеченных как штрафные
    current_time = datetime.now(UTC)
    result = await db.execute(
        select(User)
        .join(Subscription, Subscription.user_id == User.id)
        .where(
            User.status == UserStatus.ACTIVE.value,
            User.telegram_id.isnot(None),
            User.is_penalized.is_(False),
            Subscription.status == SubscriptionStatus.ACTIVE.value,
            Subscription.end_date > current_time,
        )
        .distinct()
    )
    users = list(result.scalars().all())

    penalized = 0
    errors = 0

    # Импортируем бота из основного bot модуля
    try:
        from app.services.monitoring_service import monitoring_service
        bot = monitoring_service.bot
        if not bot:
            return {'skipped': True, 'reason': 'bot not initialized'}
    except Exception:
        return {'skipped': True, 'reason': 'cannot get bot instance'}

    service = BlockedUsersService(bot)

    for user in users:
        try:
            from app.services.blocked_users_service import BlockCheckStatus
            status = await service.check_user_blocked(user.telegram_id)
            if status == BlockCheckStatus.BLOCKED:
                ok = await penalize_user(db, user)
                if ok:
                    penalized += 1
        except Exception as exc:
            logger.error('Ошибка при автосканировании пользователя', user_id=user.id, exc=str(exc))
            errors += 1

    logger.info('Авто-сканирование штрафного сквада завершено', penalized=penalized, errors=errors, checked=len(users))
    return {'penalized': penalized, 'errors': errors, 'checked': len(users)}
