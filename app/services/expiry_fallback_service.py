"""Fallback-сквад при истечении подписки / исчерпании лимита трафика.

КОНЦЕПЦИЯ
=========

Вместо полного отключения VPN при истечении подписки или исчерпании трафика
переводим юзера в специальный «fallback» сквад (только Telegram + банки + кабинет).
Юзер сохраняет минимальный VPN, чтобы суметь зайти в бота/кабинет и продлить.

ПОДХОД GRACE-PERIOD
===================

При переезде в fallback в Remnawave подменяются:
- activeInternalSquads → [EXPIRY_FALLBACK_SQUAD_UUID]
- expireAt → текущий момент + EXPIRY_FALLBACK_GRACE_DAYS (по умолчанию 3 дня)
- trafficLimitBytes → +EXPIRY_FALLBACK_GRACE_GB (для traffic-fallback)

Grace-дата задаётся ОДНАЖДЫ при переезде в fallback (now + grace_days) и
больше НЕ продлевается: как только `now - expiry_fallback_started_at >= grace_days`
и юзер не оплатил — reconcile отключает его. Так юзер без оплаты не висит в
reserved бесконечно.

Полное отключение происходит через `EXPIRY_FALLBACK_TOTAL_DAYS` (90 дней)
после `expiry_fallback_started_at`.

ВОЗВРАТ ЗАСТРЯВШИХ (panel-truth reconcile)
------------------------------------------
Если после оплаты PATCH сквадов в панель не дошёл (гонка/частичный ответ), юзер
остаётся в fallback-скваде панели, хотя в БД подписка уже активна. Косвенные
признаки БД (флаги, connected_squads) такой случай не покрывают, поэтому
reconcile раз в цикл читает fallback-сквад ПАНЕЛИ напрямую и восстанавливает
всех активных оплаченных подписок, реально сидящих в нём.

ВОЗВРАТ ИЗ FALLBACK
===================

Происходит автоматически при:
1. Продление через `extend_subscription` (любой источник: бот, кабинет, автопродление)
2. Докупка трафика через `add_subscription_traffic`
3. Webhook `user.traffic_reset` (Remnawave сбросил трафик по периоду)
4. Periodic reconcile (раз в 15 мин):
   - Внешнее продление через панель Remnawave (admin вручную увеличил expireAt)
   - Внешнее пополнение трафика через панель (admin увеличил trafficLimitBytes)
   - Если юзера руками вытащили из fallback-сквада в панели → mark returned

УСТОЙЧИВОСТЬ
============

Reconcile также защищает от:
- Потерянных вебхуков (статус EXPIRED/LIMITED в нашей БД, но fallback не применён)
- Истечения grace-периода (юзер ещё сидит в fallback, expireAt уже прошёл)
- Race conditions с conflict 409 от Remnawave

DEV_MODE
========

Если `EXPIRY_FALLBACK_DEV_MODE=true`, fallback применяется ТОЛЬКО для юзеров
из `EXPIRY_FALLBACK_DEV_USER_IDS` (через запятую). Удобно для тестирования.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import Subscription, SubscriptionStatus, Tariff, User


logger = structlog.get_logger(__name__)


# ============================================================================
# Helpers
# ============================================================================


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


def build_fallback_squad_ids(panel_users: list, fallback_squad_uuid: str) -> set[int]:
    """Множество числовых remnawave_id пользователей панели, чьи
    ``active_internal_squads`` содержат ``fallback_squad_uuid``.

    Чистая функция — источник истины для «кто реально в fallback-скваде».
    Используется и периодическим reconcile, и разовым repair-скриптом.
    """
    result: set[int] = set()
    for pu in panel_users:
        active_squads = _extract_squad_uuids(getattr(pu, 'active_internal_squads', None))
        if fallback_squad_uuid in active_squads:
            pu_id = getattr(pu, 'id', None)
            if pu_id is not None:
                result.add(pu_id)
    return result


def choose_target_squads(
    connected_squads: list[str] | None,
    default_squad_uuid: str | None,
    pre_expiry_squads: list[str] | None = None,
) -> list[str]:
    """Целевые сквады для восстановления застрявшей подписки.

    Приоритет: ``connected_squads`` (то, что подписка должна иметь по БД) →
    ``pre_expiry_squads`` (снимок до перевода в fallback) → ``[DEFAULT_SQUAD_UUID]``.
    Если ничего нет — пустой список (кандидат пропускается вызывающим кодом).
    """
    if connected_squads:
        return list(connected_squads)
    if pre_expiry_squads:
        return list(pre_expiry_squads)
    if default_squad_uuid:
        return [default_squad_uuid]
    return []


def _chunked(seq: list, size: int):
    """Разбивает список на чанки по ``size`` элементов (для безопасного IN-запроса)."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _is_fallback_enabled() -> bool:
    return bool(settings.EXPIRY_FALLBACK_ENABLED and settings.EXPIRY_FALLBACK_SQUAD_UUID)


def _grace_days() -> int:
    return max(1, int(getattr(settings, 'EXPIRY_FALLBACK_GRACE_DAYS', 3) or 3))


def _grace_gb() -> int:
    return max(1, int(getattr(settings, 'TRAFFIC_FALLBACK_GRACE_GB', 10) or 10))


def _total_fallback_days() -> int:
    """Сколько суммарно держим юзера в fallback (по дефолту 90 дней)."""
    return max(_grace_days(), int(getattr(settings, 'EXPIRY_FALLBACK_DAYS', 90) or 90))


def _is_dev_user_allowed(subscription: Subscription) -> bool:
    """В DEV_MODE применяем fallback только к whitelisted юзерам."""
    if not getattr(settings, 'EXPIRY_FALLBACK_DEV_MODE', False):
        return True
    raw_ids = getattr(settings, 'EXPIRY_FALLBACK_DEV_USER_IDS', None) or ''
    if isinstance(raw_ids, str):
        ids = {x.strip() for x in raw_ids.split(',') if x.strip()}
    else:
        ids = {str(x).strip() for x in (raw_ids or [])}
    return str(subscription.user_id) in ids


async def _resolve_panel_ref(api, db, subscription) -> int | None:
    """Резолвит числовой remna_id панельного пользователя подписки.

    3.0.0 убрал панельный uuid целиком — идентичность чисто числовая, поэтому
    здесь (в отличие от старой версии) возвращается просто ``remna_id``, без
    пары (uuid, remna_id).

    Юзер для get_panel_user_ref получается через ``get_user_by_id(db,
    subscription.user_id)`` — с eager-load подписок, а НЕ через
    ``subscription.user``: этот relationship в вызывающих местах этого модуля
    (move_to_fallback/restore_from_fallback вызываются из webhook-сервиса,
    monitoring, кабинета, крон-джоб reconcile и т.д.) далеко не всегда
    предзагружен через selectinload, а синхронный доступ к незагруженному
    relationship-атрибуту на AsyncSession валит ``MissingGreenlet`` вместо
    аккуратного резолва (см. fe008155 — тот же класс бага). user_id — обычная
    колонка, лишнего лениво загружаемого обращения не требует.

    Без ``db`` (вызывающий не держит сессию) — best-effort пробуем
    ``subscription.user``, если он уже был предзагружен вызывающим; иначе
    отдаём get_panel_user_ref только short_uuid — резолв через telegram_id в
    этом случае недоступен.
    """
    from app.services.remnawave_service import get_panel_user_ref

    if subscription is None:
        return None

    user = None
    if db is not None and getattr(subscription, 'user_id', None):
        from app.database.crud.user import get_user_by_id

        try:
            user = await get_user_by_id(db, subscription.user_id)
        except Exception as exc:  # noqa: BLE001 - best-effort, резолв продолжится без user
            logger.warning(
                '_resolve_panel_ref: не удалось загрузить User по user_id',
                user_id=subscription.user_id,
                error=str(exc),
            )

    if user is None:
        try:
            user = subscription.user
        except Exception:  # noqa: BLE001 - незагруженный relationship без db, не крашимся
            user = None

    _uuid, remna_id = await get_panel_user_ref(api, db, subscription=subscription, user=user)
    return remna_id


