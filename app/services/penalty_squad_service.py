"""Сервис штрафного сквада: перемещение нарушителей и авто-возврат после разблокировки."""

from __future__ import annotations

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription, SubscriptionStatus, User, UserStatus


logger = structlog.get_logger(__name__)


def _extract_squad_uuids(raw) -> list[str]:
    """Нормализует activeInternalSquads из ответа Remnawave (list[dict] | list[str]) в list[str]."""
    if not raw:
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            uuid_val = item.get('uuid') or item.get('id')
            if uuid_val:
                out.append(uuid_val)
    return out


async def _get_user_squads(remnawave_uuid: str) -> list[str] | None:
    """Возвращает текущий список UUID activeInternalSquads пользователя из Remnawave."""
    from app.services.remnawave_service import remnawave_service
    try:
        async with remnawave_service.get_api_client() as api:
            user = await api.get_user_by_uuid(remnawave_uuid)
        if not user:
            return None
        return _extract_squad_uuids(getattr(user, 'active_internal_squads', None))
    except Exception as exc:
        logger.error('Ошибка получения сквадов юзера', remnawave_uuid=remnawave_uuid, exc=str(exc))
        return None


async def _set_user_squads(remnawave_uuid: str, squads: list[str]) -> bool:
    """Заменяет activeInternalSquads у пользователя в Remnawave.

    Верифицирует результат: после PATCH перечитывает юзера и сверяет состав UUID.
    """
    from app.services.remnawave_service import remnawave_service
    try:
        async with remnawave_service.get_api_client() as api:
            await api.update_user(uuid=remnawave_uuid, active_internal_squads=squads)
            updated = await api.get_user_by_uuid(remnawave_uuid)
        if updated is None:
            logger.error('Не удалось перечитать юзера после смены сквадов', remnawave_uuid=remnawave_uuid)
            return False
        actual = set(_extract_squad_uuids(getattr(updated, 'active_internal_squads', None)))
        expected = set(squads)
        if actual != expected:
            logger.error(
                'Смена сквадов не применилась (UUID не существует в панели?)',
                remnawave_uuid=remnawave_uuid,
                expected=sorted(expected),
                actual=sorted(actual),
            )
            return False
        return True
    except Exception as exc:
        logger.error(
            'Ошибка смены сквадов в Remnawave',
            remnawave_uuid=remnawave_uuid,
            squads=squads,
            exc=str(exc),
        )
        return False


async def penalize_user(db: AsyncSession, user: User) -> bool:
    """Перемещает пользователя в штрафной internal-сквад и помечает is_penalized=True.

    Сохраняет текущий список activeInternalSquads в users.pre_penalty_squads
    чтобы корректно восстановить при разблокировке.
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

    # Сохраняем текущий список сквадов перед заменой
    current_squads = await _get_user_squads(user.remnawave_uuid)
    if current_squads is None:
        logger.error('Не удалось получить текущие сквады юзера', user_id=user.id)
        return False

    ok = await _set_user_squads(user.remnawave_uuid, [penalty_uuid])
    if not ok:
        return False

    await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(is_penalized=True, pre_penalty_squads=current_squads)
    )
    await db.commit()

    logger.info(
        'Пользователь перемещён в штрафной сквад',
        user_id=user.id,
        telegram_id=user.telegram_id,
        penalty_squad=penalty_uuid,
        saved_squads=current_squads,
    )
    return True


async def restore_user(db: AsyncSession, user: User) -> bool:
    """Возвращает пользователя из штрафного сквада в исходный.

    Использует users.pre_penalty_squads (сохранённый при penalize_user).
    Если поле пустое — фолбэк на [DEFAULT_SQUAD_UUID] или [].
    """
    if not user.is_penalized:
        return True

    if not user.remnawave_uuid:
        await db.execute(
            update(User)
            .where(User.id == user.id)
            .values(is_penalized=False, pre_penalty_squads=None)
        )
        await db.commit()
        return True

    saved = list(getattr(user, 'pre_penalty_squads', None) or [])
    if not saved:
        # Фолбэк на дефолтный сквад
        if settings.DEFAULT_SQUAD_UUID:
            saved = [settings.DEFAULT_SQUAD_UUID]
        else:
            saved = []

    ok = await _set_user_squads(user.remnawave_uuid, saved)
    if not ok:
        return False

    await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(is_penalized=False, pre_penalty_squads=None)
    )
    await db.commit()

    logger.info(
        'Пользователь возвращён из штрафного сквада',
        user_id=user.id,
        telegram_id=user.telegram_id,
        restored_squads=saved,
    )
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
