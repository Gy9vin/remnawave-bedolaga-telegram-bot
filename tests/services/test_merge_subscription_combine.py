"""Tests for subscription combining in _handle_subscription_merge.

Uses the same SimpleNamespace mock pattern as test_account_merge_service.py.
No DB connection needed — _combine_subscription_end_dates is pure.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.account_merge_service import _combine_subscription_end_dates


# ---------------------------------------------------------------------------
# Pure helper: _combine_subscription_end_dates
# ---------------------------------------------------------------------------

NOW = datetime(2026, 7, 25, 0, 0, 0, tzinfo=UTC)
BASE = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)   # winner ends Aug 1
LOSER_END = datetime(2026, 7, 20, 0, 0, 0, tzinfo=UTC)  # loser ends Jul 20 (5d in past at NOW)
LOSER_END_FUTURE = datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)  # loser ends Aug 10 (16d remain)


def _make_sub_ns(end_date, status='active'):
    return SimpleNamespace(end_date=end_date, status=status)


class TestCombineSubscriptionEndDates:
    def test_loser_already_expired_adds_zero(self):
        """Loser's end_date is in the past → no extension."""
        winner = _make_sub_ns(BASE)
        loser = _make_sub_ns(LOSER_END)  # LOSER_END < NOW, remaining = 0
        extension = _combine_subscription_end_dates(winner, loser, NOW)
        assert extension == timedelta(0)

    def test_loser_has_remaining_days(self):
        """Loser ends Aug 10, now is Jul 25 → 16 days remaining → extension = 16 days."""
        winner = _make_sub_ns(BASE)
        loser = _make_sub_ns(LOSER_END_FUTURE)
        extension = _combine_subscription_end_dates(winner, loser, NOW)
        assert extension == timedelta(days=16)

    def test_winner_null_end_date_returns_zero(self):
        """Lifetime winner (None end_date) → never extend → returns timedelta(0)."""
        winner = _make_sub_ns(None)   # lifetime
        loser = _make_sub_ns(LOSER_END_FUTURE)
        extension = _combine_subscription_end_dates(winner, loser, NOW)
        assert extension == timedelta(0)

    def test_loser_null_end_date_is_impossible_path(self):
        """Loser cannot be lifetime (caller already picks the later end_date as winner).
        If somehow called with loser=None, treat remaining as 0 (no extension)."""
        winner = _make_sub_ns(BASE)
        loser = _make_sub_ns(None)
        extension = _combine_subscription_end_dates(winner, loser, NOW)
        assert extension == timedelta(0)


# ---------------------------------------------------------------------------
# Integration: _handle_subscription_merge single-tariff combine
# (uses the same AsyncMock DB pattern as test_account_merge_service.py)
# ---------------------------------------------------------------------------

from app.services.account_merge_service import _handle_subscription_merge  # noqa: E402


def _make_user(id, remnawave_uuid=None, subscriptions=None):
    return SimpleNamespace(
        id=id,
        remnawave_uuid=remnawave_uuid,
        subscriptions=subscriptions or [],
    )


def _make_sub(id, user_id, end_date, status='active', tariff_id=None, remnawave_uuid=None):
    return SimpleNamespace(
        id=id,
        user_id=user_id,
        end_date=end_date,
        status=status,
        tariff_id=tariff_id,
        autopay_enabled=False,
        remnawave_uuid=remnawave_uuid,
        tariff=SimpleNamespace(name='Basic'),
        traffic_limit_gb=100.0,
        traffic_used_gb=0.0,
        device_limit=3,
        is_trial=False,
    )


def _make_db():
    db = SimpleNamespace(
        execute=AsyncMock(),
        delete=AsyncMock(),
        flush=AsyncMock(),
        add=MagicMock(),
    )
    return db


_NOW = datetime(2026, 7, 25, 0, 0, 0, tzinfo=UTC)
_WINNER_END = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)   # Sep 1 ends later
_LOSER_END  = datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)  # loser ends Aug 10 (16d remain from Jul 25)
# Rule: winner = later end_date; _WINNER_END (Sep 1) > _LOSER_END (Aug 10) → correct


