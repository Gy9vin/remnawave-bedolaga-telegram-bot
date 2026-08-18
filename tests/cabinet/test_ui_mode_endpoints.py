"""Эндпоинты персонального выбора интерфейса.

Ответ всегда несёт три величины: mode — что рисовать сейчас, choice — что
человек выбрал явно (null, если не выбирал), global_default — куда его
отправит глобальный флаг, если он сбросит выбор. Без choice фронт не сможет
отличить «выбрал полный» от «не выбирал при выключенном флаге».
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import info as info_routes


def _user(choice=None):
    return SimpleNamespace(id=1, telegram_id=782789067, cabinet_ui_mode=choice)


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_get_returns_global_default_when_user_did_not_choose(monkeypatch):
    monkeypatch.setattr(info_routes, '_read_lite_mode_enabled', AsyncMock(return_value=True))
    result = await info_routes.get_user_ui_mode(user=_user(None), db=_db())
    assert result == {'mode': 'simple', 'choice': None, 'global_default': 'simple'}


@pytest.mark.asyncio
async def test_get_explicit_choice_beats_global_default(monkeypatch):
    monkeypatch.setattr(info_routes, '_read_lite_mode_enabled', AsyncMock(return_value=True))
    result = await info_routes.get_user_ui_mode(user=_user('advanced'), db=_db())
    assert result == {'mode': 'advanced', 'choice': 'advanced', 'global_default': 'simple'}


@pytest.mark.asyncio
async def test_patch_saves_choice(monkeypatch):
    monkeypatch.setattr(info_routes, '_read_lite_mode_enabled', AsyncMock(return_value=False))
    user = _user(None)
    db = _db()
    result = await info_routes.update_user_ui_mode({'mode': 'simple'}, user=user, db=db)
    assert user.cabinet_ui_mode == 'simple'
    assert result == {'mode': 'simple', 'choice': 'simple', 'global_default': 'advanced'}
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_patch_null_resets_choice_to_global_default(monkeypatch):
    """Сброс выбора возвращает человека под глобальный флаг, а не в 'advanced'."""
    monkeypatch.setattr(info_routes, '_read_lite_mode_enabled', AsyncMock(return_value=True))
    user = _user('advanced')
    db = _db()
    result = await info_routes.update_user_ui_mode({'mode': None}, user=user, db=db)
    assert user.cabinet_ui_mode is None
    assert result == {'mode': 'simple', 'choice': None, 'global_default': 'simple'}


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['lite', '', 'SIMPLE_MODE', 5])
async def test_patch_rejects_invalid_mode(monkeypatch, bad):
    monkeypatch.setattr(info_routes, '_read_lite_mode_enabled', AsyncMock(return_value=False))
    user = _user(None)
    with pytest.raises(HTTPException) as exc:
        await info_routes.update_user_ui_mode({'mode': bad}, user=user, db=_db())
    assert exc.value.status_code == 400
    assert user.cabinet_ui_mode is None