async def _has_panel_identity(db, subscription) -> bool:
    """Есть ли у подписки достижимый панельный пользователь.

    Проверять только колонки подписки нельзя: в однотарифном режиме идентичность
    лежит на пользователе, и такая проверка ложно говорит «панели нет».
    """
    if subscription is not None and (subscription.remnawave_id or subscription.remnawave_short_uuid):
        return True
    from app.services.remnawave_service import remnawave_service
    try:
        async with remnawave_service.get_api_client() as api:
            remna_id = await _resolve_panel_ref(api, db, subscription)
        return remna_id is not None
    except Exception as exc:
        logger.warning(
            '_has_panel_identity: ошибка резолва панельной идентичности',
            subscription_id=getattr(subscription, 'id', None),
            error=str(exc),
        )
        return False


async def _get_remnawave_user(remna_id_hint: int | None, db=None, subscription=None):
    """Достаёт юзера из Remnawave.

    Возвращает None если:
      - 404 (юзер удалён в панели) — нормальный кейс, не логируем как ошибку
      - remna_id не удалось резолвить — управляемая неудача (warning), панель
        не дёргаем вовсе
      - сетевая/другая ошибка — лог error

    db + subscription используются для разрешения идентификатора через
    get_panel_user_ref; ``remna_id_hint`` — фолбэк, когда вызывающий не
    держит сессию (db/subscription отсутствуют).
    """
    from app.external.remnawave_api import RemnaWaveAPIError
    from app.services.remnawave_service import remnawave_service
    try:
        async with remnawave_service.get_api_client() as api:
            if db is not None and subscription is not None:
                remna_id = await _resolve_panel_ref(api, db, subscription)
            else:
                remna_id = remna_id_hint
            if remna_id is None:
                logger.warning(
                    'expiry_fallback: remna_id не резолвлен — get_user пропущен '
                    '(управляемая неудача вместо падения)',
                    remna_id_hint=remna_id_hint,
                )
                return None
            return await api.get_user_by_id(remna_id)
    except RemnaWaveAPIError as exc:
        if getattr(exc, 'status_code', None) == 404:
            logger.debug('Remnawave: юзер не найден (404)', remna_id_hint=remna_id_hint)
            return None
        logger.error('Ошибка получения юзера из Remnawave', remna_id_hint=remna_id_hint, exc=str(exc))
        return None
    except Exception as exc:
        logger.error('Ошибка получения юзера из Remnawave', remna_id_hint=remna_id_hint, exc=str(exc))
        return None


async def _patch_user_full(
    remna_id_hint: int | None,
    *,
    squads: list[str] | None = None,
    expire_at: datetime | None = None,
    traffic_limit_bytes: int | None = None,
    verify_squad_in: list[str] | None = None,
    db=None,
    subscription=None,
) -> bool:
    """Обновляет юзера в Remnawave и при ошибке/conflict проверяет реальное состояние.

    verify_squad_in — UUID(ы) ожидаемых сквадов для верификации. Если PATCH вернул
    409 или ошибку, мы делаем get_user и проверяем не применилось ли уже.

    db + subscription используются для разрешения идентификатора через
    get_panel_user_ref; ``remna_id_hint`` — фолбэк, когда вызывающий не
    держит сессию (db/subscription отсутствуют).
    """
    from app.services.remnawave_service import remnawave_service
    try:
        async with remnawave_service.get_api_client() as api:
            if db is not None and subscription is not None:
                remna_id = await _resolve_panel_ref(api, db, subscription)
            else:
                remna_id = remna_id_hint
            if remna_id is None:
                logger.warning(
                    'expiry_fallback: remna_id не резолвлен — update_user пропущен '
                    '(управляемая неудача вместо падения)',
                    remna_id_hint=remna_id_hint,
                )
                return False
            await api.update_user(
                user_id=remna_id,
                active_internal_squads=squads if squads is not None else None,
                expire_at=expire_at,
                traffic_limit_bytes=traffic_limit_bytes,
            )
        return True
    except Exception as exc:
        # Conflict 409 verify: возможно изменения уже применились
        err_text = str(exc).lower()
        is_conflict = '409' in err_text or 'conflict' in err_text
        if is_conflict and verify_squad_in:
            try:
                async with remnawave_service.get_api_client() as api:
                    if db is not None and subscription is not None:
                        remna_id = await _resolve_panel_ref(api, db, subscription)
                    else:
                        remna_id = remna_id_hint
                    if remna_id is None:
                        raise RuntimeError('remna_id unresolved for verify — skip')
                    verified = await api.get_user_by_id(remna_id)
                if verified:
                    actual = set(_extract_squad_uuids(getattr(verified, 'active_internal_squads', None)))
                    expected = set(verify_squad_in)
                    if expected.issubset(actual) or actual == expected:
                        logger.info(
                            '409 conflict, но сквад уже применён — считаем успехом',
                            remna_id_hint=remna_id_hint,
                            actual=sorted(actual),
                        )
                        return True
            except Exception:
                pass
        logger.error(
            'Ошибка обновления юзера в Remnawave',
            remna_id_hint=remna_id_hint,
            squads=squads,
            expire_at=expire_at,
            exc=str(exc),
        )
        return False


async def _reset_remnawave_traffic(remna_id_hint: int | None, db=None, subscription=None) -> bool:
    """Сбрасывает счётчик used_traffic_bytes юзера в Remnawave в 0.

    Используется при move_to_fallback(reason='expired') — после сброса
    видно реальное потребление в fallback-скваде (Telegram + банки),
    обычно доли GB. Резкий рост = squad настроен неправильно.

    Не критичен: если не получилось, продолжаем (move уже прошёл).

    db + subscription используются для разрешения идентификатора через
    get_panel_user_ref; ``remna_id_hint`` — фолбэк, когда вызывающий не
    держит сессию (db/subscription отсутствуют).
    """
    from app.services.remnawave_service import remnawave_service
    try:
        async with remnawave_service.get_api_client() as api:
            if db is not None and subscription is not None:
                remna_id = await _resolve_panel_ref(api, db, subscription)
            else:
                remna_id = remna_id_hint
            if remna_id is None:
                logger.warning(
                    'expiry_fallback: remna_id не резолвлен — reset_user_traffic '
                    'пропущен (управляемая неудача вместо падения)',
                    remna_id_hint=remna_id_hint,
                )
                return False
            await api.reset_user_traffic(remna_id)
        logger.info('🔄 Сброшен трафик в Remnawave (move to fallback)', remna_id_hint=remna_id_hint)
        return True
    except Exception as exc:
        logger.warning(
            'Не удалось сбросить трафик в Remnawave (move to fallback)',
            remna_id_hint=remna_id_hint,
            exc=str(exc),
        )
        return False


