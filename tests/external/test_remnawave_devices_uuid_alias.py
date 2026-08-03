"""HWID-методы принимают идентификатор и как `uuid`, и как `user_uuid`.

Прод: `RemnaWaveAPI.reset_user_devices() got an unexpected keyword argument 'uuid'`
— восемь call-site'ов (смена и покупка тарифа, продление, админский сброс) зовут
метод с `uuid=`, как остальные user-методы клиента, а сигнатура принимала только
`user_uuid`. Сброс устройств падал во всех этих сценариях.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.external.remnawave_api import RemnaWaveAPI


def _api(version: int) -> RemnaWaveAPI:
    api = RemnaWaveAPI('http://panel.local', 'key')
    api._api_version = version
    return api


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['get_user_devices', 'get_user_devices_all'])
@pytest.mark.parametrize('kwargs', [
    {'uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'},
    {'user_uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'},
])
async def test_device_getters_accept_both_names(method, kwargs):
    api = _api(2)
    api._make_request = AsyncMock(return_value={'response': {'total': 0, 'devices': []}})

    await getattr(api, method)(**kwargs)

    assert 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb' in api._make_request.call_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs', [
    {'uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'},
    {'user_uuid': 'aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb'},
])
async def test_reset_user_devices_accepts_both_names(kwargs):
    """Именно этот вызов падал TypeError в проде."""
    api = _api(2)
    api._make_request = AsyncMock(return_value={'response': {'total': 0, 'devices': []}})

    assert await api.reset_user_devices(**kwargs) is True


@pytest.mark.asyncio
async def test_reset_user_devices_v3_uses_remna_id_without_uuid():
    api = _api(3)
    api._make_request = AsyncMock(return_value={'response': {'total': 0, 'devices': []}})

    assert await api.reset_user_devices(remna_id=42) is True
    assert '42' in api._make_request.call_args.args[1]
