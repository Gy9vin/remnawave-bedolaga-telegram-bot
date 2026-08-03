"""Третий по счёту продовый ValueError в этом классе багов (после fe008155 —
disable/enable, и 2042a0c6 — устройства в кабинете): ``expiry_fallback_service``
звал ``api.get_user_by_id``/``update_user``/``reset_user_traffic`` с
``remna_id=None`` (числовой id панели), когда ``users.remnawave_id`` ещё не
забэкфиллен (колонка заведена миграцией 9026 без массового бэкфилла —
заполняется лениво через ``get_panel_user_ref``).

Причина: ``_get_remnawave_user``/``_patch_user_full``/``_reset_remnawave_traffic``
резолвили юзера через ``getattr(subscription, 'user', None)`` — если
relationship не предзагружен через ``selectinload`` (а вызывающие этого модуля
разбросаны по webhook-сервису, monitoring, кабинету, admin-хендлерам и крону
reconcile — не все eager-load'ят ``.user``), и если резолв всё же не находил
remna_id, панель дёргалась с ``remna_id=None``.

Фикс: юзер резолвится через ``get_user_by_id(db, subscription.user_id)``
(eager-load, как в daily_subscription_service._get_panel_used_gb — тот же
паттерн уже был в кодовой базе), а если резолв всё равно не находит remna_id —
панель не дёргается вовсе (управляемая неудача + warning), как в
``grace_access_runtime.set_panel_user_enabled_state_grace_safe`` (T4).

3.0.0 полностью убрал панельный uuid — идентичность чисто числовая
(``remnawave_id``), поэтому v2/uuid-ветка (и её отдельные тестовые сценарии)
здесь больше не нужны.

Сценарии:
1. _get_remnawave_user: remnawave_id NULL + subscription БЕЗ eager-load .user
   (голый select без selectinload — тот самый MissingGreenlet-трюк) -> резолв
   через short_uuid отрабатывает, get_user_by_id зовётся с remna_id.
2. _get_remnawave_user: резолв невозможен -> None без похода в API.
3. _patch_user_full: те же два сценария.
4. _reset_remnawave_traffic: lazy resolve + no-resolution managed failure.
5. admin_user_fallback_restore (app/handlers/admin/users.py): второй забытый
   вызов _patch_user_full без db/subscription — регрессионный тест на реальный
   хендлер "вытащить из fallback вручную".
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.database.models import PromoGroup, Subscription, SubscriptionStatus, User, UserPromoGroup
from tests.fixtures.sqlite_memory import memory_session


TABLES = (
    User.__table__,
    Subscription.__table__,
    PromoGroup.__table__,
    UserPromoGroup.__table__,
)


class FakeApiV3:
    """RemnaWaveAPI-заглушка: воспроизводит реальный get_user_by_id/update_user/
    reset_user_traffic — падает, если numeric user_id не передан, как настоящий клиент."""

    def __init__(self, *, resolve_by_short_uuid: dict[str, int] | None = None) -> None:
        self._resolve_by_short_uuid = resolve_by_short_uuid or {}
        self.calls: list[tuple[str, dict]] = []

    async def resolve_user(self, *, user_id=None, short_uuid=None, username=None):
        if short_uuid is not None:
            resolved_id = self._resolve_by_short_uuid.get(short_uuid)
            return {'id': resolved_id} if resolved_id is not None else None
        return None

    async def find_users_by_telegram_id(self, telegram_id: int):
        return []

    def _require_user_id(self, user_id):
        if user_id is None:
            raise ValueError('user_id required but not provided — caller must backfill from T4')

    async def get_user_by_id(self, user_id=None):
        self.calls.append(('get_user_by_id', {'user_id': user_id}))
        self._require_user_id(user_id)
        return SimpleNamespace(id=user_id, active_internal_squads=[])

    async def update_user(self, user_id=None, **kwargs):
        self.calls.append(('update_user', {'user_id': user_id, **kwargs}))
        self._require_user_id(user_id)
        return SimpleNamespace(id=user_id)

    async def reset_user_traffic(self, user_id=None):
        self.calls.append(('reset_user_traffic', {'user_id': user_id}))
        self._require_user_id(user_id)
        return SimpleNamespace(id=user_id)


def install_fake_api(monkeypatch: pytest.MonkeyPatch, api) -> None:
    from app.services.remnawave_service import remnawave_service

    @asynccontextmanager
    async def get_api_client():
        yield api

    monkeypatch.setattr(remnawave_service, 'get_api_client', get_api_client)


async def _make_user_and_sub(
    db,
    *,
    telegram_id: int,
    remnawave_id: int | None,
    short_uuid: str | None = None,
) -> tuple[User, Subscription]:
    user = User(telegram_id=telegram_id, remnawave_id=remnawave_id)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    subscription = Subscription(
        user_id=user.id,
        status=SubscriptionStatus.ACTIVE.value,
        remnawave_short_uuid=short_uuid,
        end_date=datetime.now(UTC) + timedelta(days=30),
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return user, subscription


async def _fetch_bare_subscription(db, subscription_id: int) -> Subscription:
    """Голый select без selectinload(.user) — воспроизводит вызывающие места,

    которые не eager-load'ят relationship (webhook/monitoring/reconcile).
    Доступ к .user на этом объекте на AsyncSession без явной подгрузки
    вызвал бы MissingGreenlet до фикса.
    """
    result = await db.execute(select(Subscription).where(Subscription.id == subscription_id))
    return result.scalar_one()


# ---------------------------------------------------------------------------
# _get_remnawave_user
# ---------------------------------------------------------------------------


async def test_get_remnawave_user_lazy_resolve_via_short_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.expiry_fallback_service import _get_remnawave_user

    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await _make_user_and_sub(
            db, telegram_id=555, remnawave_id=None, short_uuid='short-1'
        )
        bare_sub = await _fetch_bare_subscription(db, subscription.id)

        api = FakeApiV3(resolve_by_short_uuid={'short-1': 4242})
        install_fake_api(monkeypatch, api)

        result = await _get_remnawave_user(bare_sub.remnawave_id, db=db, subscription=bare_sub)

        assert result is not None
        assert api.calls == [('get_user_by_id', {'user_id': 4242})]

        refreshed = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
        assert refreshed.remnawave_id == 4242


async def test_get_remnawave_user_no_resolution_returns_none_without_api_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.expiry_fallback_service import _get_remnawave_user

    async with memory_session(monkeypatch, TABLES) as db:
        _user, subscription = await _make_user_and_sub(
            db, telegram_id=556, remnawave_id=None, short_uuid=None
        )
        bare_sub = await _fetch_bare_subscription(db, subscription.id)

        api = FakeApiV3(resolve_by_short_uuid={})
        install_fake_api(monkeypatch, api)

        result = await _get_remnawave_user(bare_sub.remnawave_id, db=db, subscription=bare_sub)

        assert result is None
        # Управляемая неудача: панель не дёргалась вовсе.
        assert api.calls == []


# ---------------------------------------------------------------------------
# _patch_user_full
# ---------------------------------------------------------------------------


async def test_patch_user_full_lazy_resolve_via_short_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.expiry_fallback_service import _patch_user_full

    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await _make_user_and_sub(
            db, telegram_id=558, remnawave_id=None, short_uuid='short-2'
        )
        bare_sub = await _fetch_bare_subscription(db, subscription.id)

        api = FakeApiV3(resolve_by_short_uuid={'short-2': 9001})
        install_fake_api(monkeypatch, api)

        ok = await _patch_user_full(
            bare_sub.remnawave_id,
            squads=['fallback-squad'],
            db=db,
            subscription=bare_sub,
        )

        assert ok is True
        update_calls = [c for c in api.calls if c[0] == 'update_user']
        assert len(update_calls) == 1
        assert update_calls[0][1]['user_id'] == 9001
        assert update_calls[0][1]['active_internal_squads'] == ['fallback-squad']

        refreshed = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
        assert refreshed.remnawave_id == 9001


async def test_patch_user_full_no_resolution_returns_false_without_api_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.expiry_fallback_service import _patch_user_full

    async with memory_session(monkeypatch, TABLES) as db:
        _user, subscription = await _make_user_and_sub(
            db, telegram_id=559, remnawave_id=None, short_uuid=None
        )
        bare_sub = await _fetch_bare_subscription(db, subscription.id)

        api = FakeApiV3(resolve_by_short_uuid={})
        install_fake_api(monkeypatch, api)

        ok = await _patch_user_full(
            bare_sub.remnawave_id,
            squads=['fallback-squad'],
            db=db,
            subscription=bare_sub,
        )

        assert ok is False
        assert api.calls == []


# ---------------------------------------------------------------------------
# _reset_remnawave_traffic
# ---------------------------------------------------------------------------


async def test_reset_remnawave_traffic_lazy_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.expiry_fallback_service import _reset_remnawave_traffic

    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await _make_user_and_sub(
            db, telegram_id=561, remnawave_id=None, short_uuid='short-3'
        )
        bare_sub = await _fetch_bare_subscription(db, subscription.id)

        api = FakeApiV3(resolve_by_short_uuid={'short-3': 777})
        install_fake_api(monkeypatch, api)

        ok = await _reset_remnawave_traffic(bare_sub.remnawave_id, db=db, subscription=bare_sub)

        assert ok is True
        assert api.calls == [('reset_user_traffic', {'user_id': 777})]

        refreshed = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
        assert refreshed.remnawave_id == 777


async def test_reset_remnawave_traffic_no_resolution_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.expiry_fallback_service import _reset_remnawave_traffic

    async with memory_session(monkeypatch, TABLES) as db:
        _user, subscription = await _make_user_and_sub(
            db, telegram_id=562, remnawave_id=None, short_uuid=None
        )
        bare_sub = await _fetch_bare_subscription(db, subscription.id)

        api = FakeApiV3(resolve_by_short_uuid={})
        install_fake_api(monkeypatch, api)

        ok = await _reset_remnawave_traffic(bare_sub.remnawave_id, db=db, subscription=bare_sub)

        assert ok is False
        assert api.calls == []


# ---------------------------------------------------------------------------
# admin_user_fallback_restore (app/handlers/admin/users.py) — второй забытый
# вызов _patch_user_full без db/subscription.
# ---------------------------------------------------------------------------


def _unwrap(fn):
    while hasattr(fn, '__wrapped__'):
        fn = fn.__wrapped__
    return fn


def _callback(data: str):
    callback = MagicMock()
    callback.data = data
    callback.message = MagicMock()
    callback.message.edit_text = AsyncMock()
    callback.answer = AsyncMock()
    return callback


async def test_admin_user_fallback_restore_lazy_resolve_active_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Регрессия: admin_user_fallback_restore звал _patch_user_full без
    db=/subscription=, поэтому с ещё не забэкфилленным remnawave_id
    восстановление из fallback ВСЕГДА проваливалось (managed-failure после
    этого фикса) — хотя вся нужная информация (db, subscription) уже была в
    хендлере."""
    import app.handlers.admin.users as admin_users_mod

    async with memory_session(monkeypatch, TABLES) as db:
        user, subscription = await _make_user_and_sub(
            db, telegram_id=9999, remnawave_id=None, short_uuid='short-admin-1'
        )
        subscription.status = SubscriptionStatus.ACTIVE.value
        subscription.connected_squads = ['squad-a']
        await db.commit()

        api = FakeApiV3(resolve_by_short_uuid={'short-admin-1': 12345})
        install_fake_api(monkeypatch, api)

        handler = _unwrap(admin_users_mod.admin_user_fallback_restore)
        callback = _callback(f'admin_user_fallback_restore_{user.id}')

        await handler(callback, user, db)

        update_calls = [c for c in api.calls if c[0] == 'update_user']
        assert update_calls, 'ожидали PATCH юзера в панели'
        assert update_calls[0][1]['user_id'] == 12345

        text = callback.message.edit_text.call_args.args[0]
        assert '✅' in text

        refreshed = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
        assert refreshed.remnawave_id == 12345