# ============================================================================
# Move / Restore — основные операции
# ============================================================================


async def move_to_fallback(
    db: AsyncSession,
    subscription: Subscription,
    *,
    reason: str,
    notify: bool = True,
) -> bool:
    """Переводит подписку в fallback-сквад с grace-периодом.

    reason: 'expired' | 'traffic'
    notify: если False — не отправлять TG/admin уведомления (bulk-операции).
            При массовом scan-and-move уведомления спавнят сотни bg-tasks
            и каждая берёт DB-соединение — пул выжимается → весь сервис ляжет.

    Возвращает True если перевод успешен (или уже был в fallback).
    """
    if not _is_fallback_enabled():
        return False

    if not _is_dev_user_allowed(subscription):
        logger.debug(
            'DEV_MODE: пропуск move_to_fallback (юзер не в whitelist)',
            subscription_id=subscription.id,
            user_id=subscription.user_id,
        )
        return False

    if not await _has_panel_identity(db, subscription):
        logger.warning('Нет панельной идентичности у подписки', subscription_id=subscription.id)
        return False

    fallback_uuid = settings.EXPIRY_FALLBACK_SQUAD_UUID

    # Если уже в fallback — обновим только reason-флаг и продлим grace при необходимости
    if subscription.expiry_fallback_active or subscription.traffic_fallback_active:
        if reason == 'expired':
            subscription.expiry_fallback_active = True
        elif reason == 'traffic':
            subscription.traffic_fallback_active = True
        await db.commit()
        return True

    # Считываем текущее состояние юзера в Remnawave
    rw_user = await _get_remnawave_user(subscription.remnawave_id, db=db, subscription=subscription)
    if not rw_user:
        return False

    original_squads = _extract_squad_uuids(getattr(rw_user, 'active_internal_squads', None))
    # Не сохраняем сам fallback-сквад в pre_expiry (если по какой-то причине он там)
    original_squads = [s for s in original_squads if s != fallback_uuid]

    original_expire_at = getattr(rw_user, 'expire_at', None)
    original_traffic_limit = getattr(rw_user, 'traffic_limit_bytes', None)

    # Grace-период вместо «+10 лет»: текущий момент + EXPIRY_FALLBACK_GRACE_DAYS
    new_expire_at = datetime.now(UTC) + timedelta(days=_grace_days())

    # Для traffic-fallback поднимаем лимит на GRACE_GB
    new_traffic_limit = original_traffic_limit
    if reason == 'traffic' and original_traffic_limit and original_traffic_limit > 0:
        new_traffic_limit = int(original_traffic_limit) + _grace_gb() * (1024 ** 3)

    ok = await _patch_user_full(
        subscription.remnawave_id,
        squads=[fallback_uuid],
        expire_at=new_expire_at,
        traffic_limit_bytes=new_traffic_limit,
        verify_squad_in=[fallback_uuid],
        db=db,
        subscription=subscription,
    )
    if not ok:
        return False

    # Сброс трафика — только для expired (для traffic-fallback нельзя:
    # юзер уже превысил лимит, +10GB grace потеряет смысл при сбросе).
    # Сброс нужен чтобы по счётчику было видно реальный fallback-трафик
    # (Telegram + банки) — должно быть ~доли GB, аномалия = утечка в squad'е.
    if reason == 'expired':
        await _reset_remnawave_traffic(subscription.remnawave_id, db=db, subscription=subscription)
        subscription.traffic_used_gb = 0.0
        subscription.traffic_reset_at = datetime.now(UTC)

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
        grace_days=_grace_days(),
    )

    # Уведомления в фоне (админам + юзеру)
    if notify:
        _dispatch_fallback_notifications(
            user_id=subscription.user_id,
            subscription_id=subscription.id,
            action='moved',
            reason=reason,
            original_squads=original_squads,
            fallback_squad=fallback_uuid,
        )
    return True


# Ограничивает параллельность bg-нотификаций (их open AsyncSession + Bot).
# Без лимита массовый move_to_fallback (например, monitoring при старте после
# даунтайма) выжимает DB connection pool → весь сервис ложится.
_NOTIFY_SEMAPHORE = asyncio.Semaphore(5)


async def regrace_disabled_subscriptions(db: AsyncSession) -> dict:
    """Массово возвращает DISABLED/EXPIRED подписки в fallback и даёт +grace дней.

    Юзкейс: после cleanup_expired (или гонки webhook → DISABLED) куча юзеров
    висит в Remnawave/БД со статусом DISABLED/EXPIRED. Эта функция:
      1. Находит подписки status IN (DISABLED, EXPIRED) AND end_date <= now
         AND is_trial=False AND user.status != BLOCKED AND fallback флаги
         сняты (expiry_fallback_active=False, чтобы не дёргать тех, кто УЖЕ
         в fallback и сейчас в grace-периоде)
      2. Для каждой — enable_user() в Remnawave (если в Remnawave DISABLED)
         + status=EXPIRED в БД + move_to_fallback(reason='expired')
         → expire_at = now+grace_days
      3. Возвращает статистику.

    Используется для разовой амнистии после cleanup или для «дать всем
    ещё шанс продлиться 3 дня».
    """
    from app.services.remnawave_service import remnawave_service

    stats = {
        'success': False,
        'scanned': 0,
        'restored': 0,
        'failed': 0,
        'skipped_no_uuid': 0,
        'skipped_blocked_user': 0,
        'skipped_already_in_fallback': 0,
    }

    if not bool(getattr(settings, 'EXPIRY_FALLBACK_ENABLED', False)):
        stats['error'] = 'EXPIRY_FALLBACK_ENABLED=false'
        return stats
    if not getattr(settings, 'EXPIRY_FALLBACK_SQUAD_UUID', None):
        stats['error'] = 'EXPIRY_FALLBACK_SQUAD_UUID не задан'
        return stats

    now = datetime.now(UTC)
    from app.database.models import User as UserModel

    # Ловим обе категории:
    # - DISABLED: гонка webhook не успела (наш webhook fix может ещё не быть)
    # - EXPIRED: cleanup нормально снял флаги fallback, но в Remnawave juser DISABLED
    # Фильтруем тех, кто УЖЕ в активном fallback (expiry_fallback_active=true) —
    # их трогать не надо, они в grace, продлевают сами.
    result = await db.execute(
        select(Subscription)
        .options(selectinload(Subscription.user))
        .join(UserModel, UserModel.id == Subscription.user_id)
        .where(
            Subscription.status.in_([
                SubscriptionStatus.DISABLED.value,
                SubscriptionStatus.EXPIRED.value,
            ]),
            Subscription.is_trial.is_(False),
            Subscription.end_date <= now,
            UserModel.status != 'blocked',
            Subscription.expiry_fallback_active.is_not(True),
            Subscription.traffic_fallback_active.is_not(True),
            or_(
                Subscription.remnawave_id.is_not(None),
                Subscription.remnawave_short_uuid.is_not(None),
            ),
        )
    )
    subs = list(result.scalars().all())
    stats['scanned'] = len(subs)

    total = len(subs)
    processed = 0
    for sub in subs:
        try:
            # enable юзера в Remnawave (после cleanup он DISABLED там)
            try:
                from app.services.remnawave_service import get_panel_user_ref
                async with remnawave_service.get_api_client() as api:
                    _uuid, _remna_id = await get_panel_user_ref(
                        api, db, subscription=sub, user=sub.user,
                    )
                    if _remna_id is not None:
                        await api.enable_user(_remna_id)
            except Exception as enable_exc:
                logger.warning(
                    'regrace: enable_user failed, continuing',
                    subscription_id=sub.id,
                    error=str(enable_exc),
                )

            # status=EXPIRED чтобы move_to_fallback корректно отработал
            sub.status = SubscriptionStatus.EXPIRED.value
            await db.commit()

            ok = await move_to_fallback(db, sub, reason='expired', notify=False)
            if ok:
                stats['restored'] += 1
            else:
                stats['failed'] += 1
        except Exception as exc:
            stats['failed'] += 1
            logger.error('regrace: ошибка обработки подписки', subscription_id=sub.id, error=str(exc))

        processed += 1
        if processed % 100 == 0:
            logger.info(
                'regrace progress',
                processed=processed,
                total=total,
                restored=stats['restored'],
                failed=stats['failed'],
            )

    stats['success'] = True
    logger.info('regrace_disabled_subscriptions completed', **stats)
    return stats


