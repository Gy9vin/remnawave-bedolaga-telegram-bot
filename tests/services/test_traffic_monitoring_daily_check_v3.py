"""Суточная проверка трафика (run_daily_check) на панели v3.

v3 не отдаёт `uuid` у пользователя (только числовой `id`) — RemnaWaveUser.uuid
остаётся None (см. _parse_user: `uuid=user_data.get('uuid')`). Старый код:

- отсекал таких пользователей двумя guard'ами `if not user.uuid: ...`
  (внутри check_user_daily_traffic и в списковом включении tasks), из-за чего
  суточная проверка на v3 не делала НИ ОДНОГО запроса;
- даже если бы дошёл до запроса, вызывал
  `api.get_bandwidth_stats_user(user.uuid, ...)` — путь-параметр v3 это
  числовой userId, а не uuid, так что запрос ушёл бы с None/некорректным ID.

Фикс резолвит path_id так же, как app/cabinet/routes/admin_users.py:1091 —
через `api._resolve_user_path(uuid=..., remna_id=...)`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.external.remnawave_api import (
    RemnaWaveAPI,
    RemnaWaveUser,
    TrafficLimitStrategy,
    UserStatus,
)
from app.services.traffic_monitoring_service import TrafficMonitoringServiceV2


def _v3_user(remna_id: int) -> RemnaWaveUser:
    """Пользователь как его отдаёт панель v3: uuid=None, id=<число>."""
    now = datetime.now(UTC)
    return RemnaWaveUser(
        short_uuid='short-uuid',
        username='v3user',
        status=UserStatus.ACTIVE,
        traffic_limit_bytes=0,
        traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
        expire_at=now,
        telegram_id=123,
        email=None,
        hwid_device_limit=None,
        description=None,
        tag=None,
        subscription_url='https://panel.local/sub/short-uuid',
        active_internal_squads=[],
        created_at=now,
        updated_at=now,
        uuid=None,
        id=remna_id,
    )


@pytest.mark.asyncio
async def test_run_daily_check_resolves_numeric_path_id_for_v3_user():
    service = TrafficMonitoringServiceV2()

    service.is_daily_check_enabled = lambda: True
    service._load_nodes_cache = AsyncMock()
    service.get_daily_threshold_gb = lambda: 999999  # порог заведомо не превышается
    service.get_concurrency = lambda: 2

    user = _v3_user(remna_id=777)
    service.get_all_users_with_traffic = AsyncMock(return_value=[user])

    fake_api = AsyncMock()
    fake_api.get_api_version = AsyncMock(return_value=3)
    # Реальная логика резолва path_id, но с закэшированной v3-версией —
    # не мокаем эту часть, чтобы тест ловил регресс и в самой resolve-логике.
    real_api_for_resolve = RemnaWaveAPI('http://panel.local', 'key')
    real_api_for_resolve._api_version = 3
    fake_api._resolve_user_path = real_api_for_resolve._resolve_user_path
    fake_api.get_bandwidth_stats_user = AsyncMock(return_value={'total': 0})

    @asynccontextmanager
    async def _fake_get_api_client():
        yield fake_api

    service.remnawave_service.get_api_client = _fake_get_api_client

    violations = await service.run_daily_check(bot=AsyncMock())

    assert violations == []
    fake_api.get_bandwidth_stats_user.assert_awaited_once()
    called_path_id = fake_api.get_bandwidth_stats_user.await_args.args[0]
    assert called_path_id == '777'
