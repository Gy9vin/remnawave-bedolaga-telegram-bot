"""Tests for GET /cabinet/admin/users/merge/preview."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes.admin_user_linking import admin_merge_preview


_NOW = datetime(2026, 7, 30, 0, 0, 0, tzinfo=UTC)


def _make_sub(id, user_id, remnawave_id=None, subscription_url=None,
              subscription_crypto_link=None, remnawave_short_uuid=None,
              end_date=None, status='active', tariff_name='Basic'):
    return SimpleNamespace(
        id=id, user_id=user_id,
        remnawave_id=remnawave_id,
        subscription_url=subscription_url,
        subscription_crypto_link=subscription_crypto_link,
        remnawave_short_uuid=remnawave_short_uuid,
        end_date=end_date or datetime(2026, 12, 1, tzinfo=UTC),
        status=status,
        tariff=SimpleNamespace(name=tariff_name),
        tariff_id=1,
    )


def _make_user(id, subs=None, telegram_id=None, email=None):
    subs = subs or []
    return SimpleNamespace(
        id=id, username=f'user{id}', first_name='Test', email=email,
        telegram_id=telegram_id, password_hash=None,
        google_id=None, yandex_id=None, discord_id=None, vk_id=None,
        balance_kopeks=1000, subscriptions=subs,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        status='active', referral_code='ref', referred_by_id=None,
        remnawave_id=None,
    )


def _fake_db():
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one.return_value = 3  # active referrals
    db.execute = AsyncMock(return_value=result)
    return db


def _make_admin():
    return SimpleNamespace(id=99)


async def test_preview_route_returns_both_users():
    """Preview returns primary and secondary user info."""
    primary_sub = _make_sub(10, 1, remnawave_id='rw-p',
                            subscription_url='https://link/p', remnawave_short_uuid='short-p')
    primary = _make_user(1, subs=[primary_sub], telegram_id=111)
    secondary_sub = _make_sub(20, 2, remnawave_id='rw-s',
                              subscription_url='https://link/s', remnawave_short_uuid='short-s')
    secondary = _make_user(2, subs=[secondary_sub])
    db = _fake_db()

    fake_devices = {'devices': [
        {'hwid': 'hw1', 'app': 'SingBox', 'platform': 'iOS', 'lastSeen': '2026-07-20T10:00:00Z'},
    ], 'total': 1}

    async def fake_get_user(db_, uid):
        return {1: primary, 2: secondary}.get(uid)

    with patch('app.cabinet.routes.admin_user_linking.get_user_by_id', side_effect=fake_get_user), \
         patch('app.cabinet.routes.admin_user_linking._count_active_referrals',
               new_callable=AsyncMock, return_value=3) as mock_refs, \
         patch('app.cabinet.routes.admin_user_linking._get_remnawave_api') as mock_api_ctx:
        # set up async context manager
        mock_api = AsyncMock()
        mock_api.get_user_devices_all = AsyncMock(return_value=fake_devices)
        mock_api_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_api)
        mock_api_ctx.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await admin_merge_preview(
            primary_user_id=1,
            secondary_user_id=2,
            admin=_make_admin(),
            db=db,
        )

    assert result.primary.id == 1
    assert result.secondary.id == 2
    assert len(result.primary.subscriptions) == 1
    p_sub = result.primary.subscriptions[0]
    assert p_sub.subscription_id == 10
    assert p_sub.subscription_url == 'https://link/p'
    assert p_sub.remnawave_short_uuid == 'short-p'
    assert p_sub.devices_count == 1
    assert len(p_sub.devices) == 1
    assert p_sub.devices[0].app == 'SingBox'


async def test_preview_panel_unavailable_returns_null_devices():
    """If RemnaWave API throws, devices_count=None, devices=[], no 500."""
    primary_sub = _make_sub(10, 1, remnawave_id='rw-p')
    primary = _make_user(1, subs=[primary_sub])
    secondary = _make_user(2, subs=[])
    db = _fake_db()

    async def fake_get_user(db_, uid):
        return {1: primary, 2: secondary}.get(uid)

    with patch('app.cabinet.routes.admin_user_linking.get_user_by_id', side_effect=fake_get_user), \
         patch('app.cabinet.routes.admin_user_linking._count_active_referrals',
               new_callable=AsyncMock, return_value=0), \
         patch('app.cabinet.routes.admin_user_linking._get_remnawave_api') as mock_api_ctx:
        mock_api_ctx.return_value.__aenter__.side_effect = RuntimeError('panel down')
        mock_api_ctx.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await admin_merge_preview(
            primary_user_id=1,
            secondary_user_id=2,
            admin=_make_admin(),
            db=db,
        )

    assert result.primary.subscriptions[0].devices_count is None
    assert result.primary.subscriptions[0].devices == []


async def test_preview_same_user_raises_400():
    """primary_user_id == secondary_user_id → 400."""
    db = _fake_db()
    with pytest.raises(HTTPException) as exc:
        await admin_merge_preview(
            primary_user_id=5,
            secondary_user_id=5,
            admin=_make_admin(),
            db=db,
        )
    assert exc.value.status_code == 400


async def test_preview_unknown_user_raises_404():
    """Unknown user → 404."""
    db = _fake_db()

    async def fake_get_user(db_, uid):
        return None

    with patch('app.cabinet.routes.admin_user_linking.get_user_by_id', side_effect=fake_get_user):
        with pytest.raises(HTTPException) as exc:
            await admin_merge_preview(
                primary_user_id=1,
                secondary_user_id=2,
                admin=_make_admin(),
                db=db,
            )
    assert exc.value.status_code == 404


async def test_preview_route_registered(registered_paths):
    assert 'GET' in registered_paths.get('/cabinet/admin/users/merge/preview', set())
