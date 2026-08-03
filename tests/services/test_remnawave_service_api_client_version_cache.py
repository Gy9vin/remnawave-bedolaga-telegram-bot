"""RemnaWaveService.get_api_client() создаёт НОВЫЙ экземпляр RemnaWaveAPI на
каждый вызов (комментарий в RemnaWaveService.__init__: "чтобы параллельные
корутины не перезаписывали друг другу aiohttp-сессию"). Побочный эффект: при
REMNAWAVE_API_VERSION=auto собственный per-instance кэш RemnaWaveAPI
(``_api_version``) обнуляется на каждый вызов, и КАЖДЫЙ последующий panel-вызов
из ~90 мест кодовой базы заново гоняет полный сетевой probe версии панели
(_detect_api_version: 1-2 HTTP-запроса), прежде чем сделать полезный запрос.

Прод-инцидент (2026-08-03): многие из этих ~90 мест вызываются, пока где-то
выше по стеку уже открыта DB-сессия (async with AsyncSessionLocal()) — лишние
round-trip'ы на каждый вызов панели напрямую удлиняют время удержания
соединения из пула, что и привело к исчерпанию QueuePool (20 + 20 overflow)
и таймауту тривиального COUNT-запроса в startup_notification_service.

Этот тест фиксирует контракт: повторные get_api_client() с одним и тем же
base_url должны переиспользовать УЖЕ определённую версию панели без повторного
сетевого probe.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from app.services import remnawave_service as remnawave_service_module
from app.services.remnawave_service import RemnaWaveService


def _make_service() -> RemnaWaveService:
    service = RemnaWaveService.__new__(RemnaWaveService)
    service._config_error = None
    service._api_kwargs = {
        'base_url': 'http://panel.version-cache-test.local',
        'api_key': 'key',
        'secret_key': None,
        'username': None,
        'password': None,
        'caddy_token': None,
        'auth_type': 'api_key',
    }
    return service


async def test_get_api_client_reuses_detected_version_across_instances():
    """Второй вызов get_api_client() не должен зондировать версию заново."""
    remnawave_service_module._PANEL_API_VERSION_CACHE.clear()
    service = _make_service()

    probe_payload = {'response': {'users': [{'id': 1}], 'hasMore': False, 'nextCursor': None}}

    with patch.object(type(settings), 'get_remnawave_api_version', return_value='auto'):
        async with service.get_api_client() as api:
            api._make_request = AsyncMock(return_value=probe_payload)
            version = await api.get_api_version()
            assert version == 3
            assert api._make_request.call_count == 1

        # Второй клиент — новый экземпляр RemnaWaveAPI (это по дизайну), но
        # версия должна быть уже известна из кэша -> ноль сетевых вызовов.
        async with service.get_api_client() as api2:
            api2._make_request = AsyncMock(return_value=probe_payload)
            version2 = await api2.get_api_version()
            assert version2 == 3
            api2._make_request.assert_not_called()

    remnawave_service_module._PANEL_API_VERSION_CACHE.clear()


async def test_get_api_client_does_not_cache_when_version_forced():
    """При forced-режиме (REMNAWAVE_API_VERSION=2|3) кэш не используется —
    там и так нет сетевых вызовов, поэтому кэшировать нечего и не нужно
    рисковать рассинхроном при смене форсированной версии в рантайме."""
    remnawave_service_module._PANEL_API_VERSION_CACHE.clear()
    service = _make_service()

    with patch.object(type(settings), 'get_remnawave_api_version', return_value='2'):
        async with service.get_api_client() as api:
            api._make_request = AsyncMock()
            version = await api.get_api_version()
            assert version == 2
            api._make_request.assert_not_called()

    assert remnawave_service_module._PANEL_API_VERSION_CACHE == {}
