from unittest.mock import AsyncMock

import pytest

from app.handlers.admin import google_migration as gm


@pytest.mark.asyncio
async def test_send_handler_starts_service(monkeypatch):
    monkeypatch.setattr(gm.google_migration_service, 'start', AsyncMock(return_value=True))
    callback = AsyncMock()
    callback.message = AsyncMock()
    await gm.handle_send_invites(callback)
    gm.google_migration_service.start.assert_awaited_once()
    callback.answer.assert_awaited()


def test_register_handlers_smoke():
    from aiogram import Dispatcher
    dp = Dispatcher()
    gm.register_handlers(dp)  # must not raise
