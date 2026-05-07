"""Fallback-сквад при истечении подписки / исчерпании лимита трафика.

Концепция: вместо полного отключения VPN при истечении подписки или исчерпании
трафика — переводим юзера в специальный «fallback» сквад (только Telegram +
банки + кабинет). Так юзер сохраняет минимальный VPN, чтобы суметь зайти в
бота/кабинет и продлить.

В Remnawave подменяются:
- activeInternalSquads → [EXPIRY_FALLBACK_SQUAD_UUID]
- expireAt → +EXPIRY_FALLBACK_DAYS дней (чтобы Remnawave не перевёл в EXPIRED)
- trafficLimitBytes → +оригинал * 10 (чтобы fallback не упёрся в лимит)

Оригинальные значения сохраняются в Subscription.pre_expiry_* для восстановления.

При продлении / докупке / промокоде → restore_from_fallback() возвращает всё.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription, SubscriptionStatus, User


logger = structlog.get_logger(__name__)


def _extract_squad_uuids(raw) -> list[str]:
    """Нормализует activeInternalSquads из ответа Remnawave (list[dict]|list[str]) в list[str]."""
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


async def _get_remnawave_user(remnawave_uuid: str):
    """Достаёт юзера из Remnawave."""
    from app.services.remnawave_service import remnawave_service
    try:
        async with remnawave_service.get_api_client() as api:
            return await api.get_user_by_uuid(remnawave_uuid)
    except Exception as exc:
        logger.error('Ошибка получения юзера из Remnawave', remnawave_uuid=remnawave_uuid, exc=str(exc))
        return None


async def _patch_user_full(
    remnawave_uuid: str,
    *,
    squads: list[str] | None = None,
    expire_at: datetime | None = None,
    traffic_limit_bytes: int | None = None,
) -> bool:
    """Обновляет юзера в Remnawave указанными полями."""
    from app.services.remnawave_service import remnawave_service
    try:
        async with remnawave_service.get_api_client() as api:
            await api.update_user(
                uuid=remnawave_uuid,
                active_internal_squads=squads if squads is not None else None,
                expire_at=expire_at,
                traffic_limit_bytes=traffic_limit_bytes,
            )
        return True
    except Exception as exc:
        logger.error(
            'Ошибка обновления юзера в Remnawave',
            remnawave_uuid=remnawave_uuid,
            squads=squads,
            expire_at=expire_at,
            exc=str(exc),
        )
        return False


def _is_fallback_enabled() -> bool:
    return bool(settings.EXPIRY_FALLBACK_ENABLED and settings.EXPIRY_FALLBACK_SQUAD_UUID)


async def move_to_fallback(
    db: AsyncSession,
    subscription: Subscription,
    *,
    reason: str,
) -> bool:
    """Переводит подписку в fallback-сквад.

    reason: 'expired' | 'traffic'

    Возвращает True если перевод успешен (или уже был в fallback).
    """
    if not _is_fallback_enabled():
        return False

    if not subscription.remnawave_uuid:
        logger.warning('Нет remnawave_uuid у подписки', subscription_id=subscription.id)
        return False

    fallback_uuid = settings.EXPIRY_FALLBACK_SQUAD_UUID

    # Если уже в fallback — обновим только reason-флаг
    if subscription.expiry_fallback_active or subscription.traffic_fallback_active:
        if reason == 'expired':
            subscription.expiry_fallback_active = True
        elif reason == 'traffic':
            subscription.traffic_fallback_active = True
        await db.commit()
        return True

    # Считываем текущее состояние юзера в Remnawave
    rw_user = await _get_remnawave_user(subscription.remnawave_uuid)
    if not rw_user:
        return False

    original_squads = _extract_squad_uuids(getattr(rw_user, 'active_internal_squads', None))
    original_expire_at = getattr(rw_user, 'expire_at', None)
    original_traffic_limit = getattr(rw_user, 'traffic_limit_bytes', None)

    # Расширяем expireAt на EXPIRY_FALLBACK_DAYS вперёд от now
    new_expire_at = datetime.now(UTC) + timedelta(days=int(settings.EXPIRY_FALLBACK_DAYS or 90))

    # Если в reason='traffic' — поднимаем лимит чтобы fallback мог работать
    new_traffic_limit = original_traffic_limit
    if reason == 'traffic' and original_traffic_limit and original_traffic_limit > 0:
        # Поднимаем на 10x исходного лимита (или минимум +10GB)
        bonus = max(original_traffic_limit, 10 * 1024 ** 3)
        new_traffic_limit = original_traffic_limit + bonus

    ok = await _patch_user_full(
        subscription.remnawave_uuid,
        squads=[fallback_uuid],
        expire_at=new_expire_at,
        traffic_limit_bytes=new_traffic_limit,
    )
    if not ok:
        return False

    # Сохраняем оригиналы в БД
    subscription.pre_expiry_squads = original_squads
    subscription.pre_expiry_expire_at = original_expire_at
    subscription.pre_expiry_traffic_limit_bytes = original_traffic_limit
    if reason == 'expired':
        subscription.expiry_fallback_active = True
    elif reason == 'traffic':
        subscription.traffic_fallback_active = True
    subscription.expiry_fallback_started_at = datetime.now(UTC)
    await db.commit()

    logger.info(
        '🔄 Подписка переведена в fallback-сквад',
        subscription_id=subscription.id,
        user_id=subscription.user_id,
        reason=reason,
        original_squads=original_squads,
        fallback_squad=fallback_uuid,
    )
    return True


async def restore_from_fallback(
    db: AsyncSession,
    subscription: Subscription,
    *,
    new_expire_at: datetime | None = None,
    new_traffic_limit_bytes: int | None = None,
) -> bool:
    """Возвращает подписку из fallback в исходные сквады/лимиты.

    new_expire_at, new_traffic_limit_bytes — если переданы, используются вместо
    сохранённых оригиналов (актуально при продлении: подписка получила новый
    end_date, и его надо отнести в Remnawave).
    """
    if not subscription.expiry_fallback_active and not subscription.traffic_fallback_active:
        return True  # уже не в fallback

    if not subscription.remnawave_uuid:
        # Просто снимаем флаги в БД
        subscription.expiry_fallback_active = False
        subscription.traffic_fallback_active = False
        subscription.pre_expiry_squads = None
        subscription.pre_expiry_expire_at = None
        subscription.pre_expiry_traffic_limit_bytes = None
        subscription.expiry_fallback_started_at = None
        await db.commit()
        return True

    saved_squads = list(subscription.pre_expiry_squads or [])
    if not saved_squads and settings.DEFAULT_SQUAD_UUID:
        saved_squads = [settings.DEFAULT_SQUAD_UUID]

    expire_at = new_expire_at or subscription.pre_expiry_expire_at
    traffic_limit = new_traffic_limit_bytes
    if traffic_limit is None:
        traffic_limit = subscription.pre_expiry_traffic_limit_bytes

    ok = await _patch_user_full(
        subscription.remnawave_uuid,
        squads=saved_squads,
        expire_at=expire_at,
        traffic_limit_bytes=traffic_limit,
    )
    if not ok:
        return False

    subscription.expiry_fallback_active = False
    subscription.traffic_fallback_active = False
    subscription.pre_expiry_squads = None
    subscription.pre_expiry_expire_at = None
    subscription.pre_expiry_traffic_limit_bytes = None
    subscription.expiry_fallback_started_at = None
    await db.commit()

    logger.info(
        '✅ Подписка возвращена из fallback',
        subscription_id=subscription.id,
        user_id=subscription.user_id,
        restored_squads=saved_squads,
    )
    return True


async def cleanup_old_fallback_subscriptions(db: AsyncSession) -> dict:
    """Полностью отключает (status=expired в нашей БД, disable в Remnawave) подписки,
    которые висят в fallback больше EXPIRY_FALLBACK_DAYS.

    Возвращает {'cleaned': N, 'errors': N}.
    """
    if not settings.EXPIRED_CLEANUP_ENABLED:
        return {'cleaned': 0, 'skipped': True}

    threshold = datetime.now(UTC) - timedelta(days=int(settings.EXPIRY_FALLBACK_DAYS or 90))

    result = await db.execute(
        select(Subscription, User)
        .join(User, User.id == Subscription.user_id)
        .where(
            and_(
                Subscription.expiry_fallback_active.is_(True),
                Subscription.expiry_fallback_started_at.is_not(None),
                Subscription.expiry_fallback_started_at < threshold,
            )
        )
    )
    rows = list(result.all())

    cleaned = 0
    errors = 0

    require_zero_balance = bool(settings.EXPIRED_CLEANUP_REQUIRE_ZERO_BALANCE)

    from app.services.remnawave_service import remnawave_service

    for sub, user in rows:
        if require_zero_balance and (user.balance_kopeks or 0) > 0:
            continue
        try:
            if sub.remnawave_uuid:
                async with remnawave_service.get_api_client() as api:
                    await api.disable_user(sub.remnawave_uuid)
            sub.status = SubscriptionStatus.EXPIRED.value
            sub.expiry_fallback_active = False
            sub.traffic_fallback_active = False
            sub.expiry_fallback_started_at = None
            cleaned += 1
        except Exception as exc:
            logger.error('Ошибка cleanup fallback подписки', subscription_id=sub.id, exc=str(exc))
            errors += 1

    await db.commit()

    logger.info('🧹 Cleanup fallback подписок завершён', cleaned=cleaned, errors=errors, total=len(rows))
    return {'cleaned': cleaned, 'errors': errors, 'total': len(rows)}
