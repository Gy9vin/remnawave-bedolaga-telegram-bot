"""При недоступности сети вход через Telegram использует прежние ключи.

Прод: httpx.ConnectTimeout в _get_jwks (запрос идёт через SOCKS-прокси, который
лёг) — и вход через Telegram в кабинете переставал работать у всех, хотя ключи
уже были получены часом раньше. Ключи Telegram меняются крайне редко, поэтому
протухший кеш безопаснее отказа: подпись всё равно проверяется.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cabinet.auth import telegram_auth


@pytest.fixture(autouse=True)
def _reset_cache():
    telegram_auth._jwks_cache = {}
    telegram_auth._jwks_cache_expiry = None
    yield
    telegram_auth._jwks_cache = {}
    telegram_auth._jwks_cache_expiry = None


def _failing_client(error: Exception) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=error)
    return client


@pytest.mark.asyncio
async def test_stale_cache_used_when_fetch_fails():
    import httpx

    telegram_auth._jwks_cache = {'keys': [{'kid': 'old'}]}
    telegram_auth._jwks_cache_expiry = datetime.now(UTC) - timedelta(seconds=1)  # протух

    with patch.object(telegram_auth.httpx, 'AsyncClient', return_value=_failing_client(httpx.ConnectTimeout('timeout'))):
        result = await telegram_auth._get_jwks()

    assert result == {'keys': [{'kid': 'old'}]}


@pytest.mark.asyncio
async def test_error_propagates_when_no_cache_at_all():
    """Без единого успешного запроса проверять подпись нечем — ошибка идёт наверх."""
    import httpx

    with patch.object(telegram_auth.httpx, 'AsyncClient', return_value=_failing_client(httpx.ConnectTimeout('timeout'))):
        with pytest.raises(httpx.ConnectTimeout):
            await telegram_auth._get_jwks()


@pytest.mark.asyncio
async def test_fresh_cache_short_circuits_without_network():
    telegram_auth._jwks_cache = {'keys': [{'kid': 'fresh'}]}
    telegram_auth._jwks_cache_expiry = datetime.now(UTC) + timedelta(hours=1)

    client = _failing_client(AssertionError('сеть не должна дёргаться при живом кеше'))
    with patch.object(telegram_auth.httpx, 'AsyncClient', return_value=client):
        result = await telegram_auth._get_jwks()

    assert result == {'keys': [{'kid': 'fresh'}]}
    client.get.assert_not_called()
