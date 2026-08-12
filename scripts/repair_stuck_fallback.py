#!/usr/bin/env python
"""Возвращает из fallback-сквада подписки, которые уже продлили.

ПРОБЛЕМА
========

Подписка попадает в fallback-сквад (см. app/services/expiry_fallback_service.py)
при истечении срока или исчерпании трафика — доступ урезается до Telegram/банков/
кабинета, пока юзер не продлит. Возврат из fallback штатно происходит внутри
extend_subscription/add_subscription_traffic и в периодическом reconcile.

Но если это звено пропало (потерянный вебхук, ручное продление в обход обычного
пути, сбой на конкретном шаге) — подписка в БД снова ACTIVE с далёким end_date,
а флаг fallback (expiry_fallback_active/traffic_fallback_active) и урезанный
сквад в панели остались. Юзер платит полную цену, а доступ у него — как у
просроченного. Этот скрипт разово находит такие подписки и возвращает их.

КРИТЕРИЙ ОТБОРА
===============

Статус ACTIVE + (expiry_fallback_active ИЛИ traffic_fallback_active) +
end_date дальше чем --min-days дней вперёд от текущего момента. Порог в
несколько дней (по умолчанию 10) отсекает тех, кто только что продлился и
для кого grace/reconcile ещё не успели отработать — не мешаем штатному пути.

ЧТО ДЕЛАЕТ --apply
===================

Для каждой подходящей подписки вызывает restore_from_fallback(db, subscription,
new_expire_at=subscription.end_date, notify=False) — тот же путь, что использует
reconcile. notify=False намеренно: это разовая техническая уборка застрявших
записей, а не событие, о котором нужно оповещать юзера или админа.

Usage:
    python -m scripts.repair_stuck_fallback                  # dry run
    python -m scripts.repair_stuck_fallback --apply           # persist
    python -m scripts.repair_stuck_fallback --limit 50        # на выборке
    python -m scripts.repair_stuck_fallback --min-days 14     # другой порог

Сухой прогон — поведение по умолчанию: сначала отчёт, потом --apply.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionStatus
from app.services.expiry_fallback_service import restore_from_fallback
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)

_BATCH_SIZE = 500

# Пауза между обращениями к панели при restore, чтобы не заваливать Remnawave.
_SLEEP_BETWEEN_RESTORE = 0.1

_DEFAULT_MIN_DAYS = 10


@dataclass
class ProblemRow:
    subscription_id: int
    reason: str


@dataclass
class RepairReport:
    dry_run: bool = True
    checked: int = 0
    candidates: int = 0
    restored: int = 0
    errors: int = 0
    problems: list[ProblemRow] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            'dry_run': self.dry_run,
            'checked': self.checked,
            'candidates': self.candidates,
            'restored': self.restored,
            'errors': self.errors,
        }


def classify_subscription(subscription: Any, *, min_days: int, now: datetime | None = None) -> bool:
    """True, если подписка застряла в fallback, хотя реально уже продлена.

    Условия (все обязательны):
      - status == ACTIVE — только реально живые подписки, не EXPIRED/DISABLED;
      - хотя бы один из флагов fallback (expiry_fallback_active,
        traffic_fallback_active) выставлен — иначе возвращать нечего;
      - end_date дальше чем ``min_days`` дней вперёд — короткий запас нарочно
        не считаем застреванием: это могут быть подписки, которые продлили
        буквально только что, и штатный reconcile ещё не успел их разгрести.
    """
    if now is None:
        now = datetime.now(UTC)

    status = getattr(subscription, 'status', None)
    if status != SubscriptionStatus.ACTIVE.value:
        return False

    has_fallback_flag = bool(getattr(subscription, 'expiry_fallback_active', False)) or bool(
        getattr(subscription, 'traffic_fallback_active', False)
    )
    if not has_fallback_flag:
        return False

    end_date = getattr(subscription, 'end_date', None)
    if end_date is None:
        return False
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)

    return end_date > now + timedelta(days=min_days)


async def _process_batch(
    db: AsyncSession,
    batch: list[Subscription],
    *,
    apply: bool,
    min_days: int,
    report: RepairReport,
) -> None:
    """Прогоняет одну пачку подписок через классификацию и (если apply) restore.

    Вынесена из ``_run`` отдельной функцией, чтобы в тестах можно было
    проверить поведение сухого прогона / --apply / устойчивости к сбою на
    произвольном списке подписок (SimpleNamespace) без поднятия БД — единственное,
    что реально трогает БД/панель, это ``restore_from_fallback``, вызванный
    по имени модуля, поэтому в тестах он подменяется моком.
    """
    for subscription in batch:
        report.checked += 1

        if not classify_subscription(subscription, min_days=min_days):
            continue

        report.candidates += 1

        if not apply:
            continue

        try:
            ok = await restore_from_fallback(
                db, subscription, new_expire_at=subscription.end_date, notify=False
            )
        except Exception as exc:  # noqa: BLE001 — один сбой не должен ронять весь прогон
            report.errors += 1
            report.problems.append(ProblemRow(int(subscription.id), f'exception: {exc}'))
            logger.warning(
                'repair_stuck_fallback: ошибка восстановления из fallback',
                subscription_id=subscription.id,
                error=str(exc),
            )
        else:
            if ok:
                report.restored += 1
            else:
                report.errors += 1
                report.problems.append(
                    ProblemRow(int(subscription.id), 'restore_from_fallback вернул False, см. логи')
                )
        finally:
            await asyncio.sleep(_SLEEP_BETWEEN_RESTORE)


async def _fetch_batch(db: AsyncSession, *, after_id: int | None, limit: int) -> list[Subscription]:
    conditions = [
        Subscription.status == SubscriptionStatus.ACTIVE.value,
    ]
    if after_id is not None:
        conditions.append(Subscription.id > after_id)

    result = await db.execute(
        select(Subscription).where(*conditions).order_by(Subscription.id.asc()).limit(limit)
    )
    return list(result.scalars().all())


def _print_report(report: RepairReport) -> None:
    data = report.as_dict()
    print()
    print('=' * 62)
    print('  DRY RUN — ничего не восстановлено' if data['dry_run'] else '  APPLIED')
    print('=' * 62)
    print(f'  подписок проверено          : {data["checked"]}')
    verb = 'подошло бы под критерий' if data['dry_run'] else 'подошло под критерий'
    print(f'  {verb:<28} : {data["candidates"]}')
    verb2 = 'было бы восстановлено' if data['dry_run'] else 'восстановлено'
    print(f'  {verb2:<28} : {data["restored"]}')
    print(f'  ошибок                      : {data["errors"]}')
    print()
    if report.problems:
        print(f'  проблемных строк: {len(report.problems)}')
        print('  первые 20:')
        for row in report.problems[:20]:
            print(f'    subscription #{row.subscription_id}: {row.reason}')
        if len(report.problems) > 20:
            print(f'    ...и ещё {len(report.problems) - 20}')
        print()
    print('=' * 62)


async def _run(apply: bool, limit: int | None, min_days: int) -> int:
    # Настройки тарификации (в т.ч. режим мультитарифа) живут в system_settings,
    # а не только в .env — без initialize() разовый скрипт может прочитать
    # режим неверно (см. тот же приём в repair_missing_panel_users).
    await bot_configuration_service.initialize(sync_web_api_token=False)

    report = RepairReport(dry_run=not apply)

    async with AsyncSessionLocal() as db:
        after_id: int | None = None
        while True:
            if limit is not None and report.checked >= limit:
                break

            batch_limit = _BATCH_SIZE
            if limit is not None:
                batch_limit = min(batch_limit, limit - report.checked)

            batch = await _fetch_batch(db, after_id=after_id, limit=batch_limit)
            if not batch:
                break
            after_id = int(batch[-1].id)

            await _process_batch(db, batch, apply=apply, min_days=min_days, report=report)

    _print_report(report)
    logger.info('repair_stuck_fallback: finished', **report.as_dict())

    return 0 if report.errors == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            'Restore subscriptions stuck in the fallback squad after they were '
            'already extended'
        )
    )
    parser.add_argument('--apply', action='store_true', help='persist changes (default is a dry run)')
    parser.add_argument('--limit', type=int, default=None, help='process at most N subscriptions')
    parser.add_argument(
        '--min-days',
        type=int,
        default=_DEFAULT_MIN_DAYS,
        help=f'end_date must be at least this many days ahead (default {_DEFAULT_MIN_DAYS})',
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.apply, args.limit, args.min_days))


if __name__ == '__main__':
    raise SystemExit(main())
