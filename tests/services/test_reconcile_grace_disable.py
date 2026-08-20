"""Тесты нового поведения step 4 в _reconcile_single_active_fallback:
grace истёк без оплаты → отключение (вместо бесконечного продления).

Покрывает:
1. Grace ELAPSED, zero balance → disable_user вызван, sub.status=EXPIRED, флаги сброшены,
   stats['disabled_grace_expired']==1, _patch_user_full НЕ вызван.
2. Grace NOT elapsed → ничего не происходит.
3. Balance gate: grace истёк, но require_zero_balance=True и balance > 0 → не отключаем.
4. Регрессия: повторный reconcile цикл НЕ вызывает _patch_user_full («продлили grace»).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import expiry_fallback_service

FALLBACK_UUID = 'fallback-squad-uuid'
GRACE_DAYS = 3


def _now():
    return datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)


def _subscription(**overrides) -> SimpleNamespace:
    now = _now()
    base = {
        'id': 777,
        'user_id': 99,
        'status': 'expired',
        'expiry_fallback_active': True,
        'traffic_fallback_active': False,
        'remnawave_id': 12345,
        'remnawave_short_uuid': None,
        'connected_squads': [],
        'pre_expiry_squads': ['orig-squad'],
        'pre_expiry_expire_at': None,
        'pre_expiry_traffic_limit_bytes': None,
        'expiry_fallback_started_at': now - timedelta(days=GRACE_DAYS + 1),
        'end_date': now - timedelta(days=1),
        'user': SimpleNamespace(balance_kopeks=0),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _stats() -> dict:
    return {
        'restored_external': 0,
        'restored_squad_changed': 0,
        'extended_grace': 0,
        'moved_lost_webhook': 0,
        'cleaned_total_expired': 0,
        'disabled_grace_expired': 0,
        'restored_stuck_no_flags': 0,
        'errors': 0,
    }


def _fake_rw_user(expire_at=None):
    """Fake Remnawave user — в fallback-скваде, expire_at близко."""
    if expire_at is None:
        expire_at = _now() + timedelta(hours=12)
    return SimpleNamespace(
        active_internal_squads=[FALLBACK_UUID],
        expire_at=expire_at,
        traffic_limit_bytes=0,
    )


async def _call_reconcile_single(
    sub,
    stats,
    monkeypatch,
    *,
    require_zero_balance: bool = False,
    cleanup_enabled: bool = False,
    grace_days: int = GRACE_DAYS,
    total_days: int = 90,
    patch_user_full_calls: list | None = None,
    rw_user=None,
):
    """Вызывает _reconcile_single_active_fallback с полным набором моков."""
    now = _now()

    # Мок: панель всегда находима
    async def fake_has_panel_identity(db, subscription):
        return True
    monkeypatch.setattr(expiry_fallback_service, '_has_panel_identity', fake_has_panel_identity)

    # Мок: Remnawave user
    if rw_user is None:
        rw_user = _fake_rw_user()
    async def fake_get_remnawave_user(remna_id_hint, db=None, subscription=None):
        return rw_user
    monkeypatch.setattr(expiry_fallback_service, '_get_remnawave_user', fake_get_remnawave_user)

    # Мок: _patch_user_full (не должен вызываться в новом поведении)
    if patch_user_full_calls is None:
        patch_user_full_calls = []
    async def fake_patch_user_full(remna_id_hint, **kwargs):
        patch_user_full_calls.append(kwargs)
        return True
    monkeypatch.setattr(expiry_fallback_service, '_patch_user_full', fake_patch_user_full)

    db = AsyncMock()

    await expiry_fallback_service._reconcile_single_active_fallback(
        db=db,
        sub=sub,
        fallback_uuid=FALLBACK_UUID,
        grace_days=grace_days,
        grace_gb=5,
        total_days=total_days,
        require_zero_balance=require_zero_balance,
        cleanup_enabled=cleanup_enabled,
        grace_extension_threshold_hours=1,
        now=now,
        stats=stats,
    )
    return patch_user_full_calls


# ---------------------------------------------------------------------------
# 1. Grace ELAPSED, zero balance → disable
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grace_elapsed_zero_balance_disables(monkeypatch):
    """Grace истёк, баланс 0 → _disable_and_expire_fallback вызван, stats обновлён."""
    now = _now()
    sub = _subscription(
        expiry_fallback_started_at=now - timedelta(days=GRACE_DAYS + 1),
        user=SimpleNamespace(balance_kopeks=0),
    )
    stats = _stats()
    disable_calls = []

    async def fake_disable(db, s, st, *, stat_key, log_msg, **kw):
        disable_calls.append({'stat_key': stat_key, 'log_msg': log_msg, **kw})
        s.status = 'expired'
        s.expiry_fallback_active = False
        s.traffic_fallback_active = False
        s.expiry_fallback_started_at = None
        st[stat_key] += 1
        return True
    monkeypatch.setattr(expiry_fallback_service, '_disable_and_expire_fallback', fake_disable)

    patch_calls = await _call_reconcile_single(sub, stats, monkeypatch, require_zero_balance=False)

    assert len(disable_calls) == 1, 'disable helper обязан быть вызван'
    assert disable_calls[0]['stat_key'] == 'disabled_grace_expired'
    assert 'grace истёк' in disable_calls[0]['log_msg']
    assert stats['disabled_grace_expired'] == 1
    assert sub.status == 'expired'
    assert sub.expiry_fallback_active is False
    assert patch_calls == [], '_patch_user_full не должен вызываться'


# ---------------------------------------------------------------------------
# 2. Grace NOT elapsed → ничего не происходит
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grace_not_elapsed_does_nothing(monkeypatch):
    """Grace ещё не истёк → disable helper и _patch_user_full не вызываются."""
    now = _now()
    sub = _subscription(
        expiry_fallback_started_at=now - timedelta(days=GRACE_DAYS - 1),
        user=SimpleNamespace(balance_kopeks=0),
    )
    stats = _stats()
    disable_calls = []

    async def fake_disable(db, s, st, *, stat_key, log_msg, **kw):
        disable_calls.append(stat_key)
        return True
    monkeypatch.setattr(expiry_fallback_service, '_disable_and_expire_fallback', fake_disable)

    patch_calls = await _call_reconcile_single(sub, stats, monkeypatch)

    assert disable_calls == [], 'disable не должен вызываться до истечения grace'
    assert patch_calls == [], '_patch_user_full не должен вызываться'
    assert stats['disabled_grace_expired'] == 0
    assert stats['extended_grace'] == 0


# ---------------------------------------------------------------------------
# 3. Balance gate: grace elapsed, balance > 0, require_zero_balance=True → не отключаем
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grace_elapsed_balance_gate_blocks_disable(monkeypatch):
    """Grace истёк, но у юзера положительный баланс и require_zero_balance=True → не отключаем."""
    now = _now()
    sub = _subscription(
        expiry_fallback_started_at=now - timedelta(days=GRACE_DAYS + 1),
        user=SimpleNamespace(balance_kopeks=5000),  # баланс есть
    )
    stats = _stats()
    disable_calls = []

    async def fake_disable(db, s, st, *, stat_key, log_msg, **kw):
        disable_calls.append(stat_key)
        return True
    monkeypatch.setattr(expiry_fallback_service, '_disable_and_expire_fallback', fake_disable)

    await _call_reconcile_single(sub, stats, monkeypatch, require_zero_balance=True)

    assert disable_calls == [], 'при положительном балансе отключение должно быть заблокировано'
    assert stats['disabled_grace_expired'] == 0


# ---------------------------------------------------------------------------
# 4. Регрессия: повторный цикл НЕ вызывает _patch_user_full
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_regression_no_extend_on_repeat_cycle(monkeypatch):
    """Повторный reconcile (expire_at < 1 дня) не продлевает grace через _patch_user_full."""
    now = _now()
    # expire_at меньше суток от now — именно то условие, что раньше тригерило продление
    sub = _subscription(
        expiry_fallback_started_at=now - timedelta(days=GRACE_DAYS - 1),  # grace ещё не истёк
        user=SimpleNamespace(balance_kopeks=0),
    )
    stats = _stats()

    # rw_user с expire_at < 1 дня (раньше тригерил «продлили grace»)
    rw_user = _fake_rw_user(expire_at=now + timedelta(hours=6))

    disable_calls = []
    async def fake_disable(db, s, st, *, stat_key, log_msg, **kw):
        disable_calls.append(stat_key)
        return True
    monkeypatch.setattr(expiry_fallback_service, '_disable_and_expire_fallback', fake_disable)

    patch_calls = await _call_reconcile_single(
        sub, stats, monkeypatch, rw_user=rw_user
    )

    assert patch_calls == [], 'старое поведение «продлили grace» через _patch_user_full должно быть удалено'
    assert disable_calls == [], 'grace не истёк — отключение не должно произойти'
    assert stats['extended_grace'] == 0
    assert stats['disabled_grace_expired'] == 0


# ---------------------------------------------------------------------------
# 5. _disable_and_expire_fallback directly: disable_user called with panel id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disable_and_expire_fallback_calls_disable_user(monkeypatch):
    """Хелпер _disable_and_expire_fallback напрямую вызывает api.disable_user(remna_id)."""
    sub = _subscription()
    stats = _stats()
    db = AsyncMock()

    disable_user_calls = []

    class FakeApi:
        async def disable_user(self, remna_id):
            disable_user_calls.append(remna_id)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    fake_api = FakeApi()

    class FakeRemnawaveService:
        def get_api_client(self):
            return fake_api

    monkeypatch.setattr(
        'app.services.remnawave_service.remnawave_service',
        FakeRemnawaveService(),
    )

    async def fake_get_panel_user_ref(api, db, *, subscription, user):
        return (None, sub.remnawave_id)

    monkeypatch.setattr(
        'app.services.remnawave_service.get_panel_user_ref',
        fake_get_panel_user_ref,
    )

    result = await expiry_fallback_service._disable_and_expire_fallback(
        db, sub, stats,
        stat_key='disabled_grace_expired',
        log_msg='test',
    )

    assert result is True
    assert disable_user_calls == [sub.remnawave_id], 'disable_user должен быть вызван с panel id'
    assert sub.status == 'expired'
    assert sub.expiry_fallback_active is False
    assert stats['disabled_grace_expired'] == 1
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. Regression: total_days cleanup still works via helper (existing behavior preserved)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_total_days_cleanup_still_works(monkeypatch):
    """Рефактор total_days блока через хелпер не сломал поведение: подписка старше total_days
    с нулевым балансом и cleanup_enabled=True → stats['cleaned_total_expired'] == 1."""
    now = _now()
    total_days = 5
    sub = _subscription(
        expiry_fallback_started_at=now - timedelta(days=total_days + 1),
        user=SimpleNamespace(balance_kopeks=0),
    )
    stats = _stats()
    disable_calls = []

    async def fake_disable(db, s, st, *, stat_key, log_msg, **kw):
        disable_calls.append(stat_key)
        st[stat_key] += 1
        return True
    monkeypatch.setattr(expiry_fallback_service, '_disable_and_expire_fallback', fake_disable)

    db = AsyncMock()
    await expiry_fallback_service._reconcile_single_active_fallback(
        db=db,
        sub=sub,
        fallback_uuid=FALLBACK_UUID,
        grace_days=GRACE_DAYS,
        grace_gb=5,
        total_days=total_days,
        require_zero_balance=False,
        cleanup_enabled=True,
        grace_extension_threshold_hours=1,
        now=now,
        stats=stats,
    )

    assert disable_calls == ['cleaned_total_expired'], 'total_days cleanup должен вызывать хелпер'
    assert stats['cleaned_total_expired'] == 1
