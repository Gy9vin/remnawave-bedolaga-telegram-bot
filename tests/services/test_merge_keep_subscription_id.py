"""Tests for keep_subscription_id override in _handle_subscription_merge.

Uses the same SimpleNamespace + AsyncMock pattern as
tests/services/test_merge_subscription_combine.py. No DB connection required.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.account_merge_service import _handle_subscription_merge
from app.config import Settings


_NOW = datetime(2026, 7, 30, 0, 0, 0, tzinfo=UTC)
# primary sub ends sooner (loser by default logic)
_PRIMARY_END = datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)   # 11 days from NOW
# secondary sub ends later (winner by default logic)
_SECONDARY_END = datetime(2026, 9, 15, 0, 0, 0, tzinfo=UTC)  # 47 days from NOW


def _make_sub(id, user_id, end_date, status='active', tariff_id=None, remnawave_uuid=None,
              subscription_url=None, subscription_crypto_link=None, remnawave_short_uuid=None):
    return SimpleNamespace(
        id=id,
        user_id=user_id,
        end_date=end_date,
        status=status,
        tariff_id=tariff_id,
        autopay_enabled=False,
        remnawave_uuid=remnawave_uuid,
        subscription_url=subscription_url,
        subscription_crypto_link=subscription_crypto_link,
        remnawave_short_uuid=remnawave_short_uuid,
        tariff=SimpleNamespace(name='Basic'),
        traffic_limit_gb=100.0,
        traffic_used_gb=0.0,
        device_limit=3,
        is_trial=False,
    )


def _make_user(id, remnawave_uuid=None, subscriptions=None):
    return SimpleNamespace(
        id=id,
        remnawave_uuid=remnawave_uuid,
        subscriptions=subscriptions or [],
    )


def _make_db():
    return SimpleNamespace(
        execute=AsyncMock(),
        delete=AsyncMock(),
        flush=AsyncMock(),
        add=MagicMock(),
    )


def _patch_single_tariff():
    return patch.object(Settings, 'is_multi_tariff_enabled', return_value=False)


class TestKeepSubscriptionIdSingleTariff:
    async def test_keep_early_sub_preserved_url(self):
        """keep_subscription_id = primary (early-end-date sub) → primary wins,
        its subscription_url / remnawave_short_uuid are preserved, secondary's
        remnawave_uuid is deferred for deletion."""
        primary_sub = _make_sub(
            1, 1, _PRIMARY_END, remnawave_uuid='rw-p',
            subscription_url='https://link.example/primary',
            remnawave_short_uuid='short-p',
        )
        secondary_sub = _make_sub(
            2, 2, _SECONDARY_END, remnawave_uuid='rw-s',
            subscription_url='https://link.example/secondary',
            remnawave_short_uuid='short-s',
        )
        primary = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[secondary_sub])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff(), \
             patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(
                db, primary, secondary, deferred,
                keep_subscription_id=primary_sub.id,
            )

        # Primary (early-end) is kept; secondary (late-end) is the loser
        # end_date extends by remaining of secondary: Sep 15 - Jul 30 = 47 days
        expected_end = _PRIMARY_END + timedelta(days=47)
        assert primary_sub.end_date == expected_end, \
            f'Expected {expected_end}, got {primary_sub.end_date}'

        # Link fields must be unchanged
        assert primary_sub.subscription_url == 'https://link.example/primary'
        assert primary_sub.remnawave_short_uuid == 'short-p'
        assert primary_sub.remnawave_uuid == 'rw-p'

        # Loser (secondary) deferred for deletion
        assert 'rw-s' in deferred
        assert secondary.remnawave_uuid is None

    async def test_keep_late_sub_secondary_wins(self):
        """keep_subscription_id = secondary (late-end-date sub) → secondary wins.
        Behaves the same as the default when secondary_end > primary_end,
        but is explicitly selected rather than auto-chosen."""
        primary_sub = _make_sub(
            1, 1, _PRIMARY_END, remnawave_uuid='rw-p',
            subscription_url='https://link.example/primary',
            remnawave_short_uuid='short-p',
        )
        secondary_sub = _make_sub(
            2, 2, _SECONDARY_END, remnawave_uuid='rw-s',
            subscription_url='https://link.example/secondary',
            remnawave_short_uuid='short-s',
        )
        primary = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[secondary_sub])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff(), \
             patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(
                db, primary, secondary, deferred,
                keep_subscription_id=secondary_sub.id,
            )

        # Secondary sub wins; primary's remnawave_uuid should be deferred
        assert 'rw-p' in deferred
        # secondary_sub is transferred to primary.id
        assert secondary_sub.user_id == primary.id

    async def test_keep_none_preserves_original_logic(self):
        """keep_subscription_id=None → default: winner = later end_date (secondary wins here)."""
        primary_sub = _make_sub(1, 1, _PRIMARY_END, remnawave_uuid='rw-p')
        secondary_sub = _make_sub(2, 2, _SECONDARY_END, remnawave_uuid='rw-s')
        primary = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[secondary_sub])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff(), \
             patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(
                db, primary, secondary, deferred,
                keep_subscription_id=None,
            )

        # Default: secondary wins (later end_date), primary's rw uuid is deferred
        assert 'rw-p' in deferred

    async def test_keep_sub_not_in_pair_raises(self):
        """keep_subscription_id pointing to an unrelated subscription id raises ValueError."""
        primary_sub = _make_sub(1, 1, _PRIMARY_END)
        secondary_sub = _make_sub(2, 2, _SECONDARY_END)
        primary = _make_user(1, subscriptions=[primary_sub])
        secondary = _make_user(2, subscriptions=[secondary_sub])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff():
            with pytest.raises(ValueError, match='keep_subscription_id'):
                await _handle_subscription_merge(
                    db, primary, secondary, deferred,
                    keep_subscription_id=999,
                )

    async def test_keep_lifetime_winner_no_extension(self):
        """Kept sub has end_date=None (lifetime) → no extension, stays None."""
        primary_sub = _make_sub(
            1, 1, None, remnawave_uuid='rw-p',
            subscription_url='https://link.example/primary',
        )
        secondary_sub = _make_sub(2, 2, _SECONDARY_END, remnawave_uuid='rw-s')
        primary = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[secondary_sub])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff(), \
             patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(
                db, primary, secondary, deferred,
                keep_subscription_id=primary_sub.id,
            )

        assert primary_sub.end_date is None
        assert primary_sub.subscription_url == 'https://link.example/primary'
        assert 'rw-s' in deferred