async def scan_and_move_expired(db: AsyncSession) -> dict:
    """Сканирует БД и переводит просроченные подписки в fallback-сквад.

    DEV_MODE=true: переводит ТОЛЬКО юзеров из EXPIRY_FALLBACK_DEV_USER_IDS.
    DEV_MODE=false: переводит ВСЕХ с истёкшей подпиской.

    Возвращает словарь со счётчиками. Если fallback выключен или нет SQUAD_UUID —
    в ответе будет 'success': False и описание в 'error'.

    Используется и в кабинетном API, и в админ-боте.
    """
    if not bool(getattr(settings, 'EXPIRY_FALLBACK_ENABLED', False)):
        return {
            'success': False,
            'error': 'EXPIRY_FALLBACK_ENABLED=false',
            'scanned': 0,
            'moved': 0,
            'skipped_dev_mode': 0,
            'skipped_no_remnawave_uuid': 0,
            'failed': 0,
            'dev_mode_active': False,
            'dev_mode_user_count': 0,
        }

    if not getattr(settings, 'EXPIRY_FALLBACK_SQUAD_UUID', None):
        return {
            'success': False,
            'error': 'EXPIRY_FALLBACK_SQUAD_UUID не задан',
            'scanned': 0,
            'moved': 0,
            'skipped_dev_mode': 0,
            'skipped_no_remnawave_uuid': 0,
            'failed': 0,
            'dev_mode_active': False,
            'dev_mode_user_count': 0,
        }

    dev_mode = bool(getattr(settings, 'EXPIRY_FALLBACK_DEV_MODE', False))
    raw_ids = getattr(settings, 'EXPIRY_FALLBACK_DEV_USER_IDS', None) or ''
    if isinstance(raw_ids, str):
        dev_ids = {x.strip() for x in raw_ids.split(',') if x.strip()}
    else:
        dev_ids = {str(x).strip() for x in (raw_ids or [])}

    result = await db.execute(
        select(Subscription)
        .outerjoin(Tariff, Subscription.tariff_id == Tariff.id)
        .options(selectinload(Subscription.user))
        .where(
            and_(
                Subscription.end_date <= func.now(),
                Subscription.status.in_(
                    [SubscriptionStatus.ACTIVE.value, SubscriptionStatus.EXPIRED.value]
                ),
                Subscription.expiry_fallback_active.is_not(True),
                Subscription.traffic_fallback_active.is_not(True),
                ~and_(
                    Tariff.is_daily.is_(True),
                    Subscription.is_daily_paused.is_(False),
                ),
            )
        )
    )
    candidates = list(result.scalars().all())

    scanned = 0
    moved = 0
    skipped_dev = 0
    skipped_no_uuid = 0
    failed = 0

    for sub in candidates:
        scanned += 1
        if not (sub.remnawave_id or sub.remnawave_short_uuid):
            skipped_no_uuid += 1
            continue
        if not _is_dev_user_allowed(sub):
            skipped_dev += 1
            continue
        try:
            ok = await move_to_fallback(db, sub, reason='expired', notify=False)
            if ok:
                moved += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            logger.error(
                'scan_and_move_expired: ошибка для подписки',
                subscription_id=sub.id,
                error=str(exc),
            )

    logger.info(
        'scan_and_move_expired завершён',
        scanned=scanned,
        moved=moved,
        skipped_dev_mode=skipped_dev,
        skipped_no_remnawave_uuid=skipped_no_uuid,
        failed=failed,
        dev_mode=dev_mode,
        dev_user_count=len(dev_ids),
    )
    return {
        'success': True,
        'error': None,
        'scanned': scanned,
        'moved': moved,
        'skipped_dev_mode': skipped_dev,
        'skipped_no_remnawave_uuid': skipped_no_uuid,
        'failed': failed,
        'dev_mode_active': dev_mode,
        'dev_mode_user_count': len(dev_ids),
    }