def _patch_single_tariff():
    """Patch Settings.is_multi_tariff_enabled to return False (single-tariff mode).
    Patches the class method directly to avoid pydantic frozen-model restrictions."""
    from app.config import Settings
    return patch.object(Settings, 'is_multi_tariff_enabled', return_value=False)


class TestSingleTariffCombine:
    async def test_both_active_winner_end_date_extended(self):
        """Both subs active: winner end_date grows by loser's remaining days."""
        primary_sub = _make_sub(1, 1, _WINNER_END, remnawave_uuid='rw-p')
        loser_sub   = _make_sub(2, 2, _LOSER_END,  remnawave_uuid='rw-s')
        primary  = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[loser_sub])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff(), \
             patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(db, primary, secondary, deferred)

        # Remaining from loser: Aug 10 - Jul 25 = 16 days
        expected_new_end = _WINNER_END + timedelta(days=16)
        assert primary_sub.end_date == expected_new_end

    async def test_both_active_loser_remnawave_deferred_for_deletion(self):
        """Loser's RemnaWave UUID is collected for deferred deletion."""
        primary_sub = _make_sub(1, 1, _WINNER_END, remnawave_uuid='rw-p')
        loser_sub   = _make_sub(2, 2, _LOSER_END,  remnawave_uuid='rw-s')
        primary   = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[loser_sub])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff(), \
             patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(db, primary, secondary, deferred)

        assert 'rw-s' in deferred
        assert secondary.remnawave_uuid is None

    async def test_subscription_event_written(self):
        """A SubscriptionEvent row with event_type='renewal' is added to the session."""
        primary_sub = _make_sub(1, 1, _WINNER_END)
        loser_sub   = _make_sub(2, 2, _LOSER_END)
        primary   = _make_user(1, subscriptions=[primary_sub])
        secondary = _make_user(2, subscriptions=[loser_sub])
        db = _make_db()
        deferred: list[str] = []
        added_objects = []
        db.add = lambda obj: added_objects.append(obj)

        with _patch_single_tariff(), \
             patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(db, primary, secondary, deferred)

        from app.database.models import SubscriptionEvent
        events = [o for o in added_objects if isinstance(o, SubscriptionEvent)]
        assert len(events) == 1
        ev = events[0]
        # Using 'renewal' (not 'merge') so it appears in the purchase timeline
        assert ev.event_type == 'renewal'
        assert ev.user_id == primary.id
        assert ev.subscription_id == primary_sub.id
        assert ev.extra['extended_days'] == 16
        assert 'previous_end_date' in ev.extra
        assert 'new_end_date' in ev.extra
        assert ev.extra.get('reason') == 'account_merge'

    async def test_lifetime_winner_no_extension_no_event(self):
        """Lifetime winner (end_date=None): no extension, no SubscriptionEvent."""
        primary_sub = _make_sub(1, 1, None)   # lifetime
        loser_sub   = _make_sub(2, 2, _LOSER_END)
        primary   = _make_user(1, subscriptions=[primary_sub])
        secondary = _make_user(2, subscriptions=[loser_sub])
        db = _make_db()
        deferred: list[str] = []
        added_objects = []
        db.add = lambda obj: added_objects.append(obj)

        with _patch_single_tariff(), \
             patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(db, primary, secondary, deferred)

        from app.database.models import SubscriptionEvent
        events = [o for o in added_objects if isinstance(o, SubscriptionEvent)]
        assert len(events) == 0
        assert primary_sub.end_date is None  # unchanged

    async def test_only_primary_sub_no_combine(self):
        """Only primary has sub — no combine, secondary's RemnaWave deferred."""
        primary_sub = _make_sub(1, 1, _WINNER_END, remnawave_uuid='rw-p')
        primary   = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff():
            await _handle_subscription_merge(db, primary, secondary, deferred)

        assert 'rw-s' in deferred
        assert primary_sub.end_date == _WINNER_END  # unchanged

    async def test_only_secondary_sub_transferred(self):
        """Only secondary has sub — it's transferred to primary, no combine."""
        loser_sub   = _make_sub(2, 2, _LOSER_END, remnawave_uuid='rw-s')
        primary   = _make_user(1, subscriptions=[])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[loser_sub])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff():
            await _handle_subscription_merge(db, primary, secondary, deferred)

        assert loser_sub.user_id == 1
        assert primary.remnawave_uuid == 'rw-s'
        assert secondary.remnawave_uuid is None

    async def test_secondary_wins_sub_reassigned_to_primary(self):
        """Secondary has the later end_date → secondary_sub becomes winner,
        extended by primary's remaining days, then reassigned to primary.id."""
        # primary ends earlier (loser), secondary ends later (winner)
        primary_end  = datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)   # earlier — loser
        secondary_end = datetime(2026, 9, 1,  0, 0, 0, tzinfo=UTC)  # later   — winner
        # primary remaining: Aug 10 - Jul 25 = 16 days
        primary_sub   = _make_sub(1, 1, primary_end,   remnawave_uuid='rw-p')
        secondary_sub = _make_sub(2, 2, secondary_end, remnawave_uuid='rw-s')
        primary   = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[secondary_sub])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff(), \
             patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(db, primary, secondary, deferred)

        # Secondary won → extended by primary's 16 remaining days
        expected_new_end = secondary_end + timedelta(days=16)
        assert secondary_sub.end_date == expected_new_end, (
            f"Winner end_date should be {expected_new_end}, got {secondary_sub.end_date}"
        )
        # Winner reassigned to primary
        assert secondary_sub.user_id == primary.id, (
            f"winner_sub.user_id should be primary.id={primary.id}, got {secondary_sub.user_id}"
        )
        # Loser (primary_sub) expired
        assert primary_sub.status == 'expired', (
            f"primary_sub.status should be 'expired', got {primary_sub.status!r}"
        )
        # Primary's remnawave_uuid now holds winner's (secondary's) old remnawave uuid
        assert primary.remnawave_uuid == 'rw-s', (
            f"primary.remnawave_uuid should be 'rw-s', got {primary.remnawave_uuid!r}"
        )
        # Secondary's remnawave_uuid cleared
        assert secondary.remnawave_uuid is None, (
            f"secondary.remnawave_uuid should be None, got {secondary.remnawave_uuid!r}"
        )


