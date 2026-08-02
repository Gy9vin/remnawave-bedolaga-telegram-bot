"""Тесты T4: lookup через stream (v3) и resolve_user_id.

Проверяет:
- get_user_by_telegram_id: v2 → by-telegram-id/{tg}, v3 → stream?telegramId=
- get_user_by_email: v2 → by-email/{email}, v3 → stream?email=
- resolve_user_id: v3 → POST /api/users/resolve, v2 → None, 404 → None
"""

from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest

from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _api() -> RemnaWaveAPI:
    return RemnaWaveAPI('http://panel.local', 'key')


def _force_version(api: RemnaWaveAPI, version: int) -> None:
    api._api_version = version


_V2_USER: dict = {
    'uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb',
    'shortUuid': 'abc123',
    'username': 'tguser',
    'status': 'ACTIVE',
    'trafficLimitBytes': 0,
    'trafficLimitStrategy': 'NO_RESET',
    'expireAt': '2030-01-01T00:00:00Z',
    'telegramId': 111,
    'email': 'test@example.com',
    'hwidDeviceLimit': None,
    'description': None,
    'tag': None,
    'subscriptionUrl': 'https://panel.local/sub/abc123',
    'activeInternalSquads': [],
    'createdAt': '2024-01-01T00:00:00Z',
    'updatedAt': '2024-01-01T00:00:00Z',
    'userTraffic': None,
}

_V3_USER: dict = {
    'id': 77,
    'shortUuid': 'xyz789',
    'username': 'tguser3',
    'status': 'ACTIVE',
    'trafficLimitBytes': 0,
    'trafficLimitStrategy': 'NO_RESET',
    'expireAt': '2030-01-01T00:00:00Z',
    'telegramId': 222,
    'email': 'v3@example.com',
    'hwidDeviceLimit': None,
    'description': None,
    'tag': None,
    'subscriptionUrl': 'https://panel.local/sub/xyz789',
    'activeInternalSquads': [],
    'createdAt': '2024-01-01T00:00:00Z',
    'updatedAt': '2024-01-01T00:00:00Z',
    'userTraffic': None,
}

# v3 stream-ответ — _make_request отдаёт конверт {'response': {...}}, как реальная панель.
_V3_STREAM_RESP = {'response': {'users': [_V3_USER], 'nextCursor': None, 'hasMore': False}}


# ---------------------------------------------------------------------------
# T4-1: get_user_by_telegram_id
# ---------------------------------------------------------------------------

async def test_get_user_by_telegram_id_v2_calls_old_endpoint():
    """v2: вызывает /api/users/by-telegram-id/{tg}."""
    api = _api()
    _force_version(api, 2)
    api._make_request = AsyncMock(return_value={'response': [_V2_USER]})
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda u: u)

    users = await api.get_user_by_telegram_id(111)

    call_args = api._make_request.call_args
    assert call_args.args[0] == 'GET'
    assert call_args.args[1] == '/api/users/by-telegram-id/111'
    assert len(users) == 1
    assert users[0].uuid == 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'


async def test_get_user_by_telegram_id_v3_calls_stream():
    """v3: вызывает GET /api/users/stream?telegramId=222&size=1."""
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value=_V3_STREAM_RESP)
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda u: u)

    users = await api.get_user_by_telegram_id(222)

    call_args = api._make_request.call_args
    assert call_args.args[0] == 'GET'
    assert call_args.args[1] == '/api/users/stream'
    params = call_args.kwargs.get('params') or (call_args.args[2] if len(call_args.args) > 2 else {})
    assert str(params.get('telegramId')) == '222'
    assert len(users) == 1
    assert users[0].id == 77
    assert users[0].uuid is None


async def test_get_user_by_telegram_id_v3_empty_stream():
    """v3: пустой список users → пустой результат."""
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value={'response': {'users': [], 'nextCursor': None, 'hasMore': False}})
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda u: u)

    users = await api.get_user_by_telegram_id(999)
    assert users == []


# ---------------------------------------------------------------------------
# T4-2: get_user_by_email
# ---------------------------------------------------------------------------

