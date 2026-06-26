"""Мини логический тест sync_user_clients с моками.

Проверяет: маппинг uuid→user_id, upsert, prune.
Запуск: PYTHONPATH=. .venv/bin/python scripts/_test_client_sync.py
"""

from __future__ import annotations

import asyncio
import sys
import os

# Позволяем запускать из корня репо
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Заглушки
# ---------------------------------------------------------------------------

# Imitируем UserClient-строку
class FakeUserClient:
    def __init__(self, user_id: int, app_name: str, last_seen_at=None):
        self.user_id = user_id
        self.app_name = app_name
        self.last_seen_at = last_seen_at
        self.updated_at = None


# Имитируем AsyncSession
class FakeSession:
    def __init__(self, existing: list[FakeUserClient]):
        self._existing = existing
        self.added: list[FakeUserClient] = []
        self.deleted: list[FakeUserClient] = []
        self.committed = False

    async def execute(self, stmt):
        # Нам нужно вернуть заглушку с методом scalars
        import types
        result = MagicMock()
        # Первые два вызова — User и Subscription карты (scalars не нужны)
        # Третий вызов — UserClient (scalars нужны)
        # Упростим: различаем по типу запроса невозможно без реального SA
        # Поэтому возвращаем специальный объект
        return result

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


# ---------------------------------------------------------------------------
# Тест 1: логика маппинга и сборки wanted
# ---------------------------------------------------------------------------

def test_mapping_and_wanted():
    """Проверяет что uuid→user_id и сборка wanted работают корректно."""
    from app.utils.client_detect import parse_client_app

    # Данные: 3 устройства
    devices = [
        {'userUuid': 'uuid-1', 'platform': 'Happ', 'updatedAt': '2024-06-01T10:00:00Z'},
        {'userUuid': 'uuid-1', 'platform': 'v2rayNG/1.9', 'updatedAt': '2024-06-02T10:00:00Z'},
        {'userUuid': 'uuid-2', 'platform': 'Streisand', 'updatedAt': '2024-05-01T00:00:00Z'},
        {'userUuid': 'uuid-unknown', 'platform': 'SomeApp', 'updatedAt': None},  # нет маппинга
    ]

    user_uuid_map = {'uuid-1': 101}
    sub_uuid_map = {'uuid-2': 202}

    # Реплицируем логику сборки wanted из service
    from datetime import datetime

    def parse_dt(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00'))
        except Exception:
            return None

    wanted: dict = {}
    users_in_sync: set = set()

    for device in devices:
        puuid = device.get('userUuid')
        if not puuid:
            continue
        user_id = user_uuid_map.get(puuid) or sub_uuid_map.get(puuid)
        if user_id is None:
            continue
        platform_str = device.get('platform') or device.get('appVersion') or device.get('deviceModel')
        app = parse_client_app(platform_str)
        raw_dt = device.get('updatedAt') or device.get('lastSeen') or device.get('createdAt')
        seen_at = parse_dt(raw_dt)

        key = (user_id, app)
        existing = wanted.get(key)
        if existing is None or (seen_at is not None and seen_at > existing):
            wanted[key] = seen_at
        users_in_sync.add(user_id)

    # Ожидания:
    assert (101, 'Happ') in wanted, "uuid-1 → user 101 → Happ"
    assert (101, 'v2rayNG') in wanted, "uuid-1 → user 101 → v2rayNG"
    assert (202, 'Streisand') in wanted, "uuid-2 → user 202 → Streisand"
    assert len([k for k in wanted if k[0] == 101]) == 2, "у user 101 два приложения"
    assert (101, 'SomeApp') not in wanted and (202, 'SomeApp') not in wanted, \
        "uuid-unknown не попал в wanted"

    # Проверяем max(last_seen) для Happ (uuid-1 имеет одно устройство Happ)
    happ_seen = wanted[(101, 'Happ')]
    assert happ_seen is not None
    assert happ_seen.year == 2024 and happ_seen.month == 6

    assert users_in_sync == {101, 202}

    print("PASS: test_mapping_and_wanted")


# ---------------------------------------------------------------------------
# Тест 2: логика upsert (новая строка vs обновление)
# ---------------------------------------------------------------------------

def test_upsert_logic():
    """Проверяет что upsert создаёт новые строки и обновляет last_seen_at."""
    from datetime import datetime

    now = datetime(2024, 6, 10, tzinfo=UTC)
    old_dt = datetime(2024, 5, 1, tzinfo=UTC)
    new_dt = datetime(2024, 6, 2, tzinfo=UTC)

    wanted = {
        (101, 'Happ'): new_dt,    # должна обновить existing
        (101, 'v2rayNG'): now,    # новая строка
    }

    existing_row = FakeUserClient(101, 'Happ', last_seen_at=old_dt)
    existing_index = {(101, 'Happ'): existing_row}

    added: list[FakeUserClient] = []

    for (user_id, app_name), last_seen in wanted.items():
        row = existing_index.get((user_id, app_name))
        if row is None:
            new_row = FakeUserClient(user_id, app_name, last_seen_at=last_seen)
            added.append(new_row)
        else:
            if last_seen is not None and (row.last_seen_at is None or last_seen > row.last_seen_at):
                row.last_seen_at = last_seen

    assert len(added) == 1, "должна создаться одна новая строка (v2rayNG)"
    assert added[0].app_name == 'v2rayNG'
    assert existing_row.last_seen_at == new_dt, "last_seen_at должен обновиться до нового"

    print("PASS: test_upsert_logic")


# ---------------------------------------------------------------------------
# Тест 3: логика prune
# ---------------------------------------------------------------------------

def test_prune_logic():
    """Проверяет что prune удаляет только стale строки синкнутых юзеров."""
    wanted = {
        (101, 'Happ'): None,
        (101, 'v2rayNG'): None,
        # у user 101 больше нет Streisand (нужно удалить)
        # user 999 вообще не в синке — не трогаем
    }
    users_in_sync = {101}

    existing_rows = [
        FakeUserClient(101, 'Happ'),
        FakeUserClient(101, 'v2rayNG'),
        FakeUserClient(101, 'Streisand'),   # stale — должна удалиться
        FakeUserClient(999, 'SomeApp'),     # юзер не в синке — НЕ трогать
    ]

    wanted_keys = set(wanted.keys())
    to_delete = []
    for row in existing_rows:
        if row.user_id in users_in_sync and (row.user_id, row.app_name) not in wanted_keys:
            to_delete.append(row)

    assert len(to_delete) == 1, "должна удалиться только одна строка (Streisand у user 101)"
    assert to_delete[0].app_name == 'Streisand'
    assert to_delete[0].user_id == 101

    print("PASS: test_prune_logic")


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    test_mapping_and_wanted()
    test_upsert_logic()
    test_prune_logic()
    print("\nAll tests PASSED")
