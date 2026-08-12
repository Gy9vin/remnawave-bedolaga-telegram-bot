"""Панельная идентичность в однотарифном режиме лежит на пользователе.

Прод-баг: `restore_from_fallback`/`move_to_fallback`/`_reconcile_single_active_fallback`
проверяли наличие панельного пользователя только по колонкам подписки
(`subscription.remnawave_id` / `subscription.remnawave_short_uuid`). В
однотарифном режиме эти колонки пусты — идентичность лежит на
`users.remnawave_id`. Из-за этого `restore_from_fallback` СНИМАЛ флаги в БД
и возвращал True, вообще не сходив в панель — юзер оставался в
fallback-скваде, хотя БД считала, что его вернули.

Фикс: `_has_panel_identity` резолвит идентичность через `_resolve_panel_ref`
(тот же путь, что использует `_patch_user_full`), а не смотрит только на
колонки подписки.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import expiry_fallback_service


def _subscription(**overrides) -> SimpleNamespace:
    base = {
        'id': 777,
        'user_id': 42,
        'expiry_fallback_active': True,
        'traffic_fallback_active': False,
        'remnawave_id': None,
        'remnawave_short_uuid': None,
        'pre_expiry_squads': ['squad-orig'],
        'pre_expiry_expire_at': None,
        'pre_expiry_traffic_limit_bytes': None,
        'expiry_fallback_started_at': datetime.now(UTC),
        'end_date': None,
        'connected_squads': None,
        'traffic_used_gb': None,
        'traffic_reset_at': None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeApiCtx:
    """Фиктивный async context manager вместо remnawave_service.get_api_client()."""

    async def __aenter__(self):
        return SimpleNamespace()

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def mock_api_client(monkeypatch):
    """Подменяет remnawave_service.get_api_client(), чтобы не ходить в сеть."""
    from app.services import remnawave_service as remnawave_service_module

    monkeypatch.setattr(
        remnawave_service_module.remnawave_service,
        'get_api_client',
        lambda: _FakeApiCtx(),
    )


@pytest.fixture
def patch_user_full_capture(monkeypatch):
    """Мокает _patch_user_full и фиксирует, был ли он вызван (поход в панель)."""
    calls = []

    async def fake_patch_user_full(remna_id_hint, *, squads=None, expire_at=None,
                                    traffic_limit_bytes=None, verify_squad_in=None,
                                    db=None, subscription=None):
        calls.append({
            'remna_id_hint': remna_id_hint,
            'squads': squads,
            'expire_at': expire_at,
            'traffic_limit_bytes': traffic_limit_bytes,
        })
        return True

    monkeypatch.setattr(expiry_fallback_service, '_patch_user_full', fake_patch_user_full)
    return calls


@pytest.mark.asyncio
async def test_restore_reaches_panel_when_identity_only_on_user(
    monkeypatch, mock_api_client, patch_user_full_capture,
):
    """Пустые remnawave_id/short_uuid у подписки, но user.remnawave_id есть —
    restore_from_fallback обязан дойти до панели через _patch_user_full,
    а не просто снять флаги локально."""

    async def fake_resolve_panel_ref(api, db, subscription):
        return 999  # найден через user.remnawave_id

    monkeypatch.setattr(expiry_fallback_service, '_resolve_panel_ref', fake_resolve_panel_ref)

    sub = _subscription(remnawave_id=None, remnawave_short_uuid=None)
    db = AsyncMock()

    ok = await expiry_fallback_service.restore_from_fallback(db, sub, notify=False)

    assert ok is True
    assert len(patch_user_full_capture) == 1, '_patch_user_full должен быть вызван (поход в панель)'
    assert sub.expiry_fallback_active is False


@pytest.mark.asyncio
async def test_restore_clears_flags_with_warning_when_identity_unresolvable(
    monkeypatch, mock_api_client, patch_user_full_capture,
):
    """Если идентичность не резолвится вообще (ни колонки подписки, ни через
    юзера) — флаги можно снять (панельного аккаунта нет, возвращать некуда),
    но это обязано сопровождаться logger.warning, а не тихим выходом."""

    async def fake_resolve_panel_ref(api, db, subscription):
        return None

    monkeypatch.setattr(expiry_fallback_service, '_resolve_panel_ref', fake_resolve_panel_ref)

    warnings = []
    monkeypatch.setattr(
        expiry_fallback_service.logger,
        'warning',
        lambda *args, **kwargs: warnings.append((args, kwargs)),
    )

    sub = _subscription(remnawave_id=None, remnawave_short_uuid=None)
    db = AsyncMock()

    ok = await expiry_fallback_service.restore_from_fallback(db, sub, notify=False)

    assert ok is True
    assert len(patch_user_full_capture) == 0, 'в панель обращаться не должны — идентичность не резолвится'
    assert sub.expiry_fallback_active is False
    assert any(
        'restore_from_fallback' in str(args) or 'restore_from_fallback' in str(kwargs)
        for args, kwargs in warnings
    ), 'ожидался logger.warning про нерезолвленную идентичность'
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_move_to_fallback_works_when_identity_only_on_user(
    monkeypatch, mock_api_client, patch_user_full_capture,
):
    """move_to_fallback не должен отказывать в переводе, если идентичность
    подписки лежит на пользователе, а не в колонках subscriptions."""

    monkeypatch.setattr(expiry_fallback_service.settings, 'EXPIRY_FALLBACK_ENABLED', True)
    monkeypatch.setattr(expiry_fallback_service.settings, 'EXPIRY_FALLBACK_SQUAD_UUID', 'fallback-squad')
    monkeypatch.setattr(expiry_fallback_service.settings, 'EXPIRY_FALLBACK_DEV_MODE', False)

    async def fake_resolve_panel_ref(api, db, subscription):
        return 999  # найден через user.remnawave_id

    monkeypatch.setattr(expiry_fallback_service, '_resolve_panel_ref', fake_resolve_panel_ref)

    fake_rw_user = SimpleNamespace(
        active_internal_squads=['squad-orig'],
        expire_at=datetime.now(UTC),
        traffic_limit_bytes=None,
    )

    async def fake_get_remnawave_user(remna_id_hint, db=None, subscription=None):
        return fake_rw_user

    monkeypatch.setattr(expiry_fallback_service, '_get_remnawave_user', fake_get_remnawave_user)

    async def fake_reset_traffic(remna_id_hint, db=None, subscription=None):
        return True

    monkeypatch.setattr(expiry_fallback_service, '_reset_remnawave_traffic', fake_reset_traffic)

    sub = _subscription(
        expiry_fallback_active=False,
        traffic_fallback_active=False,
        remnawave_id=None,
        remnawave_short_uuid=None,
    )
    db = AsyncMock()

    ok = await expiry_fallback_service.move_to_fallback(db, sub, reason='expired', notify=False)

    assert ok is True
    assert len(patch_user_full_capture) == 1, '_patch_user_full должен быть вызван (поход в панель)'
    assert sub.expiry_fallback_active is True
