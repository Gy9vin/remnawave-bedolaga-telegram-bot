#!/usr/bin/env python
"""Компенсирует дни пользователям, оплатившим продление но застрявшим в fallback-скваде.

ПРОБЛЕМА
========

Исторический баг: при определённых путях продления подписки (в т.ч. автопокупка
после пополнения) restore_fallback_after_purchase не вызывался, и пользователь
оставался в fallback-скваде RemnaWave несмотря на успешную оплату.

Признак застревания — совокупность условий:
  1. status=ACTIVE в нашей БД;
  2. remnawave_id известен (пользователь создан в панели);
  3. В таблице transactions есть хотя бы одна завершённая запись типа
     SUBSCRIPTION_PAYMENT (факт оплаты продления);
  4. В панели RemnaWave active_internal_squads содержит EXPIRY_FALLBACK_SQUAD_UUID
     (источник истины — не флаги в нашей БД, а сама панель).

Флаги expiry_fallback_active / traffic_fallback_active намеренно НЕ используются
как фильтр: оба подслучая застревания (через автопокупку — флаги ещё стоят;
через старый баг с connected_squads — флаги уже сброшены) выявляются единым
критерием через панель.

ЧТО ДЕЛАЕТ СКРИПТ
=================

DRY-RUN (по умолчанию):
  - Находит кандидатов (оплатили + в fallback-скваде в панели)
  - Печатает таблицу: telegram_id, дата платежа, сумма ₽, lost_days,
    текущий end_date → новый end_date, целевые сквады, server_refund
  - Ничего не меняет, ничего не отправляет

--apply:
  - extend_subscription(+lost_days+5) → обновляет end_date в БД
  - _patch_user_full(squads=target_squads, expire_at=new_end_date) → один PATCH в RemnaWave
  - если server_refund_kopeks определён — add_user_balance(..., TransactionType.REFUND)
  - send_notification(SUBSCRIPTION_RENEWED) → Telegram и/или email

ВОЗВРАТ ЗА СЕРВЕР
=================

При застревании восстановленный fallback-сквад мог тарифицироваться как платный
сервер, увеличивая сумму следующего SUBSCRIPTION_PAYMENT. Скрипт пытается
определить эту переплату из SubscriptionServer.paid_price_kopeks (запись создаётся
в add_subscription_servers при каждом продлении).

Надёжное определение возможно только когда для данной подписки ровно одна запись
SubscriptionServer с fallback-скватом и paid_price_kopeks > 0 (однозначный
единственный цикл). В остальных случаях (нет записей / несколько записей от
нескольких продлений / цена 0) сумма помечается «н/д» и возврат не начисляется.
При необходимости — вернуть вручную.

БЕЗОПАСНОСТЬ
============

При --apply каждый пользователь обрабатывается в отдельном try/except.
Если PATCH в панели вернул False (_patch_user_full) — транзакция откатывается,
компенсация НЕ начисляется.

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
from app.database.crud.user import add_user_balance
from app.database.database import AsyncSessionLocal
from app.database.models import ServerSquad, Subscription, SubscriptionServer, SubscriptionStatus, Transaction, TransactionType
from app.services.expiry_fallback_service import _extract_squad_uuids, _patch_user_full
from app.services.notification_delivery_service import NotificationType, notification_delivery_service
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)


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


def calc_server_refund_kopeks(fallback_server_paid_prices: list[int]) -> int | None:
    """Консервативный расчёт суммы возврата за сервер.

    Принимает список paid_price_kopeks из SubscriptionServer-записей,
    соответствующих fallback-скваду данной подписки.

    Возвращает сумму в копейках, только если она определяется однозначно:
    ровно одна запись с paid_price_kopeks > 0 (один цикл продления, чёткая сумма).

    Возвращает None во всех неоднозначных случаях:
    - Нет записей (fallback-сквад не попал в add_subscription_servers).
    - Несколько записей (несколько циклов продления; нельзя выделить последний).
    - Одна запись с ценой <= 0 (списания не было).

    На практике чаще всего возвращает None — fallback-сквад нередко
    не фиксируется как add-on, а накопленные записи делают определение
    ненадёжным. При None рекомендуется ручная проверка.
    """
    if len(fallback_server_paid_prices) != 1:
        return None
    price = fallback_server_paid_prices[0]
    if price <= 0:
        return None
    return price


def _build_fallback_squad_ids(
    panel_users: list,
    fallback_squad_uuid: str,
) -> set[int]:
    """Возвращает множество числовых remnawave_id пользователей панели,
    чьи active_internal_squads содержат fallback_squad_uuid.

    Чистая функция — тестируется без БД и без живой панели.
    """
    result: set[int] = set()
    for pu in panel_users:
        active_squads = _extract_squad_uuids(getattr(pu, 'active_internal_squads', None))
        if fallback_squad_uuid in active_squads:
            result.add(pu.id)
    return result


# ============================================================================
# Запросы к БД
# ============================================================================


async def _fetch_candidates(
    db: AsyncSession,
    *,
    user_ids: list[int] | None,
    limit: int | None,
) -> list[Subscription]:
    """Подписки-кандидаты: ACTIVE + remnawave_id известен.

    Флаги expiry_fallback_active / traffic_fallback_active намеренно НЕ фильтруются:
    - застрявшие через путь автопокупки имеют флаги ЕЩЁ УСТАНОВЛЕННЫМИ
      (автопокупка не вызвала restore_fallback_after_purchase);
    - застрявшие через старый баг с connected_squads — флаги уже сброшены.
    Оба под-случая ловятся одним запросом. Источник истины — панель RemnaWave
    (проверяется позже через _is_in_fallback_squad).
    """
    conditions = [
        Subscription.status == SubscriptionStatus.ACTIVE.value,
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


async def _get_fallback_server_paid_prices(
    db: AsyncSession,
    subscription_id: int,
) -> list[int]:
    """Возвращает список paid_price_kopeks из SubscriptionServer для fallback-сквада.

    Используется для conservative-расчёта server_refund через calc_server_refund_kopeks.
    Если EXPIRY_FALLBACK_SQUAD_UUID не настроен или сквад не найден в БД — возвращает [].
    """
    fallback_uuid = settings.EXPIRY_FALLBACK_SQUAD_UUID
    if not fallback_uuid:
        return []

    squad_result = await db.execute(
        select(ServerSquad.id).where(ServerSquad.squad_uuid == fallback_uuid)
    )
    squad_id = squad_result.scalar_one_or_none()
    if squad_id is None:
        return []

    result = await db.execute(
        select(SubscriptionServer.paid_price_kopeks)
        .where(
            SubscriptionServer.subscription_id == subscription_id,
            SubscriptionServer.server_squad_id == squad_id,
        )
    )
    return [row[0] for row in result.fetchall()]


# ============================================================================
# Вспомогательный вывод
# ============================================================================


def _print_dry_run_table(rows: list[dict[str, Any]]) -> None:
    print()
    print('=' * 115)
    print('  DRY-RUN — ничего не изменено')
    print('=' * 115)
    if not rows:
        print('  Кандидатов не найдено.')
    else:
        header = (
            f"  {'user_id':>8}  {'tg_id':>12}  {'дата платежа':<13}  {'сумма':>8}  "
            f"{'lost_d':>6}  {'текущий end':>12}  {'новый end':>12}  {'srv_refund':>10}  сквады"
        )
        print()
        print(header)
        print('  ' + '-' * 111)
        for r in rows:
            squads_short = ','.join(str(s)[:8] for s in (r['target_squads'] or [])[:2]) or '—'
            srv_refund = r.get('server_refund_kopeks')
            srv_refund_str = f'{srv_refund / 100:.0f}₽' if srv_refund is not None else 'н/д'
            print(
                f"  {r['user_id']:>8}  {str(r.get('telegram_id') or '—'):>12}  "
                f"{r['paid_at']:<13}  {r['amount_rub']:>7.0f}₽  "
                f"{r['lost_days']:>6}  {r['end_date_cur']:>12}  {r['end_date_new']:>12}  "
                f"{srv_refund_str:>10}  {squads_short}"
            )
    print()
    print(f'  Итого кандидатов: {len(rows)}')
    print()
    print('  DRY-RUN, изменения не применялись; для применения запусти с --apply')
    print('  Колонка srv_refund: сумма возврата за сервер или «н/д» (определить не удалось — проверить вручную)')
    print('=' * 115)
    print()


def _build_notification_text(
    lost_days: int,
    renewal_ts: datetime,
    new_end_date: datetime,
    server_refund_kopeks: int | None = None,
) -> str:
    base = (
        f'✅ Мы исправили техническую ошибку: вы оставались в резервном скваде после оплаты '
        f'{renewal_ts.strftime("%d.%m.%Y")}. '
        f'Вернули {lost_days} потерянных дней и начислили бонус 5 дней в качестве извинений. '
        f'Новая дата окончания подписки: {new_end_date.strftime("%d.%m.%Y")}.'
    )
    if server_refund_kopeks and server_refund_kopeks > 0:
        server_rub = server_refund_kopeks / 100
        base += f' Также вернули ошибочно списанные {server_rub:.0f} ₽ за сервер на ваш баланс.'
    base += ' Приятного пользования!'
    return base


# ============================================================================
# Основной цикл
# ============================================================================


async def _run(*, apply: bool, limit: int | None, user_ids: list[int] | None) -> int:
    await bot_configuration_service.initialize(sync_web_api_token=False)

    fallback_uuid = settings.EXPIRY_FALLBACK_SQUAD_UUID
    if not fallback_uuid:
        logger.warning('repair_stuck_fallback_compensate: EXPIRY_FALLBACK_SQUAD_UUID не задан — нечего делать')
        return 0

    # --- Шаг 1: один обход панели (O(1) запросов), строим множество remnawave_id ---
    from app.services.remnawave_service import remnawave_service  # noqa: PLC0415
    async with remnawave_service.get_api_client() as api:
        panel_users = await api.get_all_users_stream(size=500)
    fallback_squad_ids: set[int] = _build_fallback_squad_ids(panel_users, fallback_uuid)
    logger.info(
        'repair_stuck_fallback_compensate: панель прочитана',
        total_panel_users=len(panel_users),
        in_fallback_squad=len(fallback_squad_ids),
    )

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
            # Загружаем все ACTIVE-подписки без лимита; фильтрация по squad-сету ниже
            all_subs = await _fetch_candidates(db, user_ids=user_ids, limit=None)
            # Оставляем только тех, чей remnawave_id присутствует в fallback-скваде панели
            candidates = [c for c in all_subs if c.remnawave_id in fallback_squad_ids]
            if limit is not None:
                candidates = candidates[:limit]
            logger.info(
                'repair_stuck_fallback_compensate: загружено подписок-кандидатов',
                total_active=len(all_subs),
                in_fallback=len(candidates),
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

                    # 2. Рассчитать lost_days, целевые сквады и возможный возврат за сервер
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

                    # Консервативный расчёт возврата за сервер
                    fallback_prices = await _get_fallback_server_paid_prices(db, sub.id)
                    server_refund_kopeks = calc_server_refund_kopeks(fallback_prices)

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
                            'server_refund_kopeks': server_refund_kopeks,
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
                        server_refund_kopeks=server_refund_kopeks,
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

                    # Возврат за ошибочно списанный сервер (если сумма определена)
                    if server_refund_kopeks and server_refund_kopeks > 0:
                        try:
                            await add_user_balance(
                                db,
                                sub.user,
                                amount_kopeks=server_refund_kopeks,
                                description='Возврат ошибочно списанной платы за сервер (fallback-баг)',
                                transaction_type=TransactionType.REFUND,
                            )
                            logger.info(
                                'repair_stuck_fallback_compensate: возврат за сервер начислен',
                                subscription_id=sub.id,
                                user_id=sub.user_id,
                                server_refund_kopeks=server_refund_kopeks,
                            )
                        except Exception as refund_exc:  # noqa: BLE001
                            logger.warning(
                                'repair_stuck_fallback_compensate: не удалось начислить возврат за сервер',
                                subscription_id=sub.id,
                                user_id=sub.user_id,
                                error=str(refund_exc),
                            )

                    # Уведомление (ошибка уведомления не отменяет компенсацию)
                    notif_text = _build_notification_text(
                        lost_days, renewal_ts, sub.end_date, server_refund_kopeks
                    )
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