async def scan_and_restore_active(db: AsyncSession) -> dict:
    """Сканирует БД и вытаскивает из fallback тех, у кого ЕСТЬ активная подписка.

    Логика на уровне ЮЗЕРА (не подписки), потому что в single-tariff режиме
    несколько подписок одного юзера делят user.remnawave_uuid. Старая
    expired-сабка в fallback блокирует Remnawave доступ для свежей active.

    Алгоритм:
      1. Собираем юзеров у которых ХОТЯ БЫ ОДНА подписка в fallback.
      2. Для каждого такого юзера смотрим ВСЕ его подписки.
      3. Если есть ACTIVE с end_date > now+grace*2.5 → PATCH Remnawave
         в её состояние (squads + expire) + снимаем fallback-флаги
         со ВСЕХ его подписок.

    Возвращает {'scanned_users', 'restored_users', 'skipped_genuine', 'failed'}.
    """
    if not bool(getattr(settings, 'EXPIRY_FALLBACK_ENABLED', False)):
        return {
            'success': False,
            'error': 'EXPIRY_FALLBACK_ENABLED=false',
            'scanned': 0, 'restored': 0, 'skipped_genuine_fallback': 0, 'failed': 0,
        }

    grace_days = _grace_days()
    now = datetime.now(UTC)
    threshold = now + timedelta(days=int(grace_days) + int(grace_days * 1.5))

    # Уникальные user_id у кого есть хотя бы одна подписка в fallback
    affected_users_q = await db.execute(
        select(Subscription.user_id).where(
            or_(
                Subscription.expiry_fallback_active.is_(True),
                Subscription.traffic_fallback_active.is_(True),
            )
        ).distinct()
    )
    affected_user_ids = [row[0] for row in affected_users_q.all()]

    scanned_users = 0
    restored_users = 0
    skipped_genuine = 0
    failed = 0

    for user_id in affected_user_ids:
        scanned_users += 1

        # Все подписки юзера (user eager-load нужен для v3-резолва remna_id)
        all_subs_q = await db.execute(
            select(Subscription)
            .options(selectinload(Subscription.user))
            .where(Subscription.user_id == user_id)
        )
        all_subs = list(all_subs_q.scalars().all())

        # Ищем активную с end_date в будущем
        active_sub = None
        for s in all_subs:
            end_dt = s.end_date
            if end_dt is not None and end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=UTC)
            if (
                s.status == SubscriptionStatus.ACTIVE.value
                and end_dt is not None
                and end_dt > threshold
                and not s.expiry_fallback_active
                and not s.traffic_fallback_active
            ):
                if active_sub is None or (active_sub.end_date and end_dt > active_sub.end_date.replace(tzinfo=UTC)):
                    active_sub = s

        if not active_sub:
            skipped_genuine += 1
            continue

        # PATCH Remnawave в состояние active_sub
        if not (active_sub.remnawave_id or active_sub.remnawave_short_uuid):
            skipped_genuine += 1
            continue

        active_end = active_sub.end_date
        if active_end is not None and active_end.tzinfo is None:
            active_end = active_end.replace(tzinfo=UTC)

        try:
            # db+subscription прокидываем для v3-резолва remna_id через
            # get_panel_user_ref (active_sub.user загружен selectinload'ом выше).
            ok = await _patch_user_full(
                active_sub.remnawave_id,
                squads=active_sub.connected_squads or [],
                expire_at=active_end,
                verify_squad_in=active_sub.connected_squads if active_sub.connected_squads else None,
                db=db,
                subscription=active_sub,
            )
            if not ok:
                failed += 1
                logger.warning(
                    'scan_and_restore_active: PATCH failed',
                    user_id=user_id,
                    active_sub_id=active_sub.id,
                )
                continue

            # Снимаем fallback-флаги со всех подписок юзера
            for s in all_subs:
                if s.expiry_fallback_active or s.traffic_fallback_active:
                    _clear_fallback_state(s)
            await db.commit()

            restored_users += 1
            logger.info(
                'scan_and_restore_active: восстановлен юзер',
                user_id=user_id,
                active_sub_id=active_sub.id,
                end_date=active_end,
                squads=active_sub.connected_squads,
            )
        except Exception as exc:
            failed += 1
            logger.error(
                'scan_and_restore_active: ошибка для юзера',
                user_id=user_id,
                error=str(exc),
            )

    logger.info(
        'scan_and_restore_active завершён',
        scanned_users=scanned_users,
        restored_users=restored_users,
        skipped_genuine_fallback=skipped_genuine,
        failed=failed,
    )
    return {
        'success': True,
        'error': None,
        'scanned': scanned_users,
        'restored': restored_users,
        'skipped_genuine_fallback': skipped_genuine,
        'failed': failed,
    }


async def restore_from_fallback(
    db: AsyncSession,
    subscription: Subscription,
    *,
    new_expire_at: datetime | None = None,
    new_traffic_limit_bytes: int | None = None,
    notify: bool = True,
) -> bool:
    """Возвращает подписку из fallback в исходные сквады/лимиты.

    new_expire_at, new_traffic_limit_bytes — если переданы, используются вместо
    сохранённых оригиналов (при продлении: подписка получила новый end_date).
    notify: False для batch-операций (reconcile), чтобы не выжать DB pool.
    """
    if not subscription.expiry_fallback_active and not subscription.traffic_fallback_active:
        return True

    if not await _has_panel_identity(db, subscription):
        # Панельная идентичность действительно не резолвится (даже через
        # пользователя) — возвращать некуда, снимаем флаги в БД, но громко
        # предупреждаем в логах вместо тихого выхода.
        logger.warning(
            'restore_from_fallback: панельная идентичность не резолвится — '
            'снимаем флаги без обращения к панели',
            subscription_id=subscription.id,
            user_id=subscription.user_id,
        )
        _clear_fallback_state(subscription)
        await db.commit()
        return True

    saved_squads = list(subscription.pre_expiry_squads or [])
    if not saved_squads and settings.DEFAULT_SQUAD_UUID:
        saved_squads = [settings.DEFAULT_SQUAD_UUID]

    # Источник даты — наша БД (могли продлить за время в fallback), а не
    # снимок pre_expiry_expire_at, сделанный в момент переезда в fallback.
    expire_at = new_expire_at or subscription.end_date or subscription.pre_expiry_expire_at
    if expire_at is not None:
        expire_at_aware = expire_at if expire_at.tzinfo is not None else expire_at.replace(tzinfo=UTC)
        if expire_at_aware <= datetime.now(UTC):
            # Панель RemnaWave 3.2.0 отвергает PATCH с expireAt в прошлом
            # (update-user.command.ts: .refine(date > new Date())) — весь
            # запрос упал бы, и сквады/лимит трафика не восстановились бы.
            # Дату для истёкшей подписки всё равно поправит обычная синхронизация,
            # а сквады и лимит важнее восстановить сейчас.
            logger.warning(
                'restore_from_fallback: дата окончания в прошлом, expire_at не отправляется в панель',
                subscription_id=subscription.id,
                expire_at=expire_at_aware.isoformat(),
            )
            expire_at = None

    traffic_limit = new_traffic_limit_bytes
    if traffic_limit is None:
        traffic_limit = subscription.pre_expiry_traffic_limit_bytes

    ok = await _patch_user_full(
        subscription.remnawave_id,
        squads=saved_squads,
        expire_at=expire_at,
        traffic_limit_bytes=traffic_limit,
        verify_squad_in=saved_squads,
        db=db,
        subscription=subscription,
    )
    if not ok:
        return False

    captured_user_id = subscription.user_id
    captured_sub_id = subscription.id
    captured_squads = list(saved_squads)

    _clear_fallback_state(subscription)
    await db.commit()

    logger.info(
        '✅ Подписка возвращена из fallback',
        subscription_id=captured_sub_id,
        user_id=captured_user_id,
        restored_squads=captured_squads,
    )

    # Уведомления в фоне
    if notify:
        _dispatch_fallback_notifications(
            user_id=captured_user_id,
            subscription_id=captured_sub_id,
            action='restored',
            reason='extension',
            original_squads=captured_squads,
            fallback_squad=None,
        )
    return True


