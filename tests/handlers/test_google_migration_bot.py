from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.utils.decorators as _decorators
from app.handlers.admin import google_migration as gm


@pytest.mark.asyncio
async def test_send_handler_starts_service(monkeypatch):
    # Patch is_admin on the settings object used inside the decorators module.
    # We also need isinstance(callback, CallbackQuery) to pass, so we patch
    # the decorators module's settings.is_admin via the module-level reference.
    mock_settings = MagicMock()
    mock_settings.is_admin.return_value = True
    monkeypatch.setattr(_decorators, 'settings', mock_settings)

    # Also patch get_texts used in the decorator (safety net for unexpected branches)
    monkeypatch.setattr(_decorators, 'get_texts', MagicMock(return_value=MagicMock()))

    monkeypatch.setattr(gm.google_migration_service, 'start', AsyncMock(return_value=True))

    # Make callback look like a real CallbackQuery to pass isinstance check
    from aiogram.types import CallbackQuery
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=123, username='testadmin')
    callback.answer = AsyncMock()
    callback.message = AsyncMock()

    await gm.handle_send_invites(callback)
    gm.google_migration_service.start.assert_awaited_once()
    callback.answer.assert_awaited()


@pytest.mark.asyncio
async def test_send_handler_blocked_for_non_admin(monkeypatch):
    # Security regression guard: without @admin_required a non-admin could
    # trigger the mass email campaign. This test fails if the gate is removed.
    mock_settings = MagicMock()
    mock_settings.is_admin.return_value = False
    monkeypatch.setattr(_decorators, 'settings', mock_settings)
    monkeypatch.setattr(_decorators, 'get_texts', MagicMock(return_value=MagicMock()))

    monkeypatch.setattr(gm.google_migration_service, 'start', AsyncMock(return_value=True))

    from aiogram.types import CallbackQuery
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=999, username='random')
    callback.answer = AsyncMock()
    callback.message = AsyncMock()

    await gm.handle_send_invites(callback)
    gm.google_migration_service.start.assert_not_awaited()


def test_register_handlers_smoke():
    from aiogram import Dispatcher
    dp = Dispatcher()
    gm.register_handlers(dp)  # must not raise
