"""Корень прод-инцидента с застрявшими в fallback: _patch_user_full в happy-path
(PATCH вернул 200) снимал флаги, НЕ проверив, что панель реально применила сквад.
Если панель ответила 200, но active_internal_squads не применились (частичный
PATCH), restore считал успех ложно → юзер оставался в fallback-скваде.

Фикс: при заданном verify_squad_in happy-path тоже сверяет фактическое состояние
панели (get_user_by_id) и возвращает False, если сквад не применён — тогда флаги
не снимаются и подписка остаётся видимой для reconcile/повтора.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import expiry_fallback_service as efs


class _FakeAPI:
    def __init__(self, applied_squads):
        self._applied = applied_squads
        self.update_calls: list = []
        self.get_calls = 0

    async def update_user(self, *, user_id, active_internal_squads=None,
                          expire_at=None, traffic_limit_bytes=None):
        self.update_calls.append(active_internal_squads)
        return SimpleNamespace(id=user_id, active_internal_squads=active_internal_squads)

    async def get_user_by_id(self, remna_id):
        self.get_calls += 1
        return SimpleNamespace(active_internal_squads=self._applied)


class _Ctx:
    def __init__(self, api):
        self.api = api

    async def __aenter__(self):
        return self.api

    async def __aexit__(self, *a):
        return False


def _patch_api(monkeypatch, api):
    import app.services.remnawave_service as rw

    monkeypatch.setattr(
        rw, 'remnawave_service',
        SimpleNamespace(get_api_client=lambda: _Ctx(api)),
        raising=False,
    )


@pytest.mark.asyncio
async def test_returns_false_when_panel_did_not_apply_squad(monkeypatch):
    """PATCH 200, но панель отдаёт fallback-сквад — сквад не применён → False."""
    api = _FakeAPI(applied_squads=['fallback-uuid'])
    _patch_api(monkeypatch, api)

    ok = await efs._patch_user_full(
        123, squads=['target-squad'], verify_squad_in=['target-squad'],
    )

    assert ok is False, 'ложный успех: панель не применила сквад, а вернулся True'
    assert api.get_calls == 1, 'happy-path обязан сверить фактическое состояние панели'


@pytest.mark.asyncio
async def test_returns_true_when_panel_applied_squad(monkeypatch):
    """PATCH 200 и панель реально в target-скваде → True."""
    api = _FakeAPI(applied_squads=['target-squad'])
    _patch_api(monkeypatch, api)

    ok = await efs._patch_user_full(
        123, squads=['target-squad'], verify_squad_in=['target-squad'],
    )

    assert ok is True
    assert api.get_calls == 1


@pytest.mark.asyncio
async def test_returns_true_when_target_is_subset_of_actual(monkeypatch):
    """Панель может держать больше сквадов — важно, что target входит в них."""
    api = _FakeAPI(applied_squads=['target-squad', 'extra-squad'])
    _patch_api(monkeypatch, api)

    ok = await efs._patch_user_full(
        123, squads=['target-squad'], verify_squad_in=['target-squad'],
    )

    assert ok is True


@pytest.mark.asyncio
async def test_no_verify_skips_panel_readback(monkeypatch):
    """Без verify_squad_in happy-path не делает лишний GET — обратная совместимость."""
    api = _FakeAPI(applied_squads=['whatever'])
    api.get_user_by_id = AsyncMock(side_effect=AssertionError('get_user_by_id не должен вызываться'))
    _patch_api(monkeypatch, api)

    ok = await efs._patch_user_full(123, squads=['x'])

    assert ok is True