def _dispatch_fallback_notifications(
    *,
    user_id: int,
    subscription_id: int,
    action: str,
    reason: str | None,
    original_squads: list[str] | None,
    fallback_squad: str | None,
) -> None:
    """Отправляет админу + юзеру уведомление о переезде в фоне (не блокирует основной поток)."""
    try:
        from app.utils.background_admin_notify import dispatch_generic_admin_notification_bg

        async def _admin_notify(svc, bg_db):
            from app.database.crud.user import get_user_by_id

            u = await get_user_by_id(bg_db, user_id)
            if u:
                await svc.send_subscription_fallback_notification(
                    u,
                    subscription_id,
                    action=action,
                    reason=reason,
                    original_squads=original_squads,
                    fallback_squad=fallback_squad,
                )

        dispatch_generic_admin_notification_bg(_admin_notify)
    except Exception as e:
        logger.error('Не удалось запланировать админ-уведомление о fallback', error=str(e))

    # Уведомление пользователю в TG (если бот не заблокирован)
    try:

        async def _user_notify():
            from aiogram import Bot

            from app.config import settings as app_settings
            from app.database.crud.user import get_user_by_id
            from app.database.database import AsyncSessionLocal

            if not app_settings.BOT_TOKEN:
                return

            # Семафор: ограничиваем параллельность bg-нотификаций, иначе
            # массовая операция (monitoring при старте, ручной scan) спавнит
            # сотни tasks и каждая берёт DB connection — пул выжимается.
            async with _NOTIFY_SEMAPHORE:
                async with AsyncSessionLocal() as bg_db:
                    u = await get_user_by_id(bg_db, user_id)
                    if not u or not u.telegram_id:
                        return

                if action == 'moved':
                    if reason == 'expired':
                        text = (
                            '⚠️ <b>Подписка истекла</b>\n\n'
                            'VPN сейчас работает <b>только для Telegram, банков и кабинета</b>, '
                            'чтобы ты мог продлить.\n\n'
                            'Чтобы вернуть полный доступ — продли подписку.'
                        )
                    else:  # traffic
                        text = (
                            '⚠️ <b>Трафик закончился</b>\n\n'
                            'VPN сейчас работает <b>только для Telegram, банков и кабинета</b>, '
                            'чтобы ты мог докупить трафик.\n\n'
                            'Чтобы вернуть полный доступ — докупи трафик или дождись сброса по периоду.'
                        )
                else:
                    text = '✅ <b>Полный доступ восстановлен</b>\n\nVPN работает в обычном режиме. Спасибо!'

                bot = Bot(token=app_settings.BOT_TOKEN)
                try:
                    await bot.send_message(u.telegram_id, text, parse_mode='HTML')
                except Exception as send_err:
                    logger.debug(
                        'Не удалось отправить TG-уведомление о fallback (вероятно бот заблокирован)',
                        user_id=user_id,
                        error=str(send_err),
                    )
                finally:
                    try:
                        await bot.session.close()
                    except Exception:
                        pass

        asyncio.create_task(_user_notify())
    except RuntimeError:
        pass
    except Exception as e:
        logger.error('Не удалось запланировать TG-уведомление о fallback', error=str(e))


def _clear_fallback_state(subscription: Subscription) -> None:
    """Сбрасывает все флаги/baseline fallback в подписке."""
    subscription.expiry_fallback_active = False
    subscription.traffic_fallback_active = False
    subscription.pre_expiry_squads = None
    subscription.pre_expiry_expire_at = None
    subscription.pre_expiry_traffic_limit_bytes = None
    subscription.expiry_fallback_started_at = None


async def clear_fallback_after_purchase(db: AsyncSession, subscription: Subscription) -> bool:
    """Снимает fallback-флаги после успешной покупки/продления.

    ВНИМАНИЕ: НЕ трогает панель — предполагает, что Remnawave уже
    синхронизирована с правильными сквадами. Это допущение оказалось неверным:
    при пустом ``subscription.connected_squads`` create/update_remnawave_user
    пропускают отправку ``active_internal_squads`` (пустой список ложен в
    Python), PATCH уходит частичным, и панель оставляет юзера в
    fallback-скваде — а эта функция снимала флаги, делая подписку невидимой
    для reconcile. Используйте ``restore_fallback_after_purchase`` в местах
    покупки/продления вместо этой функции. Оставлена для обратной
    совместимости и на случай, когда панель гарантированно уже приведена
    в правильное состояние в этой же транзакции.
    """
    if not subscription.expiry_fallback_active and not subscription.traffic_fallback_active:
        return False
    _clear_fallback_state(subscription)
    await db.commit()
    logger.info(
        '✅ Сняты fallback-флаги после покупки/продления',
        subscription_id=subscription.id,
        user_id=subscription.user_id,
    )
    return True


async def restore_fallback_after_purchase(db: AsyncSession, subscription: Subscription) -> bool:
    """Восстанавливает сквады в панели после покупки/продления, если подписка была в fallback.

    Замена ``clear_fallback_after_purchase`` в местах покупки/продления
    (``subscription_purchase_service.py``, ``handlers/subscription/purchase.py``,
    ``cabinet/routes/subscription_modules/purchase.py``): та функция снимала
    fallback-флаги без проверки, что панель действительно вернула юзера в
    рабочие сквады. Здесь переиспользуется уже проверенный
    ``restore_from_fallback`` — тот же путь, что и у докупки трафика, ручного
    возврата админом и вебхука. Флаги снимаются только при подтверждённом
    PATCH; если стоящих fallback-флагов нет — no-op (идентично прежнему
    поведению).
    """
    return await restore_from_fallback(db, subscription)


# ============================================================================
# Periodic reconcile — устойчивость к потерянным вебхукам и внешним правкам
# ============================================================================


