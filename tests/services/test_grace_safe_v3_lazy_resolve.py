"""T4-fix: v3 disable/enable не должны падать ValueError'ом, когда

``users.remnawave_id`` ещё не забэкфиллен (миграция 9026 добавила колонку без
бэкфилла — она заполняется лениво).

Продовый баг: ``set_panel_user_enabled_state_grace_safe`` (grace-режим
DISABLED/OBSERVE — дефолтный режим, GRACE_ACCESS_MODE='false') звал
``api.disable_user/enable_user`` с ``remna_id=None`` без попытки резолва, и на
v3-панели это падало ``ValueError`` из ``_resolve_user_path`` (T4).

Сценарии здесь:
1. v3 + remnawave_id IS NULL в БД + grace DISABLED -> disable_remnawave_user
   резолвит remna_id лениво (через short_uuid/telegram_id) и отрабатывает.
2. То же для enable_remnawave_user.
3. v3 + резолв невозможен (нет db и негде взять short_uuid/telegram_id) ->
   управляемая неудача (False), не необработанный ValueError.
4. v2 -> поведение не меняется (uuid, без обращений к БД).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database.models import Subscription, User
from app.services import grace_access_runtime as gar
from app.services.grace_access_service import GraceAccessMode
from app.services.subscription_service import SubscriptionService
from tests.fixtures.sqlite_memory import memory_session


TABLES = (User.__table__, Subscription.__table__)


class FakeApiV3:
    """Минимальная v3 RemnaWaveAPI-заглушка для этих тестов.

    resolve_user_id всегда возвращает None (короткий uuid здесь не
    участвует) — резолв идёт через get_user_by_telegram_id, как в
    get_panel_user_ref, когда короткого uuid нет.
    """

    def __init__(self, *, panel_users_by_telegram: dict[int, int] | None = None) -> None:
        self._panel_users_by_telegram = panel_users_by_telegram or {}
        self.calls: list[tuple[str, dict]] = []

    async def get_api_version(self) -> int:
        return 3

    async def resolve_user_id(self, *, short_uuid=None, username=None, remna_id=None):
        return None

    async def get_user_by_telegram_id(self, telegram_id: int):
        remna_id = self._panel_users_by_telegram.get(telegram_id)
        if remna_id is None:
            return []
        return [SimpleNamespace(id=remna_id)]

    async def enable_user(self, uuid=None, remna_id=None):
        self.calls.append(('enable', {'uuid': uuid, 'remna_id': remna_id}))
        if remna_id is None:
            # Мимикрирует реальный _resolve_user_path на v3: uuid полностью
            # игнорируется, remna_id обязателен.
            raise ValueError('remna_id required for v3 but not provided — caller must backfill from T4')
        return SimpleNamespace(uuid=None, id=remna_id)

    async def disable_user(self, uuid=None, remna_id=None):
        self.calls.append(('disable', {'uuid': uuid, 'remna_id': remna_id}))
        if remna_id is None:
            raise ValueError('remna_id required for v3 but not provided — caller must backfill from T4')
        return SimpleNamespace(uuid=None, id=remna_id)


class FakeApiV2:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.get_api_version_calls = 0

    async def get_api_version(self) -> int:
        self.get_api_version_calls += 1
        return 2

    async def enable_user(self, uuid=None, remna_id=None):
        self.calls.append(('enable', {'uuid': uuid, 'remna_id': remna_id}))
        return SimpleNamespace(uuid=uuid, id=remna_id)

    async def disable_user(self, uuid=None, remna_id=None):
        self.calls.append(('disable', {'uuid': uuid, 'remna_id': remna_id}))
        return SimpleNamespace(uuid=uuid, id=remna_id)


def install_fake_api(monkeypatch: pytest.MonkeyPatch, api) -> None:
    @asynccontextmanager
    async def get_api_client(self):
        yield api

    monkeypatch.setattr(SubscriptionService, 'get_api_client', get_api_client)


@pytest.fixture(autouse=True)
def _grace_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Дефолтный продовый режим: GRACE_ACCESS_MODE='false' -> DISABLED."""
    monkeypatch.setattr(gar.grace_access_runtime, '_mode', GraceAccessMode.DISABLED)


# ---------------------------------------------------------------------------
# 1: disable_remnawave_user, v3, remnawave_id IS NULL, db передан -> success
# ---------------------------------------------------------------------------

