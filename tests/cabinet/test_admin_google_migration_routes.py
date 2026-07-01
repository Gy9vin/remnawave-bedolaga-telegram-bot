from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_routes_registered():
    from app.cabinet.routes import router
    paths = {r.path: r.methods for r in router.routes if hasattr(r, 'methods')}
    assert '/cabinet/admin/google-migration/status' in paths
    assert 'GET' in paths['/cabinet/admin/google-migration/status']
    assert 'POST' in paths['/cabinet/admin/google-migration/send']


@pytest.mark.asyncio
async def test_send_starts_service(monkeypatch):
    from app.cabinet.routes import admin_google_migration as mod
    monkeypatch.setattr(mod.google_migration_service, 'start', AsyncMock(return_value=True))
    resp = await mod.send_invites(admin=SimpleNamespace(id=1), db=AsyncMock())
    assert resp == {'started': True}


def test_at_risk_route_registered():
    from app.cabinet.routes import router
    paths = {r.path: r.methods for r in router.routes if hasattr(r, 'methods')}
    assert 'GET' in paths['/cabinet/admin/google-migration/at-risk']


@pytest.mark.asyncio
async def test_at_risk_returns_list(monkeypatch):
    from app.cabinet.routes import admin_google_migration as mod
    monkeypatch.setattr(mod, 'get_google_at_risk_users', AsyncMock(return_value=[{'id': 1, 'email': 'a@b.c', 'auth_type': 'google', 'has_telegram': False, 'blocked_bot': True}]))
    resp = await mod.get_at_risk_users(admin=SimpleNamespace(id=1), db=AsyncMock())
    assert resp['count'] == 1
    assert resp['users'][0]['blocked_bot'] is True


@pytest.mark.asyncio
async def test_status_returns_stats(monkeypatch):
    from app.cabinet.routes import admin_google_migration as mod
    monkeypatch.setattr(mod, 'get_google_migration_stats', AsyncMock(return_value={'total': 3, 'google_only': 2, 'with_password': 1}))
    monkeypatch.setattr(mod.google_migration_service, 'get_status', lambda: {'running': False, 'total': 0, 'sent': 0, 'failed': 0, 'started_at': None, 'finished_at': None})
    resp = await mod.get_migration_status(admin=SimpleNamespace(id=1), db=AsyncMock())
    assert resp['stats']['total'] == 3
    assert resp['run']['running'] is False
