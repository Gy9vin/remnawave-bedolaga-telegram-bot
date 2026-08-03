"""T6: get_panel_user_ref не должен ронять вызывающую бизнес-логику, если
попытка сохранить лениво резолвленный remnawave_id в БД не удалась.

Продовый баг: ``process_subscription`` (remnawave_service.py, sync_users_to_panel)
вызывает ``get_panel_user_ref`` из ``asyncio.gather`` над общей AsyncSession —
несколько подписок обрабатываются параллельно на ОДНОЙ сессии. Когда
``get_panel_user_ref`` лениво резолвит remna_id и пытается ``await db.commit()``,
а сессия в этот момент уже занята чужим commit()/flush() (или просто
конкурентным использованием той же AsyncSession), SQLAlchemy бросает
``IllegalStateChangeError: Method 'commit()' can't be called here; method
'_prepare_impl()' is already in progress`` — и вся синхронизация подписки
падает, хотя сам remna_id был успешно резолвлен.

Сценарии:
1. commit() бросает IllegalStateChangeError -> get_panel_user_ref НЕ
   пробрасывает исключение и возвращает корректный remna_id.
2. commit() бросает произвольную ошибку (например, обрыв соединения) -> то же
   самое: no raise, warning в лог, remna_id возвращается.
3. commit() отрабатывает нормально (обычный, "свой" контекст) -> бэкфилл
   по-прежнему сохраняется как раньше (commit реально вызывается).
4. v2 не звонит commit() вообще (поведение не изменилось).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IllegalStateChangeError

from app.services.remnawave_service import get_panel_user_ref


def _make_user(*, telegram_id: int = 111, remnawave_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        telegram_id=telegram_id,
        remnawave_uuid='panel-uuid',
        remnawave_id=remnawave_id,
        subscriptions=[],
    )


def _make_subscription(*, remnawave_short_uuid: str | None = 'shortXYZ') -> SimpleNamespace:
    return SimpleNamespace(remnawave_short_uuid=remnawave_short_uuid)


def _make_client(*, api_version: int = 3, resolved_id: int | None = 42) -> MagicMock:
    client = MagicMock()
    client.get_api_version = AsyncMock(return_value=api_version)
    client.resolve_user_id = AsyncMock(return_value=resolved_id)
    mock_panel_user = SimpleNamespace(id=resolved_id)
    client.get_user_by_telegram_id = AsyncMock(return_value=[mock_panel_user])
    return client


# ---------------------------------------------------------------------------
# 1: commit() бросает ровно ту ошибку из прод-трейсбека -> не пробрасывается
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_illegal_state_change_error_does_not_propagate():
    """Воспроизводит прод-баг: commit() внутри чужой/конкурентной транзакции."""
    user = _make_user(remnawave_id=None)
    sub = _make_subscription()
    client = _make_client(api_version=3, resolved_id=4242)

    db = AsyncMock()
    db.commit = AsyncMock(
        side_effect=IllegalStateChangeError(
            "Method 'commit()' can't be called here; method '_prepare_impl()' "
            'is already in progress and this would cause an unexpected state '
            'change to <SessionTransactionState.CLOSED: 5>'
        )
    )

    # Не должно бросить исключение наружу.
    result = await get_panel_user_ref(client, db, user=user, subscription=sub)

    assert result == (None, 4242)
    # remna_id резолвлен и записан в объект юзера в памяти, несмотря на то что
    # сохранить в БД не удалось.
    assert user.remnawave_id == 4242
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2: commit() бросает произвольную ошибку -> тоже не пробрасывается
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_arbitrary_error_does_not_propagate():
    user = _make_user(remnawave_id=None)
    sub = _make_subscription()
    client = _make_client(api_version=3, resolved_id=99)

    db = AsyncMock()
    db.commit = AsyncMock(side_effect=RuntimeError('connection reset by peer'))

    result = await get_panel_user_ref(client, db, user=user, subscription=sub)

    assert result == (None, 99)
    assert user.remnawave_id == 99
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3: "свой" контекст — commit() отрабатывает нормально, бэкфилл сохраняется
#    как раньше.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_success_still_persists_as_before():
    user = _make_user(remnawave_id=None)
    sub = _make_subscription()
    client = _make_client(api_version=3, resolved_id=7)

    db = AsyncMock()
    db.commit = AsyncMock()

    result = await get_panel_user_ref(client, db, user=user, subscription=sub)

    assert result == (None, 7)
    assert user.remnawave_id == 7
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4: v2 — commit() не вызывается вообще, поведение не изменилось.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v2_never_calls_commit():
    user = _make_user(remnawave_id=None)
    user.remnawave_uuid = 'v2-uuid'
    sub = _make_subscription()
    client = _make_client(api_version=2)

    db = AsyncMock()
    db.commit = AsyncMock()

    result = await get_panel_user_ref(client, db, user=user, subscription=sub)

    assert result == ('v2-uuid', None)
    db.commit.assert_not_awaited()
