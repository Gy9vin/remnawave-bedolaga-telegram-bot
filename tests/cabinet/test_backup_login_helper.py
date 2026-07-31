"""Тесты хелпера needs_backup_login."""
from types import SimpleNamespace

import pytest

# нельзя импортировать до добавления функции — тест должен падать
from app.cabinet.routes.account_linking import needs_backup_login


def _user(telegram_id=None, email=None, password_hash=None, yandex_id=None) -> SimpleNamespace:
    return SimpleNamespace(
        telegram_id=telegram_id,
        email=email,
        password_hash=password_hash,
        yandex_id=yandex_id,
        google_id=None,
        discord_id=None,
        vk_id=None,
    )


def test_needs_backup_login_single_telegram():
    """Только Telegram → True."""
    user = _user(telegram_id=123456)
    assert needs_backup_login(user) is True


def test_needs_backup_login_two_methods():
    """Telegram + email → False."""
    user = _user(telegram_id=123456, email='a@b.com', password_hash='hash')
    assert needs_backup_login(user) is False


def test_needs_backup_login_oauth_plus_telegram():
    """Telegram + Yandex OAuth → False."""
    user = _user(telegram_id=123456, yandex_id='ya_123')
    assert needs_backup_login(user) is False


def test_needs_backup_login_email_only():
    """Только email → True."""
    user = _user(email='a@b.com', password_hash='hash')
    assert needs_backup_login(user) is True


def test_needs_backup_login_zero_methods():
    """Ноль методов → True (edge case, аккаунт-зомби)."""
    user = _user()
    assert needs_backup_login(user) is True
