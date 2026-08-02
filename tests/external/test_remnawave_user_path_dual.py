"""Тесты T3: двойной путь пользователя v2/v3 и двойной парсер.

Параметризованы по api_version (2 и 3). Мокируем _make_request на экземпляре
и форсируем _api_version в кэш, чтобы get_api_version() вернул его без сети.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, call

import pytest

from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError


def _api() -> RemnaWaveAPI:
    return RemnaWaveAPI('http://panel.local', 'key')


# ---------------------------------------------------------------------------
# Минимальный v2 user-ответ (JSON dict как из response['response'])
# ---------------------------------------------------------------------------

_V2_USER: dict = {
    'uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb',
    'shortUuid': 'abc123',
    'username': 'testuser',
    'status': 'ACTIVE',
    'trafficLimitBytes': 0,
    'trafficLimitStrategy': 'NO_RESET',
    'expireAt': '2030-01-01T00:00:00Z',
    'telegramId': None,
    'email': None,
    'hwidDeviceLimit': None,
    'description': None,
    'tag': None,
    'subscriptionUrl': 'https://panel.local/sub/abc123',
    'activeInternalSquads': [],
    'createdAt': '2024-01-01T00:00:00Z',
    'updatedAt': '2024-01-01T00:00:00Z',
    'userTraffic': None,
}

# ---------------------------------------------------------------------------
# Минимальный v3 user-ответ (нет uuid, есть id)
# ---------------------------------------------------------------------------

_V3_USER: dict = {
    'id': 42,
    'shortUuid': 'xyz789',
    'username': 'testuser3',
    'status': 'ACTIVE',
    'trafficLimitBytes': 0,
    'trafficLimitStrategy': 'NO_RESET',
    'expireAt': '2030-01-01T00:00:00Z',
    'telegramId': None,
    'email': None,
    'hwidDeviceLimit': None,
    'description': None,
    'tag': None,
    'subscriptionUrl': 'https://panel.local/sub/xyz789',
    'activeInternalSquads': [],
    'createdAt': '2024-01-01T00:00:00Z',
    'updatedAt': '2024-01-01T00:00:00Z',
    'userTraffic': None,
}


# ---------------------------------------------------------------------------
# Вспомогательная функция: задаёт api_version в кэш без сетевого зондирования
# ---------------------------------------------------------------------------

def _force_version(api: RemnaWaveAPI, version: int) -> None:
    """Форсировать закэшированную версию API без сетевого вызова."""
    api._api_version = version


# ---------------------------------------------------------------------------
# T3-1: get_user_by_uuid — путь и парсер
# ---------------------------------------------------------------------------

async def test_get_user_by_uuid_v2_path_and_parser():
    """v2: путь /api/users/{uuid}, uuid в объекте."""
    api = _api()
    _force_version(api, 2)

    uuid_val = 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'
    api._make_request = AsyncMock(return_value={'response': _V2_USER})
    # Отключаем happ-обогащение (не нужно для теста пути)
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda u: u)

    user = await api.get_user_by_uuid(uuid=uuid_val)

    call_args = api._make_request.call_args
    assert call_args.args[0] == 'GET'
    assert call_args.args[1] == f'/api/users/{uuid_val}'
    assert user is not None
    assert user.uuid == uuid_val
    assert user.short_uuid == 'abc123'


async def test_get_user_by_uuid_v3_path_and_parser():
    """v3: путь /api/users/{id}, id в объекте, uuid=None."""
    api = _api()
    _force_version(api, 3)

    api._make_request = AsyncMock(return_value={'response': _V3_USER})
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda u: u)

    user = await api.get_user_by_uuid(uuid=None, remna_id=42)

    call_args = api._make_request.call_args
    assert call_args.args[0] == 'GET'
    assert call_args.args[1] == '/api/users/42'
    assert user is not None
    assert user.id == 42
    assert user.uuid is None
    assert user.short_uuid == 'xyz789'


async def test_get_user_by_uuid_v3_no_remna_id_raises():
    """v3 без remna_id: ValueError с понятным сообщением."""
    api = _api()
    _force_version(api, 3)

    api._make_request = AsyncMock()

    with pytest.raises(ValueError, match='remna_id'):
        await api.get_user_by_uuid(uuid=None, remna_id=None)


# ---------------------------------------------------------------------------
# T3-2: enable_user
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('api_version,user_data,ident,expected_path', [
    (2, _V2_USER, {'uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'}, '/api/users/aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb/actions/enable'),
    (3, _V3_USER, {'remna_id': 42}, '/api/users/42/actions/enable'),
])
async def test_enable_user_path(api_version, user_data, ident, expected_path):
    api = _api()
    _force_version(api, api_version)
    api._make_request = AsyncMock(return_value={'response': user_data})
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda u: u)

    await api.enable_user(**ident)

    call_args = api._make_request.call_args
    assert call_args.args[1] == expected_path


# ---------------------------------------------------------------------------
# T3-3: disable_user
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('api_version,user_data,ident,expected_path', [
    (2, _V2_USER, {'uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'}, '/api/users/aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb/actions/disable'),
    (3, _V3_USER, {'remna_id': 42}, '/api/users/42/actions/disable'),
])
async def test_disable_user_path(api_version, user_data, ident, expected_path):
    api = _api()
    _force_version(api, api_version)
    api._make_request = AsyncMock(return_value={'response': user_data})
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda u: u)

    await api.disable_user(**ident)

    call_args = api._make_request.call_args
    assert call_args.args[1] == expected_path


# ---------------------------------------------------------------------------
# T3-4: delete_user
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('api_version,ident,expected_path', [
    (2, {'uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'}, '/api/users/aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'),
    (3, {'remna_id': 42}, '/api/users/42'),
])
async def test_delete_user_path(api_version, ident, expected_path):
    api = _api()
    _force_version(api, api_version)
    api._make_request = AsyncMock(return_value={'response': {'isDeleted': True}})

    await api.delete_user(**ident)

    call_args = api._make_request.call_args
    assert call_args.args[0] == 'DELETE'
    assert call_args.args[1] == expected_path


# ---------------------------------------------------------------------------
# T3-5: reset_user_traffic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('api_version,user_data,ident,expected_path', [
    (2, _V2_USER, {'uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'}, '/api/users/aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb/actions/reset-traffic'),
    (3, _V3_USER, {'remna_id': 42}, '/api/users/42/actions/reset-traffic'),
])
async def test_reset_user_traffic_path(api_version, user_data, ident, expected_path):
    api = _api()
    _force_version(api, api_version)
    api._make_request = AsyncMock(return_value={'response': user_data})
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda u: u)

    await api.reset_user_traffic(**ident)

    call_args = api._make_request.call_args
    assert call_args.args[1] == expected_path


# ---------------------------------------------------------------------------
# T3-6: revoke_user_subscription
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('api_version,user_data,ident,expected_path', [
    (2, _V2_USER, {'uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'}, '/api/users/aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb/actions/revoke'),
    (3, _V3_USER, {'remna_id': 42}, '/api/users/42/actions/revoke'),
])
async def test_revoke_user_subscription_path(api_version, user_data, ident, expected_path):
    api = _api()
    _force_version(api, api_version)
    api._make_request = AsyncMock(return_value={'response': user_data})
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda u: u)

    await api.revoke_user_subscription(**ident)

    call_args = api._make_request.call_args
    assert call_args.args[1] == expected_path


# ---------------------------------------------------------------------------
# T3-7: update_user — путь через PATCH /api/users и тело запроса
# ---------------------------------------------------------------------------

async def test_update_user_v2_body_has_uuid():
    """v2: PATCH /api/users, тело содержит uuid."""
    api = _api()
    _force_version(api, 2)
    api._make_request = AsyncMock(return_value={'response': _V2_USER})
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda u: u)

    uuid_val = 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'
    await api.update_user(uuid=uuid_val)

    call_args = api._make_request.call_args
    assert call_args.args[0] == 'PATCH'
    assert call_args.args[1] == '/api/users'
    body = call_args.args[2]
    assert body.get('uuid') == uuid_val
    assert 'id' not in body


async def test_update_user_v3_body_has_id():
    """v3: PATCH /api/users, тело содержит id (int)."""
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value={'response': _V3_USER})
    api.enrich_user_with_happ_link = AsyncMock(side_effect=lambda u: u)

    await api.update_user(uuid=None, remna_id=42)

    call_args = api._make_request.call_args
    assert call_args.args[0] == 'PATCH'
    assert call_args.args[1] == '/api/users'
    body = call_args.args[2]
    assert body.get('id') == 42
    assert 'uuid' not in body


# ---------------------------------------------------------------------------
# T3-8: get_user_accessible_nodes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('api_version,ident,expected_path', [
    (2, {'uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'}, '/api/users/aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb/accessible-nodes'),
    (3, {'remna_id': 42}, '/api/users/42/accessible-nodes'),
])
async def test_get_user_accessible_nodes_path(api_version, ident, expected_path):
    api = _api()
    _force_version(api, api_version)
    api._make_request = AsyncMock(return_value={'response': {'activeNodes': []}})

    await api.get_user_accessible_nodes(**ident)

    call_args = api._make_request.call_args
    assert call_args.args[1] == expected_path


# ---------------------------------------------------------------------------
# T3-9: get_user_devices / get_user_devices_all
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('api_version,ident,expected_path', [
    (2, {'user_uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'}, '/api/hwid/devices/aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'),
    (3, {'user_uuid': None, 'remna_id': 42}, '/api/hwid/devices/42'),
])
async def test_get_user_devices_path(api_version, ident, expected_path):
    api = _api()
    _force_version(api, api_version)
    api._make_request = AsyncMock(return_value={'response': {'total': 0, 'devices': []}})

    await api.get_user_devices(**ident)

    call_args = api._make_request.call_args
    assert call_args.args[1] == expected_path


@pytest.mark.parametrize('api_version,ident,expected_path', [
    (2, {'user_uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'}, '/api/hwid/devices/aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'),
    (3, {'user_uuid': None, 'remna_id': 42}, '/api/hwid/devices/42'),
])
async def test_get_user_devices_all_path(api_version, ident, expected_path):
    api = _api()
    _force_version(api, api_version)
    api._make_request = AsyncMock(return_value={'response': {'total': 0, 'devices': []}})

    await api.get_user_devices_all(**ident)

    call_args = api._make_request.call_args
    assert call_args.args[1] == expected_path


# ---------------------------------------------------------------------------
# T3-10: _resolve_user_path unit tests
# ---------------------------------------------------------------------------

def test_resolve_user_path_v2_returns_uuid():
    api = _api()
    _force_version(api, 2)
    result = api._resolve_user_path(uuid='some-uuid-val', remna_id=None)
    assert result == 'some-uuid-val'


def test_resolve_user_path_v3_returns_str_id():
    api = _api()
    _force_version(api, 3)
    result = api._resolve_user_path(uuid=None, remna_id=99)
    assert result == '99'


def test_resolve_user_path_v3_no_id_raises():
    api = _api()
    _force_version(api, 3)
    with pytest.raises(ValueError, match='remna_id'):
        api._resolve_user_path(uuid=None, remna_id=None)


# ---------------------------------------------------------------------------
# T3-11: _parse_user парсит v3 (нет uuid, есть id)
# ---------------------------------------------------------------------------

def test_parse_user_v3_populates_id_and_no_uuid():
    api = _api()
    user = api._parse_user(_V3_USER)
    assert user.id == 42
    assert user.uuid is None
    assert user.short_uuid == 'xyz789'
    assert user.subscription_url == 'https://panel.local/sub/xyz789'


def test_parse_user_v2_populates_uuid_and_no_id():
    api = _api()
    user = api._parse_user(_V2_USER)
    assert user.uuid == 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'
    assert user.id is None
    assert user.short_uuid == 'abc123'


# ---------------------------------------------------------------------------
# T3-12: get_user_stats_usage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('api_version,ident,expected_seg', [
    (2, {'user_uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'}, 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'),
    (3, {'user_uuid': None, 'remna_id': 42}, '42'),
])
async def test_get_user_stats_usage_path(api_version, ident, expected_seg):
    api = _api()
    _force_version(api, api_version)
    # get_user_stats_usage delegates to get_bandwidth_stats_user_legacy
    api._make_request = AsyncMock(return_value={'response': {}})

    await api.get_user_stats_usage(**ident, start_date='2024-01-01', end_date='2024-01-31')

    call_args = api._make_request.call_args
    assert expected_seg in call_args.args[1]
