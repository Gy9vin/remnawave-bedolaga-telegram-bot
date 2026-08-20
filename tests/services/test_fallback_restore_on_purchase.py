"""Покупка/продление с пустым connected_squads не должна оставлять юзера в fallback-скваде.

Прод-баг: `subscription_service.py` отправляет `active_internal_squads` в панель
только при непустом `subscription.connected_squads` (`if subscription.connected_squads:`).
Пустой список — ложное значение в Python, PATCH уходит частичным, панель
оставляет старые сквады (fallback). Следом вызывался `clear_fallback_after_purchase`,
который лишь снимал флаги `expiry_fallback_active`/`traffic_fallback_active` в БД,
ничего не проверяя в панели — подписка становилась невидима для
`reconcile_fallback_subscriptions` (фильтр по этим же флагам), хотя юзер
физически оставался в fallback-скваде.

Фикс:
1. Место покупки/продления зовёт `restore_fallback_after_purchase` (обёртка над
   уже проверенным `restore_from_fallback`) вместо `clear_fallback_after_purchase`.
   Флаги снимаются только если PATCH в панель подтверждён.
2. `reconcile_fallback_subscriptions` дополнительно подхватывает подписки БЕЗ
   fallback-флагов, застрявшие в fallback-скваде (случай, когда баг уже успел
   снять флаги раньше).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import expiry_fallback_service


class _FakeAPICtx:
    """Async-context для remnawave_service.get_api_client с bulk-чтением панели."""

    def __init__(self, panel_users):
        self._panel_users = panel_users

    async def __aenter__(self):
        return SimpleNamespace(get_all_users_stream=self._stream)

    async def __aexit__(self, *a):
        return False

    async def _stream(self, size=500):
        return self._panel_users


def _patch_fallback_panel(monkeypatch, panel_users):
    """Подменяет remnawave_service так, что панель отдаёт заданный список юзеров."""
    import app.services.remnawave_service as rw_mod

    fake = SimpleNamespace(get_api_client=lambda: _FakeAPICtx(panel_users))
    monkeypatch.setattr(rw_mod, 'remnawave_service', fake, raising=False)


def _subscription(**overrides) -> SimpleNamespace:
    base = {
        'id': 555,
        'user_id': 42,
        'status': 'active',
        'expiry_fallback_active': True,
        'traffic_fallback_active': False,
        'remnawave_id': 15758,
        'remnawave_short_uuid': None,
        'connected_squads': [],
        'pre_expiry_squads': ['squad-orig'],
        'pre_expiry_expire_at': None,
        'pre_expiry_traffic_limit_bytes': None,
        'expiry_fallback_started_at': datetime.now(UTC),
        'end_date': datetime.now(UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def has_panel_identity(monkeypatch):
    """Панельная идентичность резолвится (обходим сеть)."""

    async def fake_has_panel_identity(db, subscription):
        return True

    monkeypatch.setattr(expiry_fallback_service, '_has_panel_identity', fake_has_panel_identity)


@pytest.fixture
def patch_user_full_capture(monkeypatch):
    """Мокает _patch_user_full и фиксирует, с какими сквадами он был вызван."""
    calls = []

    async def fake_patch_user_full(remna_id_hint, *, squads=None, expire_at=None,
                                    traffic_limit_bytes=None, verify_squad_in=None,
                                    db=None, subscription=None):
        calls.append({'remna_id_hint': remna_id_hint, 'squads': squads})
        return True

    monkeypatch.setattr(expiry_fallback_service, '_patch_user_full', fake_patch_user_full)
    return calls


# ---------------------------------------------------------------------------
# restore_fallback_after_purchase — замена clear_fallback_after_purchase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purchase_with_empty_connected_squads_triggers_real_panel_patch(
    has_panel_identity, patch_user_full_capture,
):
    """Пустой connected_squads + стоящий fallback-флаг: покупка обязана дойти
    до реального PATCH в панель с непустым active_internal_squads, а не
    просто снять флаги в БД."""
    sub = _subscription(connected_squads=[], expiry_fallback_active=True)
    db = AsyncMock()

    ok = await expiry_fallback_service.restore_fallback_after_purchase(db, sub)

    assert ok is True
    assert len(patch_user_full_capture) == 1, 'ожидался реальный поход в панель (_patch_user_full)'
    assert patch_user_full_capture[0]['squads'], 'active_internal_squads не должен быть пустым'
    assert sub.expiry_fallback_active is False
    assert sub.traffic_fallback_active is False


@pytest.mark.asyncio
async def test_flags_not_cleared_when_panel_restore_fails(has_panel_identity, monkeypatch):
    """Если PATCH в панель не подтвердился — флаги fallback остаются, иначе
    подписка снова стала бы невидима для reconcile, хотя юзер так и сидит
    в fallback-скваде."""

    async def failing_patch_user_full(remna_id_hint, *, squads=None, expire_at=None,
                                       traffic_limit_bytes=None, verify_squad_in=None,
                                       db=None, subscription=None):
        return False

    monkeypatch.setattr(expiry_fallback_service, '_patch_user_full', failing_patch_user_full)

    sub = _subscription(connected_squads=[], expiry_fallback_active=True)
    db = AsyncMock()

    ok = await expiry_fallback_service.restore_fallback_after_purchase(db, sub)

    assert ok is False
    assert sub.expiry_fallback_active is True, 'флаг не должен сниматься без подтверждённого PATCH'
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_purchase_with_nonempty_connected_squads_and_no_fallback_is_noop(
    has_panel_identity, patch_user_full_capture,
):
    """Обычная покупка без fallback (флаги уже сняты, connected_squads заполнен) —
    поведение прежнее: панель не трогаем повторно, регрессии нет."""
    sub = _subscription(
        connected_squads=['squad-a', 'squad-b'],
        expiry_fallback_active=False,
        traffic_fallback_active=False,
    )
    db = AsyncMock()

    ok = await expiry_fallback_service.restore_fallback_after_purchase(db, sub)

    # restore_from_fallback возвращает True при отсутствии fallback-флагов
    # (уже всё в порядке) — контракт отличается от clear_fallback_after_purchase
    # (там False означало no-op), но важна панель: она не должна дёргаться повторно.
    assert ok is True
    assert len(patch_user_full_capture) == 0, 'панель не должна дёргаться повторно вне fallback-сценария'
    db.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# reconcile_fallback_subscriptions — страховочная сеть для уже застрявших
# ---------------------------------------------------------------------------


class _FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class _FakeResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return _FakeScalars(self._items)


@pytest.mark.asyncio
async def test_reconcile_picks_up_stuck_subscription_without_flags(monkeypatch):
    """Подписка без fallback-флагов (баг их уже снял раньше), с пустым
    connected_squads, но реально сидящая в панели в fallback-скваде — должна
    быть найдена и восстановлена страховочной сетью reconcile."""

    monkeypatch.setattr(expiry_fallback_service.settings, 'EXPIRY_FALLBACK_ENABLED', True)
    monkeypatch.setattr(expiry_fallback_service.settings, 'EXPIRY_FALLBACK_SQUAD_UUID', 'fallback-uuid')
    monkeypatch.setattr(expiry_fallback_service.settings, 'DEFAULT_SQUAD_UUID', 'default-squad')

    stuck_sub = _subscription(
        id=901,
        expiry_fallback_active=False,
        traffic_fallback_active=False,
        connected_squads=[],
        pre_expiry_squads=None,
        remnawave_id=4242,
    )

    db = AsyncMock()
    # 1) active-fallback query (flags True) -> пусто
    # 2) lost-webhook query (EXPIRED/LIMITED без флагов) -> пусто
    # 3) новая страховочная выборка (ACTIVE без флагов, пустой connected_squads) -> кандидат
    db.execute = AsyncMock(side_effect=[
        _FakeResult([]),
        _FakeResult([]),
        _FakeResult([stuck_sub]),
    ])

    # Панель-истина: юзер 4242 реально сидит в fallback-скваде.
    _patch_fallback_panel(monkeypatch, [
        SimpleNamespace(id=4242, active_internal_squads=['fallback-uuid']),
    ])

    patch_calls = []

    async def fake_patch_user_full(remna_id_hint, *, squads=None, expire_at=None,
                                    traffic_limit_bytes=None, verify_squad_in=None,
                                    db=None, subscription=None):
        patch_calls.append({'remna_id_hint': remna_id_hint, 'squads': squads})
        return True

    monkeypatch.setattr(expiry_fallback_service, '_patch_user_full', fake_patch_user_full)

    stats = await expiry_fallback_service.reconcile_fallback_subscriptions(db)

    assert len(patch_calls) == 1, 'reconcile обязан реально восстановить застрявшую подписку в панели'
    assert patch_calls[0]['squads'], 'active_internal_squads не должен быть пустым (fallback DEFAULT_SQUAD_UUID)'
    assert 'fallback-uuid' not in patch_calls[0]['squads']
    assert any(v for k, v in stats.items() if 'stuck' in k), 'ожидался счётчик восстановленных «застрявших без флагов» подписок'


@pytest.mark.asyncio
async def test_reconcile_skips_stuck_candidate_not_actually_in_fallback_squad(monkeypatch):
    """Кандидат с пустым connected_squads, но в панели НЕ в fallback-скваде —
    трогать не должны (например, юзер уже вручную возвращён)."""

    monkeypatch.setattr(expiry_fallback_service.settings, 'EXPIRY_FALLBACK_ENABLED', True)
    monkeypatch.setattr(expiry_fallback_service.settings, 'EXPIRY_FALLBACK_SQUAD_UUID', 'fallback-uuid')

    not_stuck_sub = _subscription(
        id=902,
        expiry_fallback_active=False,
        traffic_fallback_active=False,
        connected_squads=[],
        remnawave_id=4343,
    )

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _FakeResult([]),
        _FakeResult([]),
        _FakeResult([not_stuck_sub]),
    ])

    # Панель-истина: юзер 4343 НЕ в fallback-скваде → восстанавливать нечего.
    _patch_fallback_panel(monkeypatch, [
        SimpleNamespace(id=4343, active_internal_squads=['some-other-squad']),
    ])

    patch_calls = []

    async def fake_patch_user_full(remna_id_hint, **kwargs):
        patch_calls.append(kwargs)
        return True

    monkeypatch.setattr(expiry_fallback_service, '_patch_user_full', fake_patch_user_full)

    await expiry_fallback_service.reconcile_fallback_subscriptions(db)

    assert patch_calls == [], 'подписка не в fallback-скваде — восстанавливать нечего'
