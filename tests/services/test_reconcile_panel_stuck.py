"""Тесты panel-truth ветки reconcile: восстановление ВСЕХ реально застрявших
в fallback-скваде активных оплаченных подписок — включая тот класс, что прежняя
эвристика пропускала (status=ACTIVE, флаги сняты, connected_squads НЕПУСТОЙ,
но панель держит юзера в fallback-скваде после недошедшего PATCH).

Корневой баг прод-инцидента: юзер оплатил (renew), БД активна, но панель в
fallback-скваде → доступа нет, а reconcile его не подхватывал.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import expiry_fallback_service as efs

FALLBACK_UUID = 'fallback-squad-uuid'
DEFAULT_UUID = 'default-squad-uuid'


# ---------------------------------------------------------------------------
# Чистые функции
# ---------------------------------------------------------------------------
def test_build_fallback_squad_ids_picks_only_users_in_fallback():
    panel_users = [
        SimpleNamespace(id=1, active_internal_squads=[FALLBACK_UUID]),
        SimpleNamespace(id=2, active_internal_squads=['other-squad']),
        SimpleNamespace(id=3, active_internal_squads=[{'uuid': FALLBACK_UUID}]),  # dict-форма
        SimpleNamespace(id=4, active_internal_squads=None),
        SimpleNamespace(id=None, active_internal_squads=[FALLBACK_UUID]),  # без id — пропуск
    ]
    assert efs.build_fallback_squad_ids(panel_users, FALLBACK_UUID) == {1, 3}


def test_choose_target_squads_priority():
    # connected → приоритет
    assert efs.choose_target_squads(['a'], DEFAULT_UUID, pre_expiry_squads=['b']) == ['a']
    # нет connected → pre_expiry
    assert efs.choose_target_squads([], DEFAULT_UUID, pre_expiry_squads=['b']) == ['b']
    assert efs.choose_target_squads(None, DEFAULT_UUID, pre_expiry_squads=['b']) == ['b']
    # нет ни того ни другого → default
    assert efs.choose_target_squads(None, DEFAULT_UUID, pre_expiry_squads=None) == [DEFAULT_UUID]
    # совсем ничего → пусто
    assert efs.choose_target_squads(None, None, pre_expiry_squads=None) == []


# ---------------------------------------------------------------------------
# Инфраструктура мока БД/панели
# ---------------------------------------------------------------------------
class _Result:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._items))


class _FakeAPI:
    def __init__(self, panel_users):
        self._pu = panel_users

    async def get_all_users_stream(self, size=500):
        return self._pu


class _FakeAPICtx:
    def __init__(self, api):
        self._api = api

    async def __aenter__(self):
        return self._api

    async def __aexit__(self, *a):
        return False


def _stuck_sub(**overrides) -> SimpleNamespace:
    """Застрявший после оплаты: active, флаги СНЯТЫ, connected_squads НЕПУСТОЙ."""
    base = {
        'id': 501,
        'user_id': 6442,
        'status': 'active',
        'expiry_fallback_active': False,
        'traffic_fallback_active': False,
        'remnawave_id': 6442,
        'remnawave_short_uuid': None,
        'connected_squads': ['dbc4d3a3'],
        'pre_expiry_squads': None,
        'pre_expiry_expire_at': None,
        'pre_expiry_traffic_limit_bytes': None,
        'expiry_fallback_started_at': None,
        'end_date': datetime.now(UTC) + timedelta(days=30),
        'user': SimpleNamespace(balance_kopeks=0),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _enable_fallback(monkeypatch):
    monkeypatch.setattr(efs.settings, 'EXPIRY_FALLBACK_ENABLED', True, raising=False)
    monkeypatch.setattr(efs.settings, 'EXPIRY_FALLBACK_SQUAD_UUID', FALLBACK_UUID, raising=False)
    monkeypatch.setattr(efs.settings, 'DEFAULT_SQUAD_UUID', DEFAULT_UUID, raising=False)


def _patch_panel(monkeypatch, panel_users):
    import app.services.remnawave_service as rw_mod

    fake_service = SimpleNamespace(get_api_client=lambda: _FakeAPICtx(_FakeAPI(panel_users)))
    monkeypatch.setattr(rw_mod, 'remnawave_service', fake_service, raising=False)


# ---------------------------------------------------------------------------
# Ключевой сценарий прод-инцидента
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reconcile_restores_stuck_paid_with_nonempty_connected_squads(monkeypatch):
    _enable_fallback(monkeypatch)
    sub = _stuck_sub()

    # active_fallback (ветка1) пусто, lost (ветка2) пусто, stuck chunk (ветка3) → [sub]
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_Result([]), _Result([]), _Result([sub])])

    # Панель держит именно этого юзера (id=6442) в fallback-скваде
    _patch_panel(monkeypatch, [SimpleNamespace(id=6442, active_internal_squads=[FALLBACK_UUID])])

    patch_full = AsyncMock(return_value=True)
    monkeypatch.setattr(efs, '_patch_user_full', patch_full)

    stats = await efs.reconcile_fallback_subscriptions(db)

    # PATCH ушёл в панель с целевым сквадом из connected_squads
    patch_full.assert_awaited_once()
    _, kwargs = patch_full.await_args
    assert kwargs['squads'] == ['dbc4d3a3']
    assert kwargs['verify_squad_in'] == ['dbc4d3a3']
    # Флаги сняты ТОЛЬКО после успешного PATCH
    assert sub.expiry_fallback_active is False
    assert sub.pre_expiry_squads is None
    assert stats['restored_stuck_no_flags'] == 1
    assert stats['errors'] == 0


@pytest.mark.asyncio
async def test_reconcile_does_not_touch_users_not_in_fallback_squad(monkeypatch):
    _enable_fallback(monkeypatch)
    sub = _stuck_sub()
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_Result([]), _Result([]), _Result([sub])])

    # Панель: юзер НЕ в fallback-скваде → fallback_ids пусто → chunk-цикл не выполнится
    _patch_panel(monkeypatch, [SimpleNamespace(id=6442, active_internal_squads=['other-squad'])])
    patch_full = AsyncMock(return_value=True)
    monkeypatch.setattr(efs, '_patch_user_full', patch_full)

    stats = await efs.reconcile_fallback_subscriptions(db)

    patch_full.assert_not_awaited()
    assert stats['restored_stuck_no_flags'] == 0
    # Третий db.execute (stuck chunk) вообще не вызывается — нет id в fallback
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_reconcile_flags_not_cleared_when_panel_patch_fails(monkeypatch):
    """PATCH не подтверждён → флаги НЕ снимаем (иначе повторяем исходный баг)."""
    _enable_fallback(monkeypatch)
    sub = _stuck_sub(expiry_fallback_active=True)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_Result([]), _Result([]), _Result([sub])])
    _patch_panel(monkeypatch, [SimpleNamespace(id=6442, active_internal_squads=[FALLBACK_UUID])])

    monkeypatch.setattr(efs, '_patch_user_full', AsyncMock(return_value=False))

    stats = await efs.reconcile_fallback_subscriptions(db)

    assert sub.expiry_fallback_active is True  # НЕ снято
    assert stats['restored_stuck_no_flags'] == 0
    assert stats['errors'] == 1


@pytest.mark.asyncio
async def test_reconcile_skips_when_no_target_squad(monkeypatch):
    """Нет ни connected, ни pre_expiry, ни DEFAULT → пропуск без PATCH."""
    _enable_fallback(monkeypatch)
    monkeypatch.setattr(efs.settings, 'DEFAULT_SQUAD_UUID', None, raising=False)
    sub = _stuck_sub(connected_squads=None, pre_expiry_squads=None)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_Result([]), _Result([]), _Result([sub])])
    _patch_panel(monkeypatch, [SimpleNamespace(id=6442, active_internal_squads=[FALLBACK_UUID])])

    patch_full = AsyncMock(return_value=True)
    monkeypatch.setattr(efs, '_patch_user_full', patch_full)

    stats = await efs.reconcile_fallback_subscriptions(db)

    patch_full.assert_not_awaited()
    assert stats['restored_stuck_no_flags'] == 0
