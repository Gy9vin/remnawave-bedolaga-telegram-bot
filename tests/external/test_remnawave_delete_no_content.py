"""DELETE-эндпоинты на панели v3 отдают 204 No Content (пустое тело).

v2: `{'response': {'isDeleted': true}}`.
v3 (remnawave/backend, `@Endpoint({..., httpCode: HttpStatus.NO_CONTENT})` в
users/internal-squad/external-squads/subpage-configs контроллерах): тела нет,
`_make_request` возвращает `{}` → чтение `response['response']['isDeleted']`
падало с KeyError('response'), хотя объект в панели уже удалён.

Успешный (не бросивший RemnaWaveAPIError) DELETE = HTTP 2xx = удаление прошло.
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
    ({}, True),  # v3: 204 No Content
    ({'response': {'isDeleted': True}}, True),  # v2
    ({'response': {'isDeleted': False}}, False),  # v2: панель отказала
])
async def test_delete_user_handles_both_shapes(response, expected):
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value=response)

    assert await api.delete_user(remna_id=42) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize('method,kwargs', [
    ('delete_internal_squad', {'uuid': 'aaaa-1111'}),
    ('delete_external_squad', {'uuid': 'bbbb-2222'}),
    ('delete_subscription_page_config', {'uuid': 'cccc-3333'}),
])
async def test_delete_endpoints_handle_no_content(method, kwargs):
    api = _api()
    _force_version(api, 3)
    api._make_request = AsyncMock(return_value={})

    assert await getattr(api, method)(**kwargs) is True
