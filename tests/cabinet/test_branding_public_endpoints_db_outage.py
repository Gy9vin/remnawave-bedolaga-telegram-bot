"""Публичные branding-эндпоинты не должны падать вместе с базой.

Прод-траса: asyncpg не смог открыть новое соединение (DNS + SSL-handshake не
уложились в connect timeout 10 c), и `GET /cabinet/branding/themes` ответил 500.
Эти эндпоинты читаются на КАЖДОЙ загрузке кабинета, до авторизации, и у каждого
в коде уже есть дефолт на случай «настройка не задана». Значит, кратковременная
недоступность базы должна давать тот же дефолт, а не белый экран: тема, цвета и
название — оформление, из-за них кабинет открываться не перестаёт.

Записи (PATCH админом) это не касается: там молчаливый успех при упавшей базе
был бы враньём, и они по-прежнему падают честно.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import branding


class _DeadDb:
    """Сессия, которая валится ровно так же, как в проде: на первом execute."""

    async def execute(self, *_args, **_kwargs):
        raise TimeoutError('connection timeout')

    async def commit(self):
        raise TimeoutError('connection timeout')


@pytest.fixture
def dead_db():
    return _DeadDb()


@pytest.mark.asyncio
async def test_themes_fall_back_to_defaults(dead_db):
    response = await branding.get_enabled_themes(db=dead_db)

    assert response.dark is True
    assert response.light is True


@pytest.mark.asyncio
async def test_colors_fall_back_to_defaults(dead_db):
    response = await branding.get_theme_colors(db=dead_db)

    assert response.accent == branding.DEFAULT_THEME_COLORS['accent']


@pytest.mark.asyncio
async def test_animation_flag_falls_back_to_enabled(dead_db):
    response = await branding.get_animation_enabled(db=dead_db)

    assert response.enabled is True


@pytest.mark.asyncio
async def test_animation_config_falls_back_to_defaults(dead_db):
    response = await branding.get_animation_config(db=dead_db)

    assert response.enabled == branding.DEFAULT_ANIMATION_CONFIG['enabled']


@pytest.mark.asyncio
async def test_fullscreen_falls_back_to_default(dead_db):
    response = await branding.get_fullscreen_enabled(db=dead_db)

    assert response.enabled in (True, False)


@pytest.mark.asyncio
async def test_branding_name_falls_back_to_env_default(dead_db):
    response = await branding.get_branding(db=dead_db)

    assert response.name  # непустое имя, а не 500
    assert response.logo_letter


@pytest.mark.asyncio
async def test_email_auth_flag_falls_back(dead_db):
    response = await branding.get_email_auth_enabled(db=dead_db)

    assert response.enabled in (True, False)


@pytest.mark.asyncio
async def test_admin_write_still_fails_loudly(dead_db):
    """Молчаливый успех записи при упавшей базе — вранье админу."""
    with pytest.raises(TimeoutError):
        await branding.set_setting_value(dead_db, 'any_key', 'any_value')


@pytest.mark.asyncio
async def test_healthy_db_value_is_still_used(monkeypatch):
    """Фолбэк не должен подменять собой нормальное чтение."""
    monkeypatch.setattr(
        branding, 'get_setting_value', AsyncMock(return_value='{"dark": true, "light": false}')
    )

    response = await branding.get_enabled_themes(db=object())

    assert response.dark is True
    assert response.light is False
