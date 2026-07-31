"""Тесты эндпоинта GET /cabinet/auth/account/backup-login-suggestion."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _user_single():
    """Пользователь с одним методом входа (Telegram)."""
    return SimpleNamespace(
        telegram_id=999, email=None, password_hash=None,
        yandex_id=None, google_id=None, discord_id=None, vk_id=None,
    )


def _user_two():
    """Пользователь с двумя методами (Telegram + email)."""
    return SimpleNamespace(
        telegram_id=999, email='u@example.com', password_hash='hash',
        yandex_id=None, google_id=None, discord_id=None, vk_id=None,
    )


@pytest.mark.asyncio
async def test_backup_suggestion_needs_backup_true():
    """Один метод → needs_backup=True."""
    from app.cabinet.routes.account_linking import get_backup_login_suggestion

    result = await get_backup_login_suggestion(user=_user_single())
    assert result.needs_backup is True


@pytest.mark.asyncio
async def test_backup_suggestion_needs_backup_false():
    """Два метода → needs_backup=False."""
    from app.cabinet.routes.account_linking import get_backup_login_suggestion

    result = await get_backup_login_suggestion(user=_user_two())
    assert result.needs_backup is False


@pytest.mark.asyncio
async def test_backup_suggestion_response_shape():
    """Ответ содержит поле needs_backup типа bool."""
    from app.cabinet.routes.account_linking import get_backup_login_suggestion

    result = await get_backup_login_suggestion(user=_user_single())
    assert hasattr(result, 'needs_backup')
    assert isinstance(result.needs_backup, bool)