async def reconcile_fallback_subscriptions(db: AsyncSession) -> dict:
    """Периодическая сверка fallback-подписок с реальным состоянием Remnawave.

    Запускается из monitoring-цикла (например раз в 15 минут).

    Делает три вещи:
    1. Для подписок с active fallback в нашей БД:
       - Если в Remnawave юзер уже не в fallback-скваде → admin вручную сменил → mark returned
       - Если expireAt в Remnawave вырос больше baseline+grace → внешнее продление → restore
       - Если trafficLimitBytes вырос больше baseline+grace → внешнее пополнение → restore
       - Если grace expireAt уже прошёл и юзер всё ещё в fallback → grace истёк → отключаем
       - Если суммарно сидит больше EXPIRY_FALLBACK_DAYS → полностью отключаем

    2. Для подписок со статусом EXPIRED/LIMITED в нашей БД, но БЕЗ active fallback:
       - Скорее всего вебхук потерялся → переводим в fallback (если фича включена)

    Возвращает {'restored': N, 'extended': N, 'moved': N, 'cleaned': N, 'errors': N}.
    """
    if not _is_fallback_enabled():
        return {'skipped': True}

    stats = {
        'restored_external': 0,
        'restored_squad_changed': 0,
        'extended_grace': 0,
        'moved_lost_webhook': 0,
        'cleaned_total_expired': 0,
        'disabled_grace_expired': 0,
        'restored_stuck_no_flags': 0,
        'errors': 0,
    }

    fallback_uuid = settings.EXPIRY_FALLBACK_SQUAD_UUID
    grace_days = _grace_days()
    grace_gb = _grace_gb()
    total_days = _total_fallback_days()
    require_zero_balance = bool(getattr(settings, 'EXPIRED_CLEANUP_REQUIRE_ZERO_BALANCE', True))
    cleanup_enabled = bool(getattr(settings, 'EXPIRED_CLEANUP_ENABLED', False))
    grace_extension_threshold_hours = 1  # Не чаще раза в час продлеваем grace

    now = datetime.now(UTC)

    # ----------------------------------------------------------------
    # 1. Сверяем все active fallback-подписки
    # ----------------------------------------------------------------
    result = await db.execute(
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(
            or_(
                Subscription.expiry_fallback_active.is_(True),
                Subscription.traffic_fallback_active.is_(True),
            )
        )
    )
    active_fallback = list(result.scalars().all())

    for sub in active_fallback:
        try:
            await _reconcile_single_active_fallback(
                db, sub, fallback_uuid, grace_days, grace_gb,
                total_days, require_zero_balance, cleanup_enabled,
                grace_extension_threshold_hours, now, stats,
            )
        except Exception as exc:
            stats['errors'] += 1
            logger.error('Reconcile error для подписки', subscription_id=sub.id, error=str(exc))

    # ----------------------------------------------------------------
    # 2. Подбираем «потерянные» подписки (вебхук не дошёл)
    # ----------------------------------------------------------------
    lost_result = await db.execute(
        select(Subscription)
        .options(selectinload(Subscription.user))  # user нужен для v3-резолва remna_id
        .where(
            and_(
                Subscription.status.in_([
                    SubscriptionStatus.EXPIRED.value,
                    SubscriptionStatus.LIMITED.value,
                ]),
                Subscription.expiry_fallback_active.is_(False),
                Subscription.traffic_fallback_active.is_(False),
                or_(
                    Subscription.remnawave_id.is_not(None),
                    Subscription.remnawave_short_uuid.is_not(None),
                ),
            )
        )
        .limit(50)  # за один цикл не больше 50 чтобы не перегрузить
    )
    lost_subs = list(lost_result.scalars().all())

    for sub in lost_subs:
        try:
            # Защита от повторного загона: если админ уже вытащил юзера в панели
            # (or дал ему доступ через продление), пропускаем — не отменяем admin action.
            # db+subscription прокидываем для v3-резолва remna_id (sub.user загружен
            # selectinload'ом в запросе lost_subs выше).
            rw_user = await _get_remnawave_user(sub.remnawave_id, db=db, subscription=sub)
            if rw_user is not None:
                current_squads = set(
                    _extract_squad_uuids(getattr(rw_user, 'active_internal_squads', None))
                )
                current_expire_at = getattr(rw_user, 'expire_at', None)

                # 1) Юзер уже в НЕ-fallback скваде → админ вручную перевёл → не трогаем
                non_fallback_squads = current_squads - {fallback_uuid}
                if non_fallback_squads and fallback_uuid not in current_squads:
                    logger.info(
                        'Reconcile path2 skip: юзер уже в обычном скваде (admin action)',
                        subscription_id=sub.id,
                        user_id=sub.user_id,
                        current_squads=sorted(current_squads),
                    )
                    continue

                # 2) expire_at в Remnawave далеко в будущем → админ продлил в панели
                if current_expire_at and current_expire_at > now + timedelta(days=int(grace_days) + int(grace_days * 1.5)):
                    logger.info(
                        'Reconcile path2 skip: expire_at в панели в будущем (admin extension)',
                        subscription_id=sub.id,
                        user_id=sub.user_id,
                        panel_expire_at=current_expire_at,
                    )
                    continue

            reason = 'expired' if sub.status == SubscriptionStatus.EXPIRED.value else 'traffic'
            # notify=False: reconcile подбирает до 50 подписок за цикл,
            # массовые bg notify съедают DB pool (см. fallback_service docs выше).
            ok = await move_to_fallback(db, sub, reason=reason, notify=False)
            if ok:
                stats['moved_lost_webhook'] += 1
                logger.info(
                    'Reconcile: перенесли «потерявшуюся» подписку в fallback',
                    subscription_id=sub.id,
                    reason=reason,
                )
        except Exception as exc:
            stats['errors'] += 1
            logger.error('Reconcile move error', subscription_id=sub.id, error=str(exc))

    # ----------------------------------------------------------------
    # 3. Панель-истина: восстанавливаем ВСЕХ реально застрявших в
    #    fallback-скваде активных оплаченных подписок.
    #
    #    Косвенные признаки БД ненадёжны и оставляли дыры:
    #    - застрявший после оплаты имеет status=ACTIVE, флаги fallback СНЯТЫ
    #      и connected_squads НЕПУСТОЙ (restore обновил БД, но PATCH сквадов в
    #      панель не дошёл) — прежняя эвристика «пустой connected_squads» его
    #      не видела, и юзер висел без доступа до ручного прогона repair-скрипта;
    #    - застрявший через автопокупку имеет флаги ещё поднятыми.
    #    Единственный надёжный источник — сама панель RemnaWave: читаем её один
    #    раз и берём тех, кто фактически сидит в fallback-скваде. Ветки 1–2 уже
    #    отработали и закоммитили свои restore выше, поэтому к этому моменту
    #    панель актуальна и повторно их не заденем (их уже нет в fallback-скваде).
    # ----------------------------------------------------------------
    try:
        from app.services.remnawave_service import remnawave_service

        async with remnawave_service.get_api_client() as api:
            panel_users = await api.get_all_users_stream(size=500)
        fallback_ids = sorted(build_fallback_squad_ids(panel_users, fallback_uuid))
    except Exception as exc:
        fallback_ids = []
        logger.error('Reconcile: не удалось прочитать fallback-сквад панели для sweep', error=str(exc))

    for id_chunk in _chunked(fallback_ids, 500):
        stuck_result = await db.execute(
            select(Subscription)
            .options(selectinload(Subscription.user))
            .where(
                and_(
                    Subscription.status == SubscriptionStatus.ACTIVE.value,
                    Subscription.end_date > now,
                    Subscription.remnawave_id.in_(id_chunk),
                )
            )
        )
        for sub in stuck_result.scalars().all():
            try:
                target_squads = choose_target_squads(
                    sub.connected_squads,
                    settings.DEFAULT_SQUAD_UUID,
                    pre_expiry_squads=sub.pre_expiry_squads,
                )
                if not target_squads:
                    logger.warning(
                        'Reconcile stuck-panel: нет цели восстановления '
                        '(ни connected_squads, ни pre_expiry_squads, ни DEFAULT_SQUAD_UUID)',
                        subscription_id=sub.id,
                    )
                    continue

                ok = await _patch_user_full(
                    sub.remnawave_id,
                    squads=target_squads,
                    verify_squad_in=target_squads,
                    db=db,
                    subscription=sub,
                )
                if ok:
                    # Флаги снимаем ТОЛЬКО после подтверждённого панелью PATCH —
                    # иначе повторяем исходный баг (сняли флаги, а панель в fallback).
                    _clear_fallback_state(sub)
                    await db.commit()
                    stats['restored_stuck_no_flags'] += 1
                    logger.info(
                        '✅ Reconcile: восстановлена застрявшая в fallback-скваде подписка (panel-truth)',
                        subscription_id=sub.id,
                        user_id=sub.user_id,
                        restored_squads=target_squads,
                    )
                else:
                    stats['errors'] += 1
            except Exception as exc:
                stats['errors'] += 1
                logger.error('Reconcile stuck-panel error', subscription_id=sub.id, error=str(exc))

    if any(v > 0 for v in stats.values() if isinstance(v, int)):
        logger.info('🔁 Reconcile fallback подписок завершён', **stats)
    return stats


