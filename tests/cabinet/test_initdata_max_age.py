"""Залипший на Android Telegram WebView initData (auth_date многодневной
давности) не должен отвергаться на входе в кабинет: подлинность гарантирует
HMAC-подпись, а возраст — конфигурируемый порог CABINET_TELEGRAM_INITDATA_MAX_AGE_DAYS.

Прод-инцидент: initData возрастом ~70 дней отвергался порогом 30 дней →
цикл login → «требуется авторизация через Telegram».
"""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

import pytest

from app.cabinet.auth.telegram_auth import validate_telegram_init_data
from app.config import settings


def _make_init_data(auth_date: int, bot_token: str) -> str:
    fields = {
        'auth_date': str(auth_date),
        'user': '{"id":123,"first_name":"A"}',
    }
    data_check_string = '\n'.join(f'{k}={v}' for k, v in sorted(fields.items()))
    secret_key = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, 'hash': h})


@pytest.fixture
def bot_token(monkeypatch):
    token = '123456:TEST-BOT-TOKEN'
    monkeypatch.setattr(settings, 'BOT_TOKEN', token, raising=False)
    return token


def test_getter_days_to_seconds(monkeypatch):
    monkeypatch.setattr(settings, 'CABINET_TELEGRAM_INITDATA_MAX_AGE_DAYS', 30, raising=False)
    assert settings.get_cabinet_telegram_initdata_max_age_seconds() == 30 * 86400
    monkeypatch.setattr(settings, 'CABINET_TELEGRAM_INITDATA_MAX_AGE_DAYS', 0, raising=False)
    assert settings.get_cabinet_telegram_initdata_max_age_seconds() == 0


def test_seventy_day_initdata_rejected_by_30d_threshold(bot_token):
    """Прежнее поведение: 70-дневный initData отвергается 30-дневным порогом."""
    auth_date = int(time.time()) - 70 * 86400
    init_data = _make_init_data(auth_date, bot_token)
    assert validate_telegram_init_data(init_data, max_age_seconds=30 * 86400) is None


def test_seventy_day_initdata_accepted_by_large_threshold(bot_token):
    """Фикс: с большим порогом (дефолт настройки) залипший initData проходит."""
    auth_date = int(time.time()) - 70 * 86400
    init_data = _make_init_data(auth_date, bot_token)
    threshold = settings.get_cabinet_telegram_initdata_max_age_seconds()
    result = validate_telegram_init_data(init_data, max_age_seconds=threshold)
    assert result is not None
    assert result.get('id') == 123


def test_zero_threshold_disables_age_check(bot_token):
    """max_age_seconds=0 → возраст не проверяется (только подпись)."""
    auth_date = int(time.time()) - 3650 * 86400  # 10 лет
    init_data = _make_init_data(auth_date, bot_token)
    assert validate_telegram_init_data(init_data, max_age_seconds=0) is not None


def test_bad_signature_still_rejected(bot_token):
    """Ослабление возраста НЕ ослабляет проверку подписи."""
    auth_date = int(time.time())
    init_data = _make_init_data(auth_date, 'другой-токен')  # подписан не тем токеном
    assert validate_telegram_init_data(init_data, max_age_seconds=0) is None


def test_future_date_still_rejected(bot_token):
    """Дата из будущего сверх дрейфа отвергается даже при отключённом пороге."""
    auth_date = int(time.time()) + 3650 * 86400
    init_data = _make_init_data(auth_date, bot_token)
    assert validate_telegram_init_data(init_data, max_age_seconds=0) is None
