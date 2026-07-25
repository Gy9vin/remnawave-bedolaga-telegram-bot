"""Smoke tests that guard against signature drift in merge callers.

C1: Both phantom_service.merge_phantom_into_user and the admin linking route
must call execute_merge WITHOUT the removed keep_subscription_from parameter.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(id: int = 1, subscriptions=None) -> SimpleNamespace:
    subs = subscriptions or []
    return SimpleNamespace(
        id=id,
        telegram_id=id * 100,
        username=f'user{id}',
        subscription=subs[0] if subs else None,
        subscriptions=subs,
        balance_kopeks=0,
    )


def _make_subscription(*, status: str = 'active', days_from_now: int = 30) -> SimpleNamespace:
    end_date = datetime.now(UTC) + timedelta(days=days_from_now)
    return SimpleNamespace(
        id=999,
        status=status,
        end_date=end_date,
        user_id=2,
        remnawave_uuid=None,
    )


def _expired_subscription() -> SimpleNamespace:
    return SimpleNamespace(
        id=998,
        status='expired',
        end_date=datetime.now(UTC) - timedelta(days=1),
        user_id=2,
        remnawave_uuid=None,
    )


# ---------------------------------------------------------------------------
# C1: merge_phantom_into_user — no keep_subscription_from kwarg
# ---------------------------------------------------------------------------


class TestPhantomMergeSignature:
    """merge_phantom_into_user must call execute_merge without keep_subscription_from."""

    async def test_no_keep_subscription_from_kwarg(self):
        """execute_merge must be called without keep_subscription_from."""
        from app.services import phantom_service

        phantom = _make_user(id=2, subscriptions=[_make_subscription()])
        active_user = _make_user(id=1)

        db = AsyncMock()
        db.refresh = AsyncMock()
        db.begin_nested = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=None),
        ))

        captured_kwargs: dict = {}

        async def mock_execute_merge(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return _make_user(id=1)

        with patch.object(phantom_service, 'execute_merge', side_effect=mock_execute_merge):
            with patch.object(phantom_service, 'AuditLogCRUD') as mock_audit:
                mock_audit.create = AsyncMock()
                await phantom_service.merge_phantom_into_user(db, phantom, active_user)

        assert 'keep_subscription_from' not in captured_kwargs, (
            "execute_merge must not receive keep_subscription_from (param was removed)"
        )

    async def test_returns_true_when_phantom_has_active_sub(self):
        """Returns True when phantom had a future-dated active subscription."""
        from app.services import phantom_service

        phantom = _make_user(id=2, subscriptions=[_make_subscription(status='active', days_from_now=10)])
        active_user = _make_user(id=1)

        db = AsyncMock()
        db.refresh = AsyncMock()
        db.begin_nested = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=None),
        ))

        async def mock_execute_merge(*args, **kwargs):
            return _make_user(id=1)

        with patch.object(phantom_service, 'execute_merge', side_effect=mock_execute_merge):
            with patch.object(phantom_service, 'AuditLogCRUD') as mock_audit:
                mock_audit.create = AsyncMock()
                result = await phantom_service.merge_phantom_into_user(db, phantom, active_user)

        assert result is True

    async def test_returns_false_when_phantom_has_no_subs(self):
        """Returns False when phantom had no subscriptions."""
        from app.services import phantom_service

        phantom = _make_user(id=2, subscriptions=[])
        active_user = _make_user(id=1)

        db = AsyncMock()
        db.refresh = AsyncMock()
        db.begin_nested = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=None),
        ))

        async def mock_execute_merge(*args, **kwargs):
            return _make_user(id=1)

        with patch.object(phantom_service, 'execute_merge', side_effect=mock_execute_merge):
            with patch.object(phantom_service, 'AuditLogCRUD') as mock_audit:
                mock_audit.create = AsyncMock()
                result = await phantom_service.merge_phantom_into_user(db, phantom, active_user)

        assert result is False

    async def test_returns_false_when_phantom_has_only_expired_sub(self):
        """Returns False when phantom's only subscription is expired."""
        from app.services import phantom_service

        phantom = _make_user(id=2, subscriptions=[_expired_subscription()])
        active_user = _make_user(id=1)

        db = AsyncMock()
        db.refresh = AsyncMock()
        db.begin_nested = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=None),
            __aexit__=AsyncMock(return_value=None),
        ))

        async def mock_execute_merge(*args, **kwargs):
            return _make_user(id=1)

        with patch.object(phantom_service, 'execute_merge', side_effect=mock_execute_merge):
            with patch.object(phantom_service, 'AuditLogCRUD') as mock_audit:
                mock_audit.create = AsyncMock()
                result = await phantom_service.merge_phantom_into_user(db, phantom, active_user)

        assert result is False


# ---------------------------------------------------------------------------
# C1: admin linking — no keep_subscription_from kwarg (source code check)
# ---------------------------------------------------------------------------


class TestAdminLinkingMergeSignature:
    """The admin linking route must not pass keep_subscription_from to execute_merge."""

    def test_admin_linking_source_no_keep_subscription_from(self):
        """Scan admin_user_linking source for the removed kwarg.

        This is a static guard: if someone re-introduces keep_subscription_from
        in the execute_merge call in admin_user_linking.py, this test will catch it.
        """
        import inspect
        from app.cabinet.routes import admin_user_linking

        source = inspect.getsource(admin_user_linking)
        # Find only the execute_merge call block — avoid false positives in comments
        # or the execute_merge definition itself (which legitimately doesn't have it).
        # We check that "keep_subscription_from" does not appear as a kwarg in a call.
        import re
        # Look for actual kwarg usage (key=value pattern near execute_merge call)
        calls = re.findall(r'execute_merge\([^)]{0,500}\)', source, re.DOTALL)
        for call in calls:
            assert 'keep_subscription_from' not in call, (
                f"admin_user_linking.execute_merge call must not pass "
                f"keep_subscription_from (param was removed from execute_merge).\n"
                f"Found in call: {call[:200]}"
            )