async def _disable_and_expire_fallback(
    db: AsyncSession,
    sub: Subscription,
    stats: dict,
    *,
    stat_key: str,
    log_msg: str,
    **log_kw,
) -> bool:
    """Отключает юзера в панели, ставит EXPIRED, сбрасывает fallback-состояние и коммитит.

    Идемпотентен: already-disabled / not-found / 404 в панели → debug, не ошибка.
    Обновляет ``stats[stat_key]`` при успехе, ``stats['errors']`` при исключении.
    Возвращает True при успехе, False при ошибке.
    """
    from app.external.remnawave_api import RemnaWaveAPIError
    from app.services.remnawave_service import remnawave_service
    try:
        if sub.remnawave_id or sub.remnawave_short_uuid:
            from app.services.remnawave_service import get_panel_user_ref
            async with remnawave_service.get_api_client() as api:
                _uuid, _remna_id = await get_panel_user_ref(
                    api, db, subscription=sub, user=sub.user,
                )
                try:
                    if _remna_id is not None:
                        await api.disable_user(_remna_id)
                except RemnaWaveAPIError as api_exc:
                    # Идемпотентность: юзер уже DISABLED в Remnawave
                    # (предыдущий cleanup, ручная блокировка админа,
                    # 404 — юзер удалён в панели). Не ошибка, всё ок.
                    msg = str(api_exc).lower()
                    if (
                        'already disabled' in msg
                        or 'not found' in msg
                        or getattr(api_exc, 'status_code', None) == 404
                    ):
                        logger.debug(
                            'Reconcile cleanup: юзер уже DISABLED/удалён в Remnawave, продолжаем',
                            subscription_id=sub.id,
                            reason=msg[:120],
                        )
                    else:
                        raise
        sub.status = SubscriptionStatus.EXPIRED.value
        _clear_fallback_state(sub)
        await db.commit()
        stats[stat_key] += 1
        logger.info(log_msg, subscription_id=sub.id, **log_kw)
        return True
    except Exception as exc:
        stats['errors'] += 1
        logger.error('Reconcile cleanup error', subscription_id=sub.id, error=str(exc))
        return False


async def _reconcile_single_active_fallback(
    db: AsyncSession,
    sub: Subscription,
    fallback_uuid: str,
    grace_days: int,
    grace_gb: int,
    total_days: int,
    require_zero_balance: bool,
    cleanup_enabled: bool,
    grace_extension_threshold_hours: int,
    now: datetime,
    stats: dict,
) -> None:
    """Логика сверки одной fallback-подписки. Вызывается из reconcile_fallback_subscriptions."""

    # Полная очистка если суммарно висит больше total_days
    if (
        cleanup_enabled
        and sub.expiry_fallback_started_at
        and (now - sub.expiry_fallback_started_at) >= timedelta(days=total_days)
    ):
        user = sub.user
        if not require_zero_balance or (user and (user.balance_kopeks or 0) == 0):
            await _disable_and_expire_fallback(
                db, sub, stats,
                stat_key='cleaned_total_expired',
                log_msg='Reconcile: подписка висит в fallback больше total_days, отключаем',
                days=total_days,
            )
            return

    if not await _has_panel_identity(db, sub):
        return

    # 0) Подписка в БД реально активна (admin/cabinet/бот продлили) — restore немедленно.
    # Это страховка от случая когда PATCH в Remnawave при продлении не дошёл или
    # флаг fallback не снялся: доверяем нашему end_date, а не панели.
    # Условие: статус ACTIVE и end_date достаточно вперёд (больше нашего grace ceiling),
    # чтобы исключить случайные совпадения.
    sub_end = sub.end_date
    if sub_end is not None and sub_end.tzinfo is None:
        sub_end = sub_end.replace(tzinfo=UTC)
    if (
        sub.status == SubscriptionStatus.ACTIVE.value
        and sub_end is not None
        and sub_end > now + timedelta(days=int(grace_days) + int(grace_days * 1.5))
    ):
        ok = await restore_from_fallback(db, sub, new_expire_at=sub_end, notify=False)
        if ok:
            stats['restored_external'] += 1
            logger.info(
                'Reconcile: подписка в БД активна (продлена) — restore из fallback',
                subscription_id=sub.id,
                user_id=sub.user_id,
                db_end_date=sub_end,
                fallback_started_at=sub.expiry_fallback_started_at,
            )
        return

    rw_user = await _get_remnawave_user(sub.remnawave_id, db=db, subscription=sub)
    if not rw_user:
        return

    current_squads = set(_extract_squad_uuids(getattr(rw_user, 'active_internal_squads', None)))
    in_fallback = fallback_uuid in current_squads
    current_expire_at = getattr(rw_user, 'expire_at', None)
    current_traffic_limit = getattr(rw_user, 'traffic_limit_bytes', None) or 0

    # 1) Юзера руками вытащили из fallback в панели — синхронизируем БД
    if not in_fallback:
        _clear_fallback_state(sub)
        await db.commit()
        stats['restored_squad_changed'] += 1
        logger.info(
            'Reconcile: юзера вытащили из fallback вручную в панели — снимаем флаги',
            subscription_id=sub.id,
            current_squads=sorted(current_squads),
        )
        return

    # 2) Внешнее продление через панель Remnawave (admin вручную увеличил expireAt).
    # ВАЖНО: сравниваем НЕ с baseline_expire_at (старая дата ДО перевода в fallback),
    # а с тем, что мы САМИ поставили в Remnawave при move_to_fallback (now + grace).
    # Иначе для подписок, истёкших давно, наш собственный grace (now+5d) автоматически
    # превышал baseline+buffer и reconcile ошибочно считал это «внешним продлением» →
    # массово вытаскивал юзеров из fallback с end_date = now+grace_days.
    if current_expire_at:
        # Внешнее продление = expire_at в Remnawave заметно больше наших grace+buffer
        # (т.е. admin продлил юзера в панели минимум на ~7 дней вперёд от наших grace).
        our_grace_ceiling = now + timedelta(days=int(grace_days) + int(grace_days * 1.5))
        if current_expire_at > our_grace_ceiling:
            ok = await restore_from_fallback(
                db, sub, new_expire_at=current_expire_at, notify=False
            )
            if ok:
                stats['restored_external'] += 1
                logger.info(
                    'Reconcile: обнаружено внешнее продление через панель — restore',
                    subscription_id=sub.id,
                    baseline_expire=sub.pre_expiry_expire_at,
                    current_expire=current_expire_at,
                    threshold=our_grace_ceiling,
                )
            return

    # 3) Внешнее пополнение трафика через панель
    baseline_traffic = sub.pre_expiry_traffic_limit_bytes
    if (
        sub.traffic_fallback_active
        and baseline_traffic is not None
        and current_traffic_limit > 0
    ):
        # Мы поставили baseline + grace_gb. Если admin увеличил больше — restore.
        expected_limit = int(baseline_traffic) + grace_gb * (1024 ** 3)
        if current_traffic_limit > expected_limit + (1024 ** 3):  # buffer 1GB
            ok = await restore_from_fallback(
                db, sub, new_traffic_limit_bytes=current_traffic_limit, notify=False
            )
            if ok:
                stats['restored_external'] += 1
                logger.info(
                    'Reconcile: обнаружено внешнее пополнение трафика — restore',
                    subscription_id=sub.id,
                    baseline=baseline_traffic,
                    current=current_traffic_limit,
                )
            return

    # 4) Grace истёк и юзер не оплатил — отключаем.
    # Grace задаётся ОДНАЖДЫ при переводе в fallback (move_to_fallback → now+grace_days)
    # и больше не продлевается: истёк срок → отключение.
    if sub.expiry_fallback_started_at is not None:
        started_at = sub.expiry_fallback_started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        if (now - started_at) >= timedelta(days=grace_days):
            user = sub.user
            if not require_zero_balance or (user and (user.balance_kopeks or 0) == 0):
                await _disable_and_expire_fallback(
                    db, sub, stats,
                    stat_key='disabled_grace_expired',
                    log_msg='Reconcile: grace истёк без оплаты — отключаем',
                    grace_days=grace_days,
                    started_at=sub.expiry_fallback_started_at,
                )