async def test_disable_v3_lazy_resolve_backfills_remna_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = User(telegram_id=555, remnawave_uuid='panel-uuid-1', remnawave_id=None)
        db.add(user)
        await db.commit()

        api = FakeApiV3(panel_users_by_telegram={555: 4242})
        install_fake_api(monkeypatch, api)

        service = SubscriptionService()
        result = await service.disable_remnawave_user('panel-uuid-1', db=db)

        assert result is True
        assert api.calls == [('disable', {'uuid': 'panel-uuid-1', 'remna_id': 4242})]

        refreshed = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
        assert refreshed.remnawave_id == 4242


# ---------------------------------------------------------------------------
# 2: enable_remnawave_user, тот же сценарий
# ---------------------------------------------------------------------------

async def test_enable_v3_lazy_resolve_backfills_remna_id(monkeypatch: pytest.MonkeyPatch) -> None:
    async with memory_session(monkeypatch, TABLES) as db:
        user = User(telegram_id=777, remnawave_uuid='panel-uuid-2', remnawave_id=None)
        db.add(user)
        await db.commit()

        api = FakeApiV3(panel_users_by_telegram={777: 9001})
        install_fake_api(monkeypatch, api)

        service = SubscriptionService()
        result = await service.enable_remnawave_user('panel-uuid-2', db=db)

        assert result is True
        assert api.calls == [('enable', {'uuid': 'panel-uuid-2', 'remna_id': 9001})]

        refreshed = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
        assert refreshed.remnawave_id == 9001


# ---------------------------------------------------------------------------
# 3: v3, резолв невозможен (нет db, негде взять short_uuid/telegram_id) ->
#    управляемая неудача, а не необработанный ValueError.
# ---------------------------------------------------------------------------

async def test_disable_v3_no_db_no_resolution_returns_managed_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApiV3(panel_users_by_telegram={})
    install_fake_api(monkeypatch, api)

    service = SubscriptionService()
    result = await service.disable_remnawave_user('panel-uuid-orphan', db=None)

    assert result is False
    # api.disable_user не должен вызываться вообще с remna_id=None+uuid=None —
    # операция должна быть управляемо пропущена ДО обращения к панели.
    assert api.calls == []


async def test_enable_v3_no_db_no_resolution_returns_managed_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApiV3(panel_users_by_telegram={})
    install_fake_api(monkeypatch, api)

    service = SubscriptionService()
    result = await service.enable_remnawave_user('panel-uuid-orphan', db=None)

    assert result is False
    assert api.calls == []


# ---------------------------------------------------------------------------
# 4: v2 -> поведение прежнее (uuid, без лишних БД-обращений).
# ---------------------------------------------------------------------------

async def test_disable_v2_unchanged_uses_uuid_path(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApiV2()
    install_fake_api(monkeypatch, api)

    service = SubscriptionService()
    result = await service.disable_remnawave_user('v2-uuid-123', db=None)

    assert result is True
    assert api.calls == [('disable', {'uuid': 'v2-uuid-123', 'remna_id': None})]


async def test_enable_v2_unchanged_uses_uuid_path(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApiV2()
    install_fake_api(monkeypatch, api)

    service = SubscriptionService()
    result = await service.enable_remnawave_user('v2-uuid-123', db=None)

    assert result is True
    assert api.calls == [('enable', {'uuid': 'v2-uuid-123', 'remna_id': None})]


# ---------------------------------------------------------------------------
# Bonus: remna_id/user переданный явно вызывающим убирает лишний SELECT
# (item 3) и работает даже без db на v3.
# ---------------------------------------------------------------------------

async def test_disable_v3_explicit_remna_id_skips_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApiV3(panel_users_by_telegram={})
    install_fake_api(monkeypatch, api)

    service = SubscriptionService()
    result = await service.disable_remnawave_user('panel-uuid-3', db=None, remna_id=777)

    assert result is True
    assert api.calls == [('disable', {'uuid': 'panel-uuid-3', 'remna_id': 777})]


async def test_disable_v3_user_object_without_db_resolves_via_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_panel_user_ref умеет резолвить через telegram_id без db — только
    не коммитит найденный remna_id (некуда: сессии нет)."""
    api = FakeApiV3(panel_users_by_telegram={321: 55})
    install_fake_api(monkeypatch, api)

    user = User(telegram_id=321, remnawave_uuid='panel-uuid-4', remnawave_id=None)

    service = SubscriptionService()
    result = await service.disable_remnawave_user('panel-uuid-4', db=None, user=user)

    assert result is True
    assert api.calls == [('disable', {'uuid': 'panel-uuid-4', 'remna_id': 55})]