def _patch_multi_tariff():
    """Patch Settings.is_multi_tariff_enabled to return True (multi-tariff mode)."""
    from app.config import Settings
    return patch.object(Settings, 'is_multi_tariff_enabled', return_value=True)


class TestMultiTariffCombine:
    async def test_same_tariff_conflict_winner_extended_and_event_written(self):
        """Multi-tariff: two active subs with same tariff_id → winner extended,
        SubscriptionEvent with event_type='renewal' and reason='account_merge' written."""
        TARIFF_ID = 42
        primary_end   = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)   # winner (later)
        secondary_end = datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)  # loser (earlier)
        # secondary remaining: Aug 10 - Jul 25 = 16 days
        primary_sub   = _make_sub(1, 1, primary_end,   tariff_id=TARIFF_ID, remnawave_uuid='rw-p')
        secondary_sub = _make_sub(2, 2, secondary_end, tariff_id=TARIFF_ID, remnawave_uuid='rw-s')
        primary   = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[secondary_sub])
        db = _make_db()
        deferred: list[str] = []
        added_objects = []
        db.add = lambda obj: added_objects.append(obj)

        with _patch_multi_tariff(), \
             patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(db, primary, secondary, deferred)

        # Winner (primary_sub) extended by loser's 16 remaining days
        expected_new_end = primary_end + timedelta(days=16)
        assert primary_sub.end_date == expected_new_end, (
            f"Winner end_date should be {expected_new_end}, got {primary_sub.end_date}"
        )

        # A SubscriptionEvent row exists with event_type='renewal'
        from app.database.models import SubscriptionEvent
        events = [o for o in added_objects if isinstance(o, SubscriptionEvent)]
        assert len(events) >= 1, "Expected at least one SubscriptionEvent"
        ev = events[0]
        assert ev.event_type == 'renewal'
        assert ev.extra.get('reason') == 'account_merge'
        assert ev.extra.get('extended_days') == 16
        assert 'previous_end_date' in ev.extra
        assert 'new_end_date' in ev.extra
