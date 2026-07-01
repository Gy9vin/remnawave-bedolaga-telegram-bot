from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_blocked_active_route_registered():
    from app.cabinet.routes import router
    paths = {r.path: r.methods for r in router.routes if hasattr(r, 'methods')}
    assert '/cabinet/admin/broadcasts/{broadcast_id}/blocked-active' in paths
    assert 'GET' in paths['/cabinet/admin/broadcasts/{broadcast_id}/blocked-active']


@pytest.mark.asyncio
async def test_blocked_active_returns_count_and_users(monkeypatch):
    from app.cabinet.routes import admin_broadcasts as mod

    fake_users = [
        {
            'telegram_id': 123456,
            'username': 'testuser',
            'email': 'test@example.com',
            'tariff_name': 'Basic',
            'end_date': '2026-08-01T00:00:00+00:00',
            'days_left': 30,
        }
    ]
    monkeypatch.setattr(mod, 'get_broadcast_blocked_active_subscribers', AsyncMock(return_value=fake_users))

    resp = await mod.get_broadcast_blocked_active(
        broadcast_id=42,
        admin=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert resp['count'] == 1
    assert resp['users'][0]['telegram_id'] == 123456
    assert resp['users'][0]['tariff_name'] == 'Basic'
    mod.get_broadcast_blocked_active_subscribers.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_active_empty_when_none(monkeypatch):
    from app.cabinet.routes import admin_broadcasts as mod

    monkeypatch.setattr(mod, 'get_broadcast_blocked_active_subscribers', AsyncMock(return_value=[]))

    resp = await mod.get_broadcast_blocked_active(
        broadcast_id=99,
        admin=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert resp == {'count': 0, 'users': []}
