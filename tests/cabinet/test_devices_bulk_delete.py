"""Выборочное отключение нескольких устройств одним запросом.

Экран устройств даёт отметить несколько галочками. Без этого эндпоинта фронту
пришлось бы слать N запросов подряд: при отказе на середине человек остаётся в
состоянии, которого не выбирал, и не понимает, что отключилось, а что нет.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import devices as devices_routes


class _Api:
    def __init__(self, failing: set[str] | None = None):
        self.removed: list[str] = []
        self.failing = failing or set()

    async def remove_device(self, panel_user_id, hwid):
        if hwid in self.failing:
            return False
        self.removed.append(hwid)
        return True


def _service_with(api):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=api)
    client.__aexit__ = AsyncMock(return_value=False)
    service = MagicMock()
    service.get_api_client = MagicMock(return_value=client)
    return MagicMock(return_value=service)


@pytest.fixture
def patched(monkeypatch):
    def _apply(api, subscription=None):
        monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', _service_with(api))
        sub = subscription or SimpleNamespace(id=1, user_id=1, remnawave_id=5, device_limit=5)
        monkeypatch.setattr(
            devices_routes, 'resolve_subscription', AsyncMock(return_value=sub)
        )
        monkeypatch.setattr(
            devices_routes, '_ensure_panel_user_id', AsyncMock(return_value=5)
        )
    return _apply


@pytest.mark.asyncio
async def test_deletes_every_requested_device(patched):
    api = _Api()
    patched(api)
    result = await devices_routes.delete_devices_batch(
        devices_routes.DeleteDevicesBatchRequest(hwids=['a', 'b', 'c']),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert result['deleted_count'] == 3
    assert result['failed_hwids'] == []
    assert api.removed == ['a', 'b', 'c']


@pytest.mark.asyncio
async def test_partial_failure_is_reported_not_hidden(patched):
    """Отказ по одному устройству не должен молча выглядеть успехом."""
    api = _Api(failing={'b'})
    patched(api)
    result = await devices_routes.delete_devices_batch(
        devices_routes.DeleteDevicesBatchRequest(hwids=['a', 'b', 'c']),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert result['success'] is False
    assert result['deleted_count'] == 2
    assert result['failed_hwids'] == ['b']


@pytest.mark.asyncio
async def test_failure_on_one_device_does_not_stop_the_rest(patched):
    api = _Api(failing={'a'})
    patched(api)
    result = await devices_routes.delete_devices_batch(
        devices_routes.DeleteDevicesBatchRequest(hwids=['a', 'b']),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert api.removed == ['b']
    assert result['deleted_count'] == 1


@pytest.mark.asyncio
async def test_duplicates_are_collapsed(patched):
    api = _Api()
    patched(api)
    result = await devices_routes.delete_devices_batch(
        devices_routes.DeleteDevicesBatchRequest(hwids=['a', 'a', 'b']),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert result['deleted_count'] == 2
    assert api.removed == ['a', 'b']


@pytest.mark.asyncio
async def test_missing_subscription_gives_404(monkeypatch):
    monkeypatch.setattr(devices_routes, 'resolve_subscription', AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await devices_routes.delete_devices_batch(
            devices_routes.DeleteDevicesBatchRequest(hwids=['a']),
            subscription_id=None,
            user=SimpleNamespace(id=1),
            db=AsyncMock(),
        )
    assert exc.value.status_code == 404
