"""Тесты scripts.repair_stuck_fallback.

Без БД: подписки — SimpleNamespace с нужными полями, restore_from_fallback
подменяется асинхронным моком в модуле скрипта. Проверяем: критерий отбора
(classify_subscription) отдельно от БД, и поведение обработки пачки
(_process_batch) — сухой прогон/apply/устойчивость к сбою на одной подписке.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.database.models import SubscriptionStatus
from scripts.repair_stuck_fallback import RepairReport, _process_batch, classify_subscription


NOW = datetime(2026, 8, 12, tzinfo=UTC)


def subscription(
    sub_id=1,
    *,
    status=SubscriptionStatus.ACTIVE.value,
    expiry_fallback_active=False,
    traffic_fallback_active=False,
    days_ahead=15,
):
    end_date = NOW + timedelta(days=days_ahead) if days_ahead is not None else None
    return SimpleNamespace(
        id=sub_id,
        status=status,
        end_date=end_date,
        expiry_fallback_active=expiry_fallback_active,
        traffic_fallback_active=traffic_fallback_active,
    )


# ---------------------------------------------------------------------------
# classify_subscription — критерий отбора, без БД
# ---------------------------------------------------------------------------


def test_candidate_when_active_with_fallback_flag_and_far_end_date():
    """15 дней вперёд, статус ACTIVE, флаг стоит — застряла в fallback, продлена."""
    sub = subscription(expiry_fallback_active=True, days_ahead=15)
    assert classify_subscription(sub, min_days=10, now=NOW) is True


def test_not_candidate_when_end_date_too_close():
    """3 дня вперёд — мог продлиться только что, штатный reconcile ещё не успел."""
    sub = subscription(expiry_fallback_active=True, days_ahead=3)
    assert classify_subscription(sub, min_days=10, now=NOW) is False


def test_not_candidate_when_no_fallback_flags():
    """Обычная активная подписка без fallback-флагов — трогать нечего."""
    sub = subscription(expiry_fallback_active=False, traffic_fallback_active=False, days_ahead=15)
    assert classify_subscription(sub, min_days=10, now=NOW) is False


def test_not_candidate_when_status_not_active():
    """Флаг стоит и дата далеко, но подписка не ACTIVE — не наш случай."""
    sub = subscription(
        status=SubscriptionStatus.EXPIRED.value, expiry_fallback_active=True, days_ahead=15
    )
    assert classify_subscription(sub, min_days=10, now=NOW) is False


def test_candidate_with_traffic_fallback_flag_only():
    """traffic_fallback_active тоже считается — не только expiry_fallback_active."""
    sub = subscription(traffic_fallback_active=True, days_ahead=20)
    assert classify_subscription(sub, min_days=10, now=NOW) is True


def test_boundary_exactly_min_days_is_not_candidate():
    """end_date ровно на пороге (не строго больше) — не кандидат."""
    sub = subscription(expiry_fallback_active=True, days_ahead=10)
    assert classify_subscription(sub, min_days=10, now=NOW) is False


# ---------------------------------------------------------------------------
# _process_batch — сухой прогон / apply / устойчивость к сбоям
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_does_not_restore(monkeypatch):
    """Сухой прогон должен посчитать кандидатов, но ни разу не звать restore."""
    calls = []

    async def fake_restore(db, sub, *, new_expire_at, notify):
        calls.append(sub.id)
        return True

    monkeypatch.setattr('scripts.repair_stuck_fallback.restore_from_fallback', fake_restore)

    batch = [
        subscription(1, expiry_fallback_active=True, days_ahead=15),
        subscription(2, expiry_fallback_active=False, days_ahead=15),
    ]
    report = RepairReport(dry_run=True)
    await _process_batch(None, batch, apply=False, min_days=10, report=report)

    assert calls == []
    assert report.checked == 2
    assert report.candidates == 1
    assert report.restored == 0


@pytest.mark.asyncio
async def test_apply_calls_restore_for_each_candidate(monkeypatch):
    """--apply зовёт restore_from_fallback для каждой подходящей подписки."""
    calls = []

    async def fake_restore(db, sub, *, new_expire_at, notify):
        calls.append((sub.id, new_expire_at, notify))
        return True

    monkeypatch.setattr('scripts.repair_stuck_fallback.restore_from_fallback', fake_restore)

    sub1 = subscription(1, expiry_fallback_active=True, days_ahead=15)
    sub2 = subscription(2, traffic_fallback_active=True, days_ahead=20)
    sub3 = subscription(3, expiry_fallback_active=True, days_ahead=3)  # не кандидат

    report = RepairReport(dry_run=False)
    await _process_batch(object(), [sub1, sub2, sub3], apply=True, min_days=10, report=report)

    assert {c[0] for c in calls} == {1, 2}
    # notify=False — намеренно, это разовая уборка, а не событие для юзера
    assert all(notify is False for _, _, notify in calls)
    # new_expire_at передан как end_date подписки
    assert all(new_expire_at is not None for _, new_expire_at, _ in calls)
    assert report.checked == 3
    assert report.candidates == 2
    assert report.restored == 2
    assert report.errors == 0


@pytest.mark.asyncio
async def test_single_failure_does_not_stop_the_rest(monkeypatch):
    """Сбой (исключение) на одной подписке не должен прерывать обработку остальных."""
    calls = []

    async def flaky_restore(db, sub, *, new_expire_at, notify):
        calls.append(sub.id)
        if sub.id == 2:
            raise RuntimeError('panel is down')
        return True

    monkeypatch.setattr('scripts.repair_stuck_fallback.restore_from_fallback', flaky_restore)

    batch = [
        subscription(1, expiry_fallback_active=True, days_ahead=15),
        subscription(2, expiry_fallback_active=True, days_ahead=15),
        subscription(3, expiry_fallback_active=True, days_ahead=15),
    ]
    report = RepairReport(dry_run=False)
    await _process_batch(object(), batch, apply=True, min_days=10, report=report)

    # Все три были попытаны, несмотря на исключение на второй
    assert calls == [1, 2, 3]
    assert report.candidates == 3
    assert report.restored == 2
    assert report.errors == 1
    assert report.problems[0].subscription_id == 2


@pytest.mark.asyncio
async def test_restore_returning_false_counts_as_error(monkeypatch):
    """restore_from_fallback может вернуть False (PATCH не удался) — это тоже ошибка отчёта."""

    async def fake_restore(db, sub, *, new_expire_at, notify):
        return False

    monkeypatch.setattr('scripts.repair_stuck_fallback.restore_from_fallback', fake_restore)

    batch = [subscription(1, expiry_fallback_active=True, days_ahead=15)]
    report = RepairReport(dry_run=False)
    await _process_batch(object(), batch, apply=True, min_days=10, report=report)

    assert report.restored == 0
    assert report.errors == 1
