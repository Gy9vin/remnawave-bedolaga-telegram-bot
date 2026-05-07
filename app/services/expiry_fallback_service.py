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

Через час reconcile продлевает grace заново, если юзер всё ещё в fallback.
Так дата в Remnawave остаётся реалистичной (не «+10 лет»), и видно сколько
времени юзер сидит в reserved.

Полное отключение происходит через `EXPIRY_FALLBACK_TOTAL_DAYS` (90 дней)
после `expiry_fallback_started_at`.

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

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import Subscription, SubscriptionStatus, User


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
    verify_squad_in: list[str] | None = None,
) -> bool:
    """Обновляет юзера в Remnawave и при ошибке/conflict проверяет реальное состояние.

    verify_squad_in — UUID(ы) ожидаемых сквадов для верификации. Если PATCH вернул
    409 или ошибку, мы делаем get_user и проверяем не применилось ли уже.
    """
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
        # Conflict 409 verify: возможно изменения уже применились
        err_text = str(exc).lower()
        is_conflict = '409' in err_text or 'conflict' in err_text
        if is_conflict and verify_squad_in:
            try:
                async with remnawave_service.get_api_client() as api:
                    verified = await api.get_user_by_uuid(remnawave_uuid)
                if verified:
                    actual = set(_extract_squad_uuids(getattr(verified, 'active_internal_squads', None)))
                    expected = set(verify_squad_in)
                    if expected.issubset(actual) or actual == expected:
                        logger.info(
                            '409 conflict, но сквад уже применён — считаем успехом',
                            remnawave_uuid=remnawave_uuid,
                            actual=sorted(actual),
                        )
                        return True
            except Exception:
                pass
        logger.error(
            'Ошибка обновления юзера в Remnawave',
            remnawave_uuid=remnawave_uuid,
            squads=squads,
            expire_at=expire_at,
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
) -> bool:
    """Переводит подписку в fallback-сквад с grace-периодом.

    reason: 'expired' | 'traffic'

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

    if not subscription.remnawave_uuid:
        logger.warning('Нет remnawave_uuid у подписки', subscription_id=subscription.id)
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
    rw_user = await _get_remnawave_user(subscription.remnawave_uuid)
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
        subscription.remnawave_uuid,
        squads=[fallback_uuid],
        expire_at=new_expire_at,
        traffic_limit_bytes=new_traffic_limit,
        verify_squad_in=[fallback_uuid],
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
        grace_days=_grace_days(),
    )

    # Уведомления в фоне (админам + юзеру)
    _dispatch_fallback_notifications(
        user_id=subscription.user_id,
        subscription_id=subscription.id,
        action='moved',
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
    сохранённых оригиналов (при продлении: подписка получила новый end_date).
    """
    if not subscription.expiry_fallback_active and not subscription.traffic_fallback_active:
        return True

    if not subscription.remnawave_uuid:
        # Просто снимаем флаги в БД
        _clear_fallback_state(subscription)
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
        verify_squad_in=saved_squads,
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
        import asyncio

        async def _user_notify():
            from aiogram import Bot

            from app.config import settings as app_settings
            from app.database.crud.user import get_user_by_id
            from app.database.database import AsyncSessionLocal

            if not app_settings.BOT_TOKEN:
                return

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
       - Если grace expireAt уже прошёл и юзер всё ещё в fallback → продлеваем grace заново
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
        .where(
            and_(
                Subscription.status.in_([
                    SubscriptionStatus.EXPIRED.value,
                    SubscriptionStatus.LIMITED.value,
                ]),
                Subscription.expiry_fallback_active.is_(False),
                Subscription.traffic_fallback_active.is_(False),
                Subscription.remnawave_uuid.is_not(None),
            )
        )
        .limit(50)  # за один цикл не больше 50 чтобы не перегрузить
    )
    lost_subs = list(lost_result.scalars().all())

    for sub in lost_subs:
        try:
            reason = 'expired' if sub.status == SubscriptionStatus.EXPIRED.value else 'traffic'
            ok = await move_to_fallback(db, sub, reason=reason)
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

    if any(v > 0 for v in stats.values() if isinstance(v, int)):
        logger.info('🔁 Reconcile fallback подписок завершён', **stats)
    return stats


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
            from app.services.remnawave_service import remnawave_service
            try:
                if sub.remnawave_uuid:
                    async with remnawave_service.get_api_client() as api:
                        await api.disable_user(sub.remnawave_uuid)
                sub.status = SubscriptionStatus.EXPIRED.value
                _clear_fallback_state(sub)
                await db.commit()
                stats['cleaned_total_expired'] += 1
                logger.info(
                    'Reconcile: подписка висит в fallback больше total_days, отключаем',
                    subscription_id=sub.id,
                    days=total_days,
                )
                return
            except Exception as exc:
                stats['errors'] += 1
                logger.error('Reconcile cleanup error', subscription_id=sub.id, error=str(exc))
                return

    if not sub.remnawave_uuid:
        return

    rw_user = await _get_remnawave_user(sub.remnawave_uuid)
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

    # 2) Внешнее продление через панель Remnawave (admin вручную увеличил expireAt)
    baseline_expire_at = sub.pre_expiry_expire_at
    if baseline_expire_at and current_expire_at:
        # Грейс который мы поставили = now + grace_days в момент move_to_fallback.
        # Если current_expire_at заметно больше — внешнее продление.
        # Допустим buffer = grace_days * 1.5 (т.е. админ продлил минимум на ~5 дней)
        buffer = timedelta(days=int(grace_days * 1.5))
        external_renewal_threshold = baseline_expire_at + buffer
        if current_expire_at > external_renewal_threshold:
            ok = await restore_from_fallback(db, sub, new_expire_at=current_expire_at)
            if ok:
                stats['restored_external'] += 1
                logger.info(
                    'Reconcile: обнаружено внешнее продление через панель — restore',
                    subscription_id=sub.id,
                    baseline_expire=baseline_expire_at,
                    current_expire=current_expire_at,
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
            ok = await restore_from_fallback(db, sub, new_traffic_limit_bytes=current_traffic_limit)
            if ok:
                stats['restored_external'] += 1
                logger.info(
                    'Reconcile: обнаружено внешнее пополнение трафика — restore',
                    subscription_id=sub.id,
                    baseline=baseline_traffic,
                    current=current_traffic_limit,
                )
            return

    # 4) Grace expireAt подходит к концу — продлеваем заново
    if current_expire_at and current_expire_at - now < timedelta(days=1):
        # Не чаще раза в час
        last_extension = sub.expiry_fallback_started_at  # используем как метку последнего extend
        # На самом деле это «когда начали» — но пока что используем как индикатор
        # для предотвращения слишком частых продлений.
        # Лучше отдельное поле, но пока обходимся.
        new_grace_expire_at = now + timedelta(days=grace_days)
        ok = await _patch_user_full(
            sub.remnawave_uuid,
            squads=[fallback_uuid],
            expire_at=new_grace_expire_at,
            traffic_limit_bytes=int(current_traffic_limit) if current_traffic_limit else None,
            verify_squad_in=[fallback_uuid],
        )
        if ok:
            stats['extended_grace'] += 1
            logger.info(
                'Reconcile: продлили grace fallback',
                subscription_id=sub.id,
                new_expire=new_grace_expire_at,
            )
