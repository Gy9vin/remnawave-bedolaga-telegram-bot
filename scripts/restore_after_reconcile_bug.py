"""Откат ошибочных восстановлений из fallback-сквада.

Контекст бага
-------------
До 2026-05-10 в reconcile_fallback_subscriptions была неверная логика
обнаружения "внешнего продления через панель Remnawave": сравнивалось
current_expire_at с baseline_expire_at (старая дата истечения) + buffer.
Для подписок, истёкших давно (например, в феврале), наш собственный
grace (now + 5d) автоматически превышал baseline+buffer → reconcile
ложно решал, что admin продлил, и вытаскивал юзера из fallback с
new_expire_at = current_expire_at (то есть = наш grace, ~now+5d).

В итоге тысячи подписок ушли из fallback-сквада в исходные сквады
с искусственным subscription.end_date ≈ now+5d.

Что делает скрипт
-----------------
1. Парсит логи бота, ищет строки "обнаружено внешнее продление через
   панель — restore" и собирает {subscription_id → baseline_expire,
   restored_at} (это и есть жертвы).
2. Для каждой жертвы загружает подписку из БД и решает:
   - SKIP, если после restored_at у юзера была успешная транзакция
     SUBSCRIPTION_PAYMENT (он реально продлил после бага).
   - SKIP, если subscription.end_date ушло сильно вперёд от нашего
     ошибочного значения (>now+grace_tolerance дней) — значит реальное
     продление через бот/кабинет/sync с Remnawave.
   - SKIP, если уже снова в fallback (sync уже сам вернул).
   - ROLLBACK иначе: ставит end_date = baseline_expire, status =
     EXPIRED, потом move_to_fallback(notify=False) — возвращает в
     fallback-сквад в Remnawave.

Использование
-------------
Сначала dry-run — показать план без записи:

    python scripts/restore_after_reconcile_bug.py --log /path/to/bot.log

После проверки — реальный откат:

    python scripts/restore_after_reconcile_bug.py --log /path/to/bot.log --apply

Дополнительные флаги:
    --limit N           # обработать только первые N подписок (для теста)
    --grace-tolerance N # сколько дней после grace ещё считать ошибкой
                        # (default 14 — если end_date ≤ now+14d, считаем
                        # это ошибочным восстановлением)
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone

UTC = timezone.utc
from pathlib import Path

# Make project root importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


PATTERN = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) .*?'
    r'обнаружено внешнее продление через панель — restore '
    r'baseline_expire=datetime\.datetime\((?P<bl>[^)]+)\)\s+'
    r'current_expire=datetime\.datetime\([^)]+\)\s+'
    r'subscription_id=(?P<sid>\d+)'
)


def _parse_dt_args(arg_str: str) -> datetime:
    """'2026, 2, 10, 1, 6, 43, 628000, tzinfo=...' -> datetime UTC."""
    parts = []
    for part in arg_str.split(','):
        part = part.strip()
        if 'tzinfo' in part or '=' in part:
            break
        parts.append(int(part))
    parts += [0] * (7 - len(parts))
    return datetime(*parts[:7], tzinfo=UTC)


def parse_log(path: Path) -> dict[int, dict]:
    """{sub_id: {'baseline_expire': dt, 'restored_at': dt}}.

    При повторных событиях для одного subscription_id берём ПЕРВОЕ —
    это первый момент порчи.
    """
    victims: dict[int, dict] = {}
    with path.open('r', encoding='utf-8', errors='replace') as f:
        for line in f:
            m = PATTERN.search(line)
            if not m:
                continue
            sid = int(m.group('sid'))
            if sid in victims:
                continue
            try:
                victims[sid] = {
                    'baseline_expire': _parse_dt_args(m.group('bl')),
                    'restored_at': datetime.strptime(
                        m.group('ts'), '%Y-%m-%d %H:%M:%S'
                    ).replace(tzinfo=UTC),
                }
            except Exception:
                continue
    return victims


async def run_rollback(
    *,
    victims: dict[int, dict],
    apply_changes: bool,
    limit: int | None,
    grace_tolerance_days: int,
    throttle_ms: int = 150,
    retry_on_false: bool = True,
) -> None:
    from sqlalchemy import and_, func, select

    from app.database.database import AsyncSessionLocal  # type: ignore
    from app.database.models import (  # type: ignore
        Subscription,
        SubscriptionStatus,
        Transaction,
        TransactionType,
    )
    from app.services.expiry_fallback_service import move_to_fallback  # type: ignore

    now = datetime.now(UTC)
    stats: dict[str, int] = defaultdict(int)
    sample_lines: list[str] = []

    items = list(victims.items())
    if limit:
        items = items[:limit]

    async with AsyncSessionLocal() as db:
        for sid, info in items:
            stats['total'] += 1
            baseline: datetime = info['baseline_expire']
            restored_at: datetime = info['restored_at']

            sub = (
                await db.execute(select(Subscription).where(Subscription.id == sid))
            ).scalar_one_or_none()
            if sub is None:
                stats['skip_not_found'] += 1
                continue

            current_end = sub.end_date
            if current_end is not None and current_end.tzinfo is None:
                current_end = current_end.replace(tzinfo=UTC)

            # 1. Уже снова в fallback — sync/мы сам вернул, ничего не надо.
            if sub.expiry_fallback_active or sub.traffic_fallback_active:
                stats['skip_already_in_fallback'] += 1
                continue

            # 2. end_date ушло далеко вперёд (>now+tolerance) — реальное продление.
            if current_end and current_end > now and (current_end - now).days > grace_tolerance_days:
                stats['skip_real_renewal_by_date'] += 1
                continue

            # 3. Реальная оплата ПОСЛЕ ошибочного restore — пропускаем.
            payment_count = await db.scalar(
                select(func.count(Transaction.id)).where(
                    and_(
                        Transaction.user_id == sub.user_id,
                        Transaction.type == TransactionType.SUBSCRIPTION_PAYMENT.value,
                        Transaction.is_completed.is_(True),
                        Transaction.created_at > restored_at,
                    )
                )
            )
            if (payment_count or 0) > 0:
                stats['skip_real_payment'] += 1
                continue

            stats['will_rollback'] += 1
            if len(sample_lines) < 8:
                sample_lines.append(
                    f'  sub#{sid:<6} user#{sub.user_id:<6} '
                    f'end_date {current_end.date() if current_end else "?":<10} → '
                    f'{baseline.date()}  status: {sub.status} → expired'
                )

            if not apply_changes:
                continue

            # APPLY — откатываем
            try:
                sub.end_date = baseline
                sub.status = SubscriptionStatus.EXPIRED.value
                await db.commit()
                await db.refresh(sub)

                # Диагностика «почему False» — повторяем проверки move_to_fallback.
                from app.config import settings as _settings
                from app.services.expiry_fallback_service import (
                    _is_dev_user_allowed,  # noqa: F401  (доступ к приватной — для диагностики)
                    _is_fallback_enabled,
                )

                if not _is_fallback_enabled():
                    stats['rollback_skip_fallback_disabled'] += 1
                    if stats['rollback_skip_fallback_disabled'] <= 3:
                        print(f'[diag] sub#{sid}: fallback DISABLED in settings')
                    continue
                if not _is_dev_user_allowed(sub):
                    stats['rollback_skip_dev_mode'] += 1
                    if stats['rollback_skip_dev_mode'] <= 3:
                        print(
                            f'[diag] sub#{sid} (user#{sub.user_id}): blocked by '
                            f'DEV_MODE whitelist'
                        )
                    continue
                if not sub.remnawave_uuid:
                    stats['rollback_skip_no_uuid'] += 1
                    continue
                if sub.expiry_fallback_active or sub.traffic_fallback_active:
                    # move_to_fallback это короткий путь — вернёт True. Не наш кейс
                    # для счётчика «failed», но обработаем.
                    stats['rollback_already_marked'] += 1
                    continue

                ok = await move_to_fallback(db, sub, reason='expired', notify=False)
                if not ok and retry_on_false:
                    await asyncio.sleep(1.0)
                    ok = await move_to_fallback(db, sub, reason='expired', notify=False)
                    if ok:
                        stats['rolled_back_after_retry'] += 1
                if ok:
                    stats['rolled_back'] += 1
                else:
                    stats['rollback_move_returned_false'] += 1
                    if stats['rollback_move_returned_false'] <= 5:
                        print(
                            f'[diag] sub#{sid} (user#{sub.user_id}, '
                            f'uuid={sub.remnawave_uuid}): move_to_fallback returned False '
                            f'— check Remnawave API connectivity / panel logs'
                        )

                # Throttle между подписками (не утопить Remnawave API)
                if throttle_ms > 0:
                    await asyncio.sleep(throttle_ms / 1000.0)
            except Exception as exc:
                await db.rollback()
                stats['rollback_error'] += 1
                print(f'[ERR] sub#{sid}: {exc}', file=sys.stderr)

    mode = 'APPLY' if apply_changes else 'DRY-RUN'
    print(f'\n=== {mode} REPORT ===')
    for k in sorted(stats):
        print(f'  {k:<32} {stats[k]:>6}')
    if sample_lines:
        print('\nSample plan (first 8):')
        for line in sample_lines:
            print(line)
    if not apply_changes:
        print(
            '\nDry-run finished. Review counts/sample above, '
            'then re-run with --apply.'
        )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--log', required=True, type=Path, help='Path to bot.log')
    p.add_argument(
        '--apply',
        action='store_true',
        help='Actually rollback (default: dry-run only).',
    )
    p.add_argument('--limit', type=int, default=None, help='Process only first N (for test).')
    p.add_argument(
        '--grace-tolerance',
        type=int,
        default=14,
        help='Skip rollback if subscription.end_date is more than N days ahead (default 14).',
    )
    p.add_argument(
        '--throttle-ms',
        type=int,
        default=150,
        help='Pause N ms between subscriptions (default 150) — avoids overloading Remnawave API.',
    )
    p.add_argument(
        '--no-retry',
        action='store_true',
        help='Disable retry on False from move_to_fallback (default: retry once after 1s).',
    )
    args = p.parse_args()

    if not args.log.exists():
        print(f'Log not found: {args.log}', file=sys.stderr)
        sys.exit(2)

    print(f'[*] Parsing {args.log} ...')
    victims = parse_log(args.log)
    print(f'[*] Found {len(victims)} unique erroneous restore events.')

    if not victims:
        print('Nothing to do.')
        return

    asyncio.run(
        run_rollback(
            victims=victims,
            apply_changes=args.apply,
            limit=args.limit,
            grace_tolerance_days=args.grace_tolerance,
            throttle_ms=args.throttle_ms,
            retry_on_false=not args.no_retry,
        )
    )


if __name__ == '__main__':
    main()
