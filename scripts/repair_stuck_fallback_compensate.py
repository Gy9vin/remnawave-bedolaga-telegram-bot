#!/usr/bin/env python
"""Компенсирует дни пользователям, оплатившим продление но застрявшим в fallback-скваде.

ПРОБЛЕМА
========

Исторический баг (до коммита 9c8c01fa): при продлении подписки с непустым
connected_squads флаги fallback снимались без восстановления сквадов в панели
RemnaWave. После этого состояние такого пользователя:

  - status=ACTIVE, expiry_fallback_active=False, traffic_fallback_active=False
  - connected_squads=[...] (непустой — от тарифа, появился после застревания)
  - В панели RemnaWave: active_internal_squads=[EXPIRY_FALLBACK_SQUAD_UUID]

Этих пользователей не видит ни repair_stuck_fallback.py (требует флаги),
ни reconcile раздел 3 (требует пустой connected_squads). Они застревают навсегда.

ЧТО ДЕЛАЕТ СКРИПТ
=================

DRY-RUN (по умолчанию):
  - Находит кандидатов (оплатили + в fallback-скваде в панели)
  - Печатает таблицу: telegram_id, дата платежа, сумма ₽, lost_days,
    текущий end_date → новый end_date, целевые сквады
  - Ничего не меняет, ничего не отправляет

--apply:
  - extend_subscription(+lost_days+5) → обновляет end_date в БД
  - _patch_user_full(squads=target_squads, expire_at=new_end_date) → один PATCH в RemnaWave
  - send_notification(SUBSCRIPTION_RENEWED) → Telegram и/или email

БЕЗОПАСНОСТЬ
============

При --apply каждый пользователь обрабатывается в отдельном try/except.
Если панель недоступна (_get_remnawave_user вернул None) — пользователь
пропускается, компенсация НЕ начисляется.

Usage:
    python -m scripts.repair_stuck_fallback_compensate            # dry-run
    python -m scripts.repair_stuck_fallback_compensate --apply    # применить
    python -m scripts.repair_stuck_fallback_compensate --limit 10
    python -m scripts.repair_stuck_fallback_compensate --user-ids 1,2,3
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.subscription import extend_subscription
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionStatus, Transaction, TransactionType
from app.services.expiry_fallback_service import _extract_squad_uuids, _get_remnawave_user, _patch_user_full
from app.services.notification_delivery_service import NotificationType, notification_delivery_service
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)

# Пауза между обращениями к панели — чтобы не заваливать RemnaWave.
_SLEEP_BETWEEN = 0.3


# ============================================================================
# Чистая логика (тестируется без БД и панели)
# ============================================================================


def calc_lost_days(renewal_ts: datetime, now: datetime | None = None) -> int:
    """Количество дней от оплаты продления до текущего момента.

    Минимум 1 день — чтобы не выдавать 0 при свежем застревании.
    """
    if now is None:
        now = datetime.now(UTC)
    delta = now - renewal_ts
    return max(1, delta.days)


def choose_target_squads(
    connected_squads: list[str] | None,
    default_squad_uuid: str | None,
) -> list[str]:
    """Выбирает целевые сквады для восстановления.

    Приоритет: connected_squads (непустой) → [DEFAULT_SQUAD_UUID].
    Если оба отсутствуют — возвращает пустой список (кандидат будет пропущен).
    """
    if connected_squads:
        return list(connected_squads)
    if default_squad_uuid:
        return [default_squad_uuid]
    return []


# ============================================================================
# Запросы к БД
# ============================================================================


async def _fetch_candidates(
    db: AsyncSession,
    *,
    user_ids: list[int] | None,
    limit: int | None,
) -> list[Subscription]:
    """Подписки-кандидаты: ACTIVE, без fallback-флагов, с remnawave_id."""
    conditions = [
        Subscription.status == SubscriptionStatus.ACTIVE.value,
        Subscription.expiry_fallback_active == False,  # noqa: E712
        Subscription.traffic_fallback_active == False,  # noqa: E712
        Subscription.remnawave_id.isnot(None),
    ]
    if user_ids:
        conditions.append(Subscription.user_id.in_(user_ids))

    q = (
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(*conditions)
        .order_by(Subscription.id.asc())
    )
    if limit is not None:
        q = q.limit(limit)

    result = await db.execute(q)
    return list(result.scalars().all())


async def _find_last_renewal_transaction(
    db: AsyncSession,
    user_id: int,
) -> Transaction | None:
    """Последняя успешная транзакция оплаты подписки для данного user_id."""
    result = await db.execute(
        select(Transaction)
        .where(
            Transaction.user_id == user_id,
            Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
            Transaction.is_completed == True,  # noqa: E712
        )
        .order_by(Transaction.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ============================================================================
# Проверка панели
# ============================================================================


async def _is_in_fallback_squad(sub: Subscription, db: AsyncSession) -> bool:
    """True, если в панели у юзера активен EXPIRY_FALLBACK_SQUAD_UUID."""
    fallback_uuid = settings.EXPIRY_FALLBACK_SQUAD_UUID
    if not fallback_uuid:
        return False

    panel_user = await _get_remnawave_user(sub.remnawave_id, db=db, subscription=sub)
    if panel_user is None:
        return False

    active_squads = _extract_squad_uuids(getattr(panel_user, 'active_internal_squads', None))
    return fallback_uuid in active_squads


# ============================================================================
# Вспомогательный вывод
# ============================================================================


def _print_dry_run_table(rows: list[dict[str, Any]]) -> None:
    print()
    print('=' * 95)
    print('  DRY-RUN — ничего не изменено')
    print('=' * 95)
    if not rows:
        print('  Кандидатов не найдено.')
    else:
        header = (
            f"  {'user_id':>8}  {'tg_id':>12}  {'дата платежа':<13}  {'сумма':>8}  "
            f"{'lost_d':>6}  {'текущий end':>12}  {'новый end':>12}  сквады"
        )
        print()
        print(header)
        print('  ' + '-' * 91)
        for r in rows:
            squads_short = ','.join(str(s)[:8] for s in (r['target_squads'] or [])[:2]) or '—'
            print(
                f"  {r['user_id']:>8}  {str(r.get('telegram_id') or '—'):>12}  "
                f"{r['paid_at']:<13}  {r['amount_rub']:>7.0f}₽  "
                f"{r['lost_days']:>6}  {r['end_date_cur']:>12}  {r['end_date_new']:>12}  "
                f"{squads_short}"
            )
    print()
    print(f'  Итого кандидатов: {len(rows)}')
    print()
    print('  DRY-RUN, изменения не применялись; для применения запусти с --apply')
    print('=' * 95)
    print()


def _build_notification_text(lost_days: int, renewal_ts: datetime, new_end_date: datetime) -> str:
    return (
        f'✅ Мы исправили техническую ошибку: вы оставались в резервном скваде после оплаты '
        f'{renewal_ts.strftime("%d.%m.%Y")}. '
        f'Вернули {lost_days} потерянных дней и начислили бонус 5 дней в качестве извинений. '
        f'Новая дата окончания подписки: {new_end_date.strftime("%d.%m.%Y")}. '
        f'Приятного пользования!'
    )


# ============================================================================
# Основной цикл
# ============================================================================


async def _run(*, apply: bool, limit: int | None, user_ids: list[int] | None) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)

    now = datetime.now(UTC)
    errors = 0
    applied = 0
    skipped = 0
    dry_run_rows: list[dict[str, Any]] = []

    bot: Bot | None = None
    if apply:
        bot = Bot(token=settings.BOT_TOKEN)

    try:
        async with AsyncSessionLocal() as db:
            candidates = await _fetch_candidates(db, user_ids=user_ids, limit=limit)
            logger.info(
                'repair_stuck_fallback_compensate: загружено подписок-кандидатов',
                count=len(candidates),
                apply=apply,
            )

            for sub in candidates:
                try:
                    # 1. Найти последнюю успешную транзакцию продления
                    txn = await _find_last_renewal_transaction(db, sub.user_id)
                    if txn is None:
                        logger.debug(
                            'repair_stuck_fallback_compensate: нет транзакции — пропускаем',
                            subscription_id=sub.id,
                            user_id=sub.user_id,
                        )
                        skipped += 1
                        continue

                    # 2. Проверить панель RemnaWave (обязательно — не начисляем вслепую)
                    in_fallback = await _is_in_fallback_squad(sub, db)
                    if not in_fallback:
                        logger.debug(
                            'repair_stuck_fallback_compensate: не в fallback-скваде — пропускаем',
                            subscription_id=sub.id,
                            user_id=sub.user_id,
                        )
                        skipped += 1
                        continue

                    # 3. Рассчитать lost_days и целевые сквады
                    renewal_ts = txn.completed_at or txn.created_at
                    lost_days = calc_lost_days(renewal_ts, now)
                    target_squads = choose_target_squads(sub.connected_squads, settings.DEFAULT_SQUAD_UUID)

                    if not target_squads:
                        logger.warning(
                            'repair_stuck_fallback_compensate: нет целевых сквадов — пропускаем '
                            '(connected_squads пустой и DEFAULT_SQUAD_UUID не задан)',
                            subscription_id=sub.id,
                            user_id=sub.user_id,
                        )
                        skipped += 1
                        continue

                    total_days = lost_days + 5
                    end_date_cur = sub.end_date
                    new_end_date_preview = end_date_cur + timedelta(days=total_days)

                    if not apply:
                        dry_run_rows.append({
                            'user_id': sub.user_id,
                            'telegram_id': getattr(sub.user, 'telegram_id', None),
                            'paid_at': renewal_ts.strftime('%Y-%m-%d'),
                            'amount_rub': txn.amount_rubles,
                            'lost_days': lost_days,
                            'end_date_cur': end_date_cur.strftime('%Y-%m-%d'),
                            'end_date_new': new_end_date_preview.strftime('%Y-%m-%d'),
                            'target_squads': target_squads,
                        })
                        continue

                    # --- APPLY ---
                    logger.info(
                        'repair_stuck_fallback_compensate: применяем компенсацию',
                        subscription_id=sub.id,
                        user_id=sub.user_id,
                        lost_days=lost_days,
                        total_days=total_days,
                        target_squads=target_squads,
                        renewal_ts=str(renewal_ts),
                    )

                    # Продлить подписку в БД (без коммита — коммитим после patch)
                    sub = await extend_subscription(db, sub, days=total_days, commit=False)

                    # Обновить панель RemnaWave: сквады + новая дата
                    patch_ok = await _patch_user_full(
                        sub.remnawave_id,
                        squads=target_squads,
                        expire_at=sub.end_date,
                        verify_squad_in=target_squads,
                        db=db,
                        subscription=sub,
                    )
                    if not patch_ok:
                        logger.error(
                            'repair_stuck_fallback_compensate: _patch_user_full вернул False — '
                            'откат транзакции',
                            subscription_id=sub.id,
                            user_id=sub.user_id,
                        )
                        await db.rollback()
                        errors += 1
                        continue

                    await db.commit()
                    applied += 1
                    logger.info(
                        'repair_stuck_fallback_compensate: компенсация применена',
                        subscription_id=sub.id,
                        user_id=sub.user_id,
                        new_end_date=str(sub.end_date),
                    )

                    # Уведомление (ошибка уведомления не отменяет компенсацию)
                    notif_text = _build_notification_text(lost_days, renewal_ts, sub.end_date)
                    try:
                        await notification_delivery_service.send_notification(
                            user=sub.user,
                            notification_type=NotificationType.SUBSCRIPTION_RENEWED,
                            context={},
                            bot=bot,
                            telegram_message=notif_text,
                        )
                    except Exception as notif_exc:  # noqa: BLE001
                        logger.warning(
                            'repair_stuck_fallback_compensate: уведомление не отправлено',
                            subscription_id=sub.id,
                            user_id=sub.user_id,
                            error=str(notif_exc),
                        )

                except Exception as exc:  # noqa: BLE001 — один сбой не должен рушить весь прогон
                    errors += 1
                    logger.error(
                        'repair_stuck_fallback_compensate: необработанная ошибка',
                        subscription_id=getattr(sub, 'id', None),
                        user_id=getattr(sub, 'user_id', None),
                        error=str(exc),
                        exc_info=True,
                    )

                finally:
                    await asyncio.sleep(_SLEEP_BETWEEN)

    finally:
        if bot is not None:
            await bot.session.close()

    # --- Итог ---
    if not apply:
        _print_dry_run_table(dry_run_rows)
    else:
        print()
        print('=' * 62)
        print('  APPLY — применено')
        print('=' * 62)
        print(f'  применено компенсаций : {applied}')
        print(f'  пропущено             : {skipped}')
        print(f'  ошибок                : {errors}')
        print('=' * 62)
        print()

    logger.info(
        'repair_stuck_fallback_compensate: завершено',
        apply=apply,
        applied=applied if apply else 'dry-run',
        skipped=skipped,
        errors=errors,
    )
    return 0 if errors == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            'Компенсирует дни пользователям, застрявшим в fallback-скваде RemnaWave '
            'после успешной оплаты продления подписки. По умолчанию — DRY-RUN.'
        )
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='применить изменения (по умолчанию — dry-run)',
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        help='обработать не более N подписок',
    )
    parser.add_argument(
        '--user-ids',
        type=str,
        default=None,
        metavar='IDS',
        help='фильтр по user_id через запятую, напр. 1,2,3',
    )
    args = parser.parse_args()

    user_ids: list[int] | None = None
    if args.user_ids:
        user_ids = [int(x.strip()) for x in args.user_ids.split(',') if x.strip()]

    return asyncio.run(_run(apply=args.apply, limit=args.limit, user_ids=user_ids))


if __name__ == '__main__':
    raise SystemExit(main())
