"""Tests for keep_subscription_id validation in admin_merge_users handler.

Pattern: call handler directly with fake db + fake users — same as
tests/cabinet/test_admin_user_activity.py.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes.admin_user_linking import admin_merge_users, AdminMergeUsersRequest


def _make_sub(id, user_id):
    return SimpleNamespace(id=id, user_id=user_id, remnawave_uuid=None,
                           status='active', autopay_enabled=False, end_date=None,
                           tariff_id=None, tariff=SimpleNamespace(name='T'))


def _make_user(id, subs=None):
    subs = subs or []
    return SimpleNamespace(
        id=id, status='active', telegram_id=None, email=None,
        subscriptions=subs,
        remnawave_uuid=None,
    )


def _make_admin():
    return SimpleNamespace(id=99)


def _fake_db():
    db = AsyncMock()
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


async def test_keep_subscription_id_validated_belongs_to_users():
    """keep_subscription_id that belongs to an unrelated user raises HTTP 400."""
    primary_sub = _make_sub(id=10, user_id=1)
    secondary_sub = _make_sub(id=20, user_id=2)
    primary = _make_user(1, subs=[primary_sub])
    secondary = _make_user(2, subs=[secondary_sub])

    db = _fake_db()

    async def fake_get_user(db_, uid):
        return {1: primary, 2: secondary}.get(uid)

    with patch('app.cabinet.routes.admin_user_linking.get_user_by_id', side_effect=fake_get_user), \
         patch('app.cabinet.routes.admin_user_linking.execute_merge', new_callable=AsyncMock) as mock_merge, \
         patch('app.cabinet.routes.admin_user_linking.flush_remnawave_deletions', new_callable=AsyncMock):
        mock_merge.return_value = primary

        request = AdminMergeUsersRequest(
            primary_user_id=1,
            secondary_user_id=2,
            keep_subscription_id=999,  # unrelated id
        )

        with pytest.raises(HTTPException) as exc_info:
            await admin_merge_users(request=request, admin=_make_admin(), db=db)

        assert exc_info.value.status_code == 400
        assert 'keep_subscription_id' in exc_info.value.detail.lower()


async def test_keep_subscription_id_none_passes_none_to_execute_merge():
    """keep_subscription_id=None passes None to execute_merge."""
    primary_sub = _make_sub(id=10, user_id=1)
    secondary_sub = _make_sub(id=20, user_id=2)
    primary = _make_user(1, subs=[primary_sub])
    secondary = _make_user(2, subs=[secondary_sub])
    db = _fake_db()

    async def fake_get_user(db_, uid):
        return {1: primary, 2: secondary}.get(uid)

    with patch('app.cabinet.routes.admin_user_linking.get_user_by_id', side_effect=fake_get_user), \
         patch('app.cabinet.routes.admin_user_linking.execute_merge', new_callable=AsyncMock) as mock_merge, \
         patch('app.cabinet.routes.admin_user_linking.flush_remnawave_deletions', new_callable=AsyncMock):
        mock_merge.return_value = primary

        request = AdminMergeUsersRequest(
            primary_user_id=1,
            secondary_user_id=2,
            keep_subscription_id=None,
        )
        await admin_merge_users(request=request, admin=_make_admin(), db=db)

    mock_merge.assert_called_once()
    call_kwargs = mock_merge.call_args.kwargs
    assert call_kwargs.get('keep_subscription_id') is None


async def test_keep_subscription_id_valid_primary_passes_to_execute_merge():
    """keep_subscription_id = primary sub id → passes through."""
    primary_sub = _make_sub(id=10, user_id=1)
    secondary_sub = _make_sub(id=20, user_id=2)
    primary = _make_user(1, subs=[primary_sub])
    secondary = _make_user(2, subs=[secondary_sub])
    db = _fake_db()

    async def fake_get_user(db_, uid):
        return {1: primary, 2: secondary}.get(uid)

    with patch('app.cabinet.routes.admin_user_linking.get_user_by_id', side_effect=fake_get_user), \
         patch('app.cabinet.routes.admin_user_linking.execute_merge', new_callable=AsyncMock) as mock_merge, \
         patch('app.cabinet.routes.admin_user_linking.flush_remnawave_deletions', new_callable=AsyncMock):
        mock_merge.return_value = primary

        request = AdminMergeUsersRequest(
            primary_user_id=1,
            secondary_user_id=2,
            keep_subscription_id=10,
        )
        await admin_merge_users(request=request, admin=_make_admin(), db=db)

    call_kwargs = mock_merge.call_args.kwargs
    assert call_kwargs.get('keep_subscription_id') == 10


async def test_merge_route_registered(registered_paths):
    assert 'POST' in registered_paths.get('/cabinet/admin/users/merge', set())
