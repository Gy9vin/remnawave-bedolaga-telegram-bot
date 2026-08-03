"""bulk-actions/restart-эндпоинты на панели v3 отдают 202 с пустым телом.

v2: `{'response': {'eventSent': true}}`.
v3: тела нет, `_make_request` возвращает `{}` → чтение
`response['response']['eventSent']` падало с KeyError('response'), хотя
панель запрос уже приняла.

Успешный (не бросивший RemnaWaveAPIError) запрос = HTTP 2xx = eventSent,
если панель явно не прислала eventSent=false в теле.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.external.remnawave_api import RemnaWaveAPI


def _api() -> RemnaWaveAPI:
    return RemnaWaveAPI('http://panel.local', 'key')


def _force_version(api: RemnaWaveAPI, version: int) -> None:
    api._api_version = version


@pytest.mark.asyncio
@pytest.mark.parametrize('response,expected', [
    ({}, True),  # v3: 202 без тела
    ({'response': {'eventSent': True}}, True),  # v2
    ({'response': {'eventSent': False}}, False),  # v2: панель явно отказала
])
async def test_restart_node_handles_both_shapes(response, expected):
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value=response)

    assert await api.restart_node('node-uuid') is expected


@pytest.mark.asyncio
@pytest.mark.parametrize('response,expected', [
    ({}, True),
    ({'response': {'eventSent': True}}, True),
    ({'response': {'eventSent': False}}, False),
])
async def test_restart_all_nodes_handles_both_shapes(response, expected):
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value=response)

    assert await api.restart_all_nodes() is expected


@pytest.mark.asyncio
@pytest.mark.parametrize('method,kwargs', [
    ('add_users_to_internal_squad', {'uuid': 'aaaa-1111'}),
    ('remove_users_from_internal_squad', {'uuid': 'aaaa-1111'}),
    ('add_users_to_external_squad', {'uuid': 'bbbb-2222'}),
    ('remove_users_from_external_squad', {'uuid': 'bbbb-2222'}),
])
async def test_squad_bulk_actions_handle_no_content(method, kwargs):
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value={})

    assert await getattr(api, method)(**kwargs) is True


@pytest.mark.asyncio
@pytest.mark.parametrize('method,kwargs', [
    ('add_users_to_internal_squad', {'uuid': 'aaaa-1111'}),
    ('remove_users_from_internal_squad', {'uuid': 'aaaa-1111'}),
    ('add_users_to_external_squad', {'uuid': 'bbbb-2222'}),
    ('remove_users_from_external_squad', {'uuid': 'bbbb-2222'}),
])
async def test_squad_bulk_actions_respect_explicit_event_sent_false(method, kwargs):
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value={'response': {'eventSent': False}})

    assert await getattr(api, method)(**kwargs) is False
