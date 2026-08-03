"""add_all_users_to_squad / remove_all_users_from_squad on panel v3.

Both methods call `/api/internal-squads/{uuid}/bulk-actions/...` directly via
`api._make_request` and used to read `response.get('response', {}).get('eventSent', False)`.
On panel v3 that endpoint answers 202 with an EMPTY body, so `_make_request`
returns `{}` and `.get('eventSent', False)` silently resolved to `False` —
the admin panel/bot/webapi always showed "Error" even though the panel had
already accepted the bulk action.

Fix: success = no exception raised (HTTP 2xx); `eventSent` is only honoured
when the panel body actually contains it.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.services.remnawave_service import RemnaWaveService


def _service_with_fake_api(make_request_return):
    service = RemnaWaveService.__new__(RemnaWaveService)

    fake_api = AsyncMock()
    fake_api._make_request = AsyncMock(return_value=make_request_return)
    # _is_event_sent — реальный staticmethod с RemnaWaveAPI, а не мок.
    from app.external.remnawave_api import RemnaWaveAPI

    fake_api._is_event_sent = RemnaWaveAPI._is_event_sent

    @asynccontextmanager
    async def _fake_get_api_client():
        yield fake_api

    service.get_api_client = _fake_get_api_client
    return service, fake_api


@pytest.mark.asyncio
@pytest.mark.parametrize('response,expected', [
    ({}, True),  # v3: 202 без тела
    ({'response': {'eventSent': True}}, True),  # v2
    ({'response': {'eventSent': False}}, False),  # v2: панель явно отказала
])
async def test_add_all_users_to_squad_handles_v3_empty_body(response, expected):
    service, fake_api = _service_with_fake_api(response)

    with patch(
        'app.services.grace_access_runtime.grace_sensitive_global_panel_update',
        return_value=_allowed_grace_cm(),
    ):
        result = await service.add_all_users_to_squad('squad-uuid')

    assert result is expected
    fake_api._make_request.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('response,expected', [
    ({}, True),
    ({'response': {'eventSent': True}}, True),
    ({'response': {'eventSent': False}}, False),
])
async def test_remove_all_users_from_squad_handles_v3_empty_body(response, expected):
    service, fake_api = _service_with_fake_api(response)

    with patch(
        'app.services.grace_access_runtime.grace_sensitive_global_panel_update',
        return_value=_allowed_grace_cm(),
    ):
        result = await service.remove_all_users_from_squad('squad-uuid')

    assert result is expected
    fake_api._make_request.assert_awaited_once()


@asynccontextmanager
async def _allowed_grace_cm():
    yield True