async def test_get_user_by_email_v2_calls_old_endpoint():
    """v2: вызывает /api/users/by-email/{email}."""
    api = _api()
    _force_version(api, 2)
    api._make_request = AsyncMock(return_value={'response': [_V2_USER]})
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda u: u)

    users = await api.get_user_by_email('test@example.com')

    call_args = api._make_request.call_args
    assert call_args.args[0] == 'GET'
    assert call_args.args[1] == '/api/users/by-email/test@example.com'
    assert len(users) == 1


async def test_get_user_by_email_v3_calls_stream():
    """v3: вызывает GET /api/users/stream?email=v3@example.com&size=1."""
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value=_V3_STREAM_RESP)
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda u: u)

    users = await api.get_user_by_email('v3@example.com')

    call_args = api._make_request.call_args
    assert call_args.args[0] == 'GET'
    assert call_args.args[1] == '/api/users/stream'
    params = call_args.kwargs.get('params') or (call_args.args[2] if len(call_args.args) > 2 else {})
    assert params.get('email') == 'v3@example.com'
    assert len(users) == 1
    assert users[0].id == 77


async def test_get_user_by_email_v3_empty_stream():
    """v3: пустой users → []."""
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value={'response': {'users': [], 'nextCursor': None, 'hasMore': False}})
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda u: u)

    users = await api.get_user_by_email('nobody@example.com')
    assert users == []


# ---------------------------------------------------------------------------
# T4-3: resolve_user_id
# ---------------------------------------------------------------------------

async def test_resolve_user_id_v3_by_short_uuid():
    """v3 + short_uuid: POST /api/users/resolve {shortUuid:...} → id."""
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value={'response': {'id': 77, 'username': 'tguser3', 'shortUuid': 'xyz789'}})

    result = await api.resolve_user_id(short_uuid='xyz789')

    call_args = api._make_request.call_args
    assert call_args.args[0] == 'POST'
    assert call_args.args[1] == '/api/users/resolve'
    body = call_args.args[2]
    assert body == {'shortUuid': 'xyz789'}
    assert result == 77


async def test_resolve_user_id_v3_by_username():
    """v3 + username: POST /api/users/resolve {username:...} → id."""
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value={'response': {'id': 77, 'username': 'tguser3', 'shortUuid': 'xyz789'}})

    result = await api.resolve_user_id(username='tguser3')

    call_args = api._make_request.call_args
    body = call_args.args[2]
    assert body == {'username': 'tguser3'}
    assert result == 77


async def test_resolve_user_id_v3_by_remna_id():
    """v3 + remna_id: POST /api/users/resolve {id:...} → id."""
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value={'response': {'id': 77, 'username': 'tguser3', 'shortUuid': 'xyz789'}})

    result = await api.resolve_user_id(remna_id=77)

    call_args = api._make_request.call_args
    body = call_args.args[2]
    assert body == {'id': 77}
    assert result == 77


async def test_resolve_user_id_v3_prefers_short_uuid_over_username():
    """v3: short_uuid имеет приоритет над username."""
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value={'response': {'id': 77, 'username': 'tguser3', 'shortUuid': 'xyz789'}})

    await api.resolve_user_id(short_uuid='xyz789', username='tguser3')

    body = api._make_request.call_args.args[2]
    assert body == {'shortUuid': 'xyz789'}


async def test_resolve_user_id_v3_404_returns_none():
    """v3: 404 → None (не бросает исключение)."""
    api = _api()
    _force_version(api, 3)
    err = RemnaWaveAPIError('Not found', status_code=404)
    api._make_request = AsyncMock(side_effect=err)

    result = await api.resolve_user_id(short_uuid='nosuchkey')
    assert result is None


async def test_resolve_user_id_v2_returns_none():
    """v2: resolve_user_id всегда возвращает None (endpoint не существует)."""
    api = _api()
    _force_version(api, 2)
    api._make_request = AsyncMock()

    result = await api.resolve_user_id(short_uuid='abc123')
    assert result is None
    api._make_request.assert_not_called()


async def test_resolve_user_id_no_key_raises():
    """Если не передан ни один ключ — ValueError."""
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock()

    with pytest.raises(ValueError, match='short_uuid|username|remna_id'):
        await api.resolve_user_id()
