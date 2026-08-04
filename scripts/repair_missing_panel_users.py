#!/usr/bin/env python
"""Repair live subscriptions that lost their Remnawave panel account.

A subscription can be "alive" in the bot's own bookkeeping (status active/trial,
``end_date`` still in the future — the person paid and the term has not run out)
while the corresponding panel account no longer exists: it was never created, or
it was deleted on the panel side out of band. Those users are paying customers
with no working VPN. This script finds them and (with ``--apply``) creates the
missing panel account through the normal service path.

It deliberately does NOT try to fix rows whose panel account merely lost its
*link* (``remnawave_id`` empty but ``remnawave_short_uuid`` still resolves in the
panel roster) — creating one there would produce a duplicate account next to a
live one. That case is the job of ``scripts.backfill_remnawave_ids``; this script
only reports it.

Usage:
    python -m scripts.repair_missing_panel_users                 # dry run
    python -m scripts.repair_missing_panel_users --apply          # persist
    python -m scripts.repair_missing_panel_users --limit 50       # try on a sample

A dry run is the default on purpose: read the report, then re-run with --apply.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, SubscriptionStatus
from app.services.remnawave_identity_backfill import _PanelIndex, _load_panel_roster
from app.services.remnawave_service import RemnaWaveService
from app.services.subscription_service import SubscriptionService
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)

# Подписка считается живой при этих статусах — как в остальном коде, что
# работает с грейсом/панелью (см. remnawave_identity_backfill._ALIVE_STATUSES).
_ALIVE_STATUSES = (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)

_BATCH_SIZE = 500

# Пауза между вызовами создания панельного аккаунта, чтобы не заваливать панель.
_SLEEP_BETWEEN_CREATE = 0.1


@dataclass
class ProblemRow:
    subscription_id: int
    reason: str


@dataclass
class RepairReport:
    dry_run: bool = True
    checked: int = 0
    already_present: int = 0
    created: int = 0
    identity_missing: int = 0
    errors: int = 0
    problems: list[ProblemRow] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            'dry_run': self.dry_run,
            'checked': self.checked,
            'already_present': self.already_present,
            'created': self.created,
            'identity_missing': self.identity_missing,
            'errors': self.errors,
        }


def classify_subscription(subscription: Any, index: _PanelIndex) -> str:
    """Return 'present', 'identity_missing' or 'candidate'.

    ``present``: subscription.remnawave_id is set and the panel roster still
    knows that id — the account is where it should be, nothing to do.

    ``identity_missing``: remnawave_id is empty, but remnawave_short_uuid
    resolves in the panel roster — the account exists, only the link on the bot
    side is broken. Creating a new account here would duplicate a live one;
    that case belongs to the identity backfill, not to this script.

    ``candidate``: neither identifier resolves — no panel account exists (or the
    stored remnawave_id points at a deleted one), and one should be created.
    """
    panel_id = getattr(subscription, 'remnawave_id', None)
    if panel_id is not None and int(panel_id) in index.by_id:
        return 'present'

    short_uuid = (getattr(subscription, 'remnawave_short_uuid', None) or '').strip()
    if panel_id is None and short_uuid and short_uuid in index.by_short_uuid:
        return 'identity_missing'

    return 'candidate'


async def _fetch_alive_batch(
    db: AsyncSession, *, after_id: int | None, limit: int
) -> list[Subscription]:
    conditions = [
        Subscription.status.in_(_ALIVE_STATUSES),
        Subscription.end_date > datetime.now(UTC),
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
    print('  DRY RUN — ничего не создано' if data['dry_run'] else '  APPLIED')
    print('=' * 62)
    print(f'  живых подписок проверено    : {data["checked"]}')
    print(f'  аккаунт уже на месте        : {data["already_present"]}')
    verb = 'было бы создано' if data['dry_run'] else 'создано'
    print(f'  {verb:<28} : {data["created"]}')
    print(f'  потеряна связь (identity)   : {data["identity_missing"]}')
    print(f'  ошибок                      : {data["errors"]}')
    print()
    if report.identity_missing:
        print(
            f'  !! {report.identity_missing} подписок с потерянной связью — '
            'прогоните python -m scripts.backfill_remnawave_ids'
        )
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


async def _run(apply: bool, limit: int | None) -> int:
    # Та же причина, что и в backfill_remnawave_ids: настройки из кабинета живут
    # в system_settings, а не в .env, и одноразовый прогон обязан их подтянуть —
    # иначе, например, MULTI_TARIFF_ENABLED читался бы как False.
    await bot_configuration_service.initialize(sync_web_api_token=False)
    print(f'  режим: {"МУЛЬТИТАРИФ" if settings.is_multi_tariff_enabled() else "однотарифный"}')

    remnawave_service = RemnaWaveService()
    if not remnawave_service.is_configured:
        raise RuntimeError(f'RemnaWave panel is not configured: {remnawave_service.configuration_error}')

    panel_users = await _load_panel_roster(remnawave_service)
    index = _PanelIndex(panel_users)
    report = RepairReport(dry_run=not apply)
    logger.info('repair: panel roster loaded', panel_users=len(panel_users))
    print(f'  пользователей в панели      : {len(panel_users)}')

    subscription_service = SubscriptionService() if apply else None

    async with AsyncSessionLocal() as db:
        after_id: int | None = None
        while True:
            if limit is not None and report.checked >= limit:
                break

            batch_limit = _BATCH_SIZE
            if limit is not None:
                batch_limit = min(batch_limit, limit - report.checked)

            batch = await _fetch_alive_batch(db, after_id=after_id, limit=batch_limit)
            if not batch:
                break
            after_id = int(batch[-1].id)

            for subscription in batch:
                report.checked += 1
                verdict = classify_subscription(subscription, index)

                if verdict == 'present':
                    report.already_present += 1
                    continue

                if verdict == 'identity_missing':
                    report.identity_missing += 1
                    report.problems.append(
                        ProblemRow(int(subscription.id), 'identity_missing (run backfill_remnawave_ids)')
                    )
                    continue

                # candidate — панельного аккаунта нет.
                if not apply:
                    report.created += 1
                    continue

                try:
                    created_user = await subscription_service.create_remnawave_user(db, subscription)
                except Exception as exc:  # noqa: BLE001 — один сбой не должен ронять весь прогон
                    report.errors += 1
                    report.problems.append(ProblemRow(int(subscription.id), f'exception: {exc}'))
                    logger.warning(
                        'repair: ошибка создания панельного аккаунта',
                        subscription_id=subscription.id,
                        error=str(exc),
                    )
                else:
                    if created_user is None:
                        report.errors += 1
                        report.problems.append(
                            ProblemRow(int(subscription.id), 'create_remnawave_user вернул None, см. логи')
                        )
                    else:
                        report.created += 1
                finally:
                    await asyncio.sleep(_SLEEP_BETWEEN_CREATE)

    _print_report(report)
    logger.info('repair: finished', **report.as_dict())

    return 0 if report.errors == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Create missing Remnawave panel accounts for live subscriptions'
    )
    parser.add_argument('--apply', action='store_true', help='persist changes (default is a dry run)')
    parser.add_argument('--limit', type=int, default=None, help='process at most N subscriptions')
    args = parser.parse_args()
    return asyncio.run(_run(args.apply, args.limit))


if __name__ == '__main__':
    raise SystemExit(main())
