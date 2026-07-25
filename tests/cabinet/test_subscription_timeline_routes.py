from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_self_timeline_route_registered():
    from app.cabinet.routes import router
    paths = {r.path: r.methods for r in router.routes if hasattr(r, 'methods')}
    assert 'GET' in paths['/cabinet/subscription/timeline']


@pytest.mark.asyncio
async def test_self_timeline_returns_events(monkeypatch):
    from app.cabinet.routes import subscription as mod
    rows = [{'index': 1, 'date': '2026-03-21T02:42:00+00:00', 'new_end': '2026-04-20T02:42:00+00:00',
             'period_days': 30, 'event_type': 'purchase', 'amount_kopeks': 10000,
             'prev_end': None, 'downtime_seconds': None, 'carried_seconds': None}]
    monkeypatch.setattr(mod, 'get_subscription_purchase_timeline', AsyncMock(return_value=rows))
    resp = await mod.get_subscription_timeline(user=SimpleNamespace(id=7), db=AsyncMock())
    assert resp['events'] == rows
    assert resp['since'] == '2026-03-21T02:42:00+00:00'


def test_admin_timeline_route_registered():
    from app.cabinet.routes import router
    paths = {r.path: r.methods for r in router.routes if hasattr(r, 'methods')}
    assert 'GET' in paths['/cabinet/admin/users/{user_id}/subscription-timeline']


@pytest.mark.asyncio
async def test_admin_timeline_returns_events(monkeypatch):
    from app.cabinet.routes import admin_users as mod
    rows = [{'index': 1, 'date': '2026-03-21T02:42:00+00:00', 'new_end': '2026-04-20T02:42:00+00:00'}]
    monkeypatch.setattr(mod, 'get_subscription_purchase_timeline', AsyncMock(return_value=rows))
    resp = await mod.get_user_subscription_timeline(user_id=5, admin=SimpleNamespace(id=1), db=AsyncMock())
    assert resp == {'events': rows, 'since': '2026-03-21T02:42:00+00:00'}
