"""Прод-баг: продление триала БЕЗ tariff_id (автопродление на том же тарифе,
renewal в классическом режиме) не снимало is_trial — подписка оставалась
триальной после оплаты.

Причина: триал создаётся со status=ACTIVE (не TRIAL), а extend_subscription
снимал is_trial только через ветку tariff_id!=None или мёртвую ветку
status==TRIAL. Продление с tariff_id=None оставляло is_trial=True.

Фикс: любое реальное продление (days>0, convert_trial=True) снимает триальный
флаг независимо от tariff_id/status; бесплатный релейбл (convert_trial=False)
не трогается.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.crud import subscription as sub_crud
from app.database.models import SubscriptionStatus


def _db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.flush = AsyncMock()
    return db


def _trial_sub() -> MagicMock:
    now = datetime.now(UTC)
    s = MagicMock()
    s.id = 1
    s.user_id = 7
    s.is_trial = True
    s.status = SubscriptionStatus.ACTIVE.value  # триал создаётся ACTIVE, не TRIAL
    s.tariff_id = None  # классический режим / тот же тариф
    s.end_date = now + timedelta(days=2)  # активна
    s.start_date = now - timedelta(days=1)
    s.connected_squads = ['squad-1']
    s.traffic_limit_gb = 10
    s.traffic_used_gb = 5.0
    s.purchased_traffic_gb = 0
    s.device_limit = 1
    # Явные не-truthy значения, чтобы MagicMock не «подсовывал» истинные объекты
    s.expiry_fallback_active = False
    s.traffic_fallback_active = False
    s.is_daily_paused = False
    s.last_daily_charge_at = None
    s.auto_renewed_before_expiry = False
    s._converted_from_trial = False
    return s


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(sub_crud, '_lock_subscription_row', AsyncMock())
    monkeypatch.setattr(
        sub_crud, '_apply_base_limit_preserving_active_purchases',
        AsyncMock(return_value=(0, 10)),
    )
    monkeypatch.setattr(sub_crud, '_housekeep_expired_purchases', AsyncMock())
    monkeypatch.setattr(sub_crud, 'clear_notifications', AsyncMock())
    monkeypatch.setattr(sub_crud, 'deactivate_user_trial_subscriptions', AsyncMock(return_value=[]))
    # Не трогаем трафик в этом тесте — фокус на is_trial
    monkeypatch.setattr(type(sub_crud.settings), 'RESET_TRAFFIC_ON_PAYMENT', False, raising=False)
    monkeypatch.setattr(type(sub_crud.settings), 'is_classic_mode', lambda self: False)
    monkeypatch.setattr(type(sub_crud.settings), 'is_traffic_fixed', lambda self: False)


@pytest.mark.asyncio
async def test_extend_without_tariff_id_converts_trial(patched):
    """Продление триала (tariff_id=None) снимает is_trial — главный прод-баг."""
    sub = _trial_sub()
    result = await sub_crud.extend_subscription(_db(), sub, days=30)
    assert result.is_trial is False, 'is_trial должен сброситься при продлении без tariff_id'
    assert result.status == SubscriptionStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_extend_free_relabel_keeps_trial(patched):
    """convert_trial=False (бесплатный релейбл) НЕ снимает триал (баг #629889)."""
    sub = _trial_sub()
    result = await sub_crud.extend_subscription(_db(), sub, days=30, convert_trial=False)
    assert result.is_trial is True, 'бесплатный релейбл не должен конвертировать триал'


@pytest.mark.asyncio
async def test_extend_nontrial_unaffected(patched):
    """Обычная (не триальная) подписка: is_trial остаётся False, без сюрпризов."""
    sub = _trial_sub()
    sub.is_trial = False
    result = await sub_crud.extend_subscription(_db(), sub, days=30)
    assert result.is_trial is False


@pytest.mark.asyncio
async def test_trial_conversion_pulls_paid_traffic_classic(patched, monkeypatch):
    """Триал classic без traffic → подтягивается DEFAULT_TRAFFIC_LIMIT_GB (не триальный)."""
    monkeypatch.setattr(sub_crud.settings, 'DEFAULT_TRAFFIC_LIMIT_GB', 500, raising=False)
    apply_spy = AsyncMock(return_value=(0, 500))
    monkeypatch.setattr(sub_crud, '_apply_base_limit_preserving_active_purchases', apply_spy)
    sub = _trial_sub()  # tariff_id=None, is_trial=True
    await sub_crud.extend_subscription(_db(), sub, days=30)
    # _apply_base_limit вызван с платным base (500), а не триальным (10)
    called_bases = [c.args[2] for c in apply_spy.await_args_list]
    assert 500 in called_bases, f'ожидали платный base 500 в вызовах, получили {called_bases}'


@pytest.mark.asyncio
async def test_trial_conversion_pulls_unlimited_tariff_traffic(patched, monkeypatch):
    """Триал на безлимитном тарифе (0) без traffic → подтягивается 0 (безлимит)."""
    monkeypatch.setattr(
        'app.database.crud.tariff.get_tariff_by_id',
        AsyncMock(return_value=SimpleNamespace(id=5, traffic_limit_gb=0)),
    )
    apply_spy = AsyncMock(return_value=(0, 0))
    monkeypatch.setattr(sub_crud, '_apply_base_limit_preserving_active_purchases', apply_spy)
    sub = _trial_sub()
    sub.tariff_id = 5  # триал привязан к безлимитному тарифу
    await sub_crud.extend_subscription(_db(), sub, days=30)
    called_bases = [c.args[2] for c in apply_spy.await_args_list]
    assert 0 in called_bases, f'ожидали безлимит 0 в вызовах, получили {called_bases}'


@pytest.mark.asyncio
async def test_explicit_traffic_not_overridden(patched, monkeypatch):
    """Если traffic_limit_gb передан явно — резолв не переопределяет его."""
    resolve_spy = AsyncMock(return_value=999)
    monkeypatch.setattr(sub_crud, '_resolve_trial_paid_traffic_limit', resolve_spy)
    apply_spy = AsyncMock(return_value=(0, 42))
    monkeypatch.setattr(sub_crud, '_apply_base_limit_preserving_active_purchases', apply_spy)
    sub = _trial_sub()
    await sub_crud.extend_subscription(_db(), sub, days=30, traffic_limit_gb=42)
    resolve_spy.assert_not_awaited()  # явный traffic не трогаем
    called_bases = [c.args[2] for c in apply_spy.await_args_list]
    assert 42 in called_bases
