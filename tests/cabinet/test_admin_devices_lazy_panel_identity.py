"""Админская карточка не должна слепнуть на незаполненном panel id.

Прод-баг: в админке кабинета список подключённых устройств пуст, хотя в панели
устройства на месте. Причина — переход на RemnaWave 3.x: панельного
пользователя теперь адресует числовой `remnawave_id`, миграция колонку только
заводит, а заполняет её отдельный бэкфилл. Пока он не прогнан (или запись
пропущена из-за конфликта), колонка пуста — и эндпоинт молча возвращал пустой
список, не сходив в панель.

Пользовательская часть кабинета это уже умеет (ленивый резолв по short_uuid /
telegram_id), админская — нет. Тесты фиксируют, что умеет и она.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.cabinet.routes import admin_users
from app.config import Settings


USER_ID = 10
TELEGRAM_ID = 777001
PANEL_ID_FROM_PANEL = 15758


def _user(**overrides) -> SimpleNamespace:
    base = {
        'id': USER_ID,
        'telegram_id': TELEGRAM_ID,
        'email': None,
        'remnawave_id': None,  # бэкфилл ещё не прогнан
        'subscriptions': [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _Api:
    """Панель знает про юзера, даже когда бот его id ещё не сохранил."""

    def __init__(self, calls: dict):
        self.calls = calls

    async def resolve_user(self, short_uuid=None):
        self.calls['resolve_user'].append(short_uuid)
        return None  # short_uuid у записи нет — падаем на telegram_id

    async def find_users_by_telegram_id(self, telegram_id):
        self.calls['find_by_telegram'].append(telegram_id)
        return [SimpleNamespace(id=PANEL_ID_FROM_PANEL)]

    async def get_user_devices_all(self, panel_user_id):
        self.calls['devices_for'].append(panel_user_id)
        return {
            'devices': [
                {'hwid': 'HW-1', 'platform': 'ios', 'deviceModel': 'iPhone 15'},
                {'hwid': 'HW-2', 'platform': 'android', 'deviceModel': 'Pixel 8'},
            ],
            'total': 2,
        }


@pytest.fixture
def panel(monkeypatch):
    calls = {'resolve_user': [], 'find_by_telegram': [], 'devices_for': []}

    class Service:
        is_configured = True

        def get_api_client(self):
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=_Api(calls))
            context.__aexit__ = AsyncMock(return_value=None)
            return context

    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', Service)
    monkeypatch.setattr(admin_users, 'get_aliases_for_user', AsyncMock(return_value={}))
    return calls


@pytest.fixture
def single_tariff(monkeypatch):
    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: False)


@pytest.mark.asyncio
async def test_devices_are_fetched_after_lazy_resolve(monkeypatch, panel, single_tariff):
    user = _user()
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    db = AsyncMock()

    response = await admin_users.get_user_devices(
        user_id=USER_ID, admin=SimpleNamespace(id=1), db=db, subscription_id=None
    )

    assert panel['find_by_telegram'] == [TELEGRAM_ID]
    assert panel['devices_for'] == [PANEL_ID_FROM_PANEL]
    assert [d.hwid for d in response.devices] == ['HW-1', 'HW-2']
    assert response.total == 2


@pytest.mark.asyncio
async def test_resolved_id_is_persisted_so_the_panel_is_asked_once(monkeypatch, panel, single_tariff):
    user = _user()
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    db = AsyncMock()

    await admin_users.get_user_devices(
        user_id=USER_ID, admin=SimpleNamespace(id=1), db=db, subscription_id=None
    )

    assert user.remnawave_id == PANEL_ID_FROM_PANEL
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_ambiguous_telegram_match_is_refused(monkeypatch, panel, single_tariff):
    """Два панельных аккаунта на один telegram id — угадывать нельзя."""
    user = _user()
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))

    class AmbiguousApi(_Api):
        async def find_users_by_telegram_id(self, telegram_id):
            self.calls['find_by_telegram'].append(telegram_id)
            return [SimpleNamespace(id=101), SimpleNamespace(id=202)]

    class Service:
        is_configured = True

        def get_api_client(self):
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=AmbiguousApi(panel))
            context.__aexit__ = AsyncMock(return_value=None)
            return context

    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', Service)

    response = await admin_users.get_user_devices(
        user_id=USER_ID, admin=SimpleNamespace(id=1), db=AsyncMock(), subscription_id=None
    )

    assert response.devices == []
    assert panel['devices_for'] == []
    assert user.remnawave_id is None


@pytest.mark.asyncio
async def test_selected_subscription_without_id_falls_back_to_user_in_single_tariff(
    monkeypatch, panel, single_tariff
):
    """Админка всегда шлёт subscription_id, а в однотарифном режиме колонка у
    подписки обычно пуста — идентичность лежит на юзере. Раньше это и давало
    пустой список при живых устройствах в панели."""
    subscription = SimpleNamespace(
        id=555, user_id=USER_ID, remnawave_id=None, remnawave_short_uuid=None, device_limit=3, is_active=True
    )
    user = _user(remnawave_id=4242, subscriptions=[subscription])
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(
        admin_users, '_get_owned_subscription_or_404', AsyncMock(return_value=subscription)
    )

    response = await admin_users.get_user_devices(
        user_id=USER_ID, admin=SimpleNamespace(id=1), db=AsyncMock(), subscription_id=555
    )

    assert panel['devices_for'] == [4242]
    assert [d.hwid for d in response.devices] == ['HW-1', 'HW-2']
    assert response.device_limit == 3


@pytest.mark.asyncio
async def test_existing_panel_id_skips_the_lookup(monkeypatch, panel, single_tariff):
    """Заполненный id — никаких лишних походов в панель за идентичностью."""
    user = _user(remnawave_id=4242)
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))

    await admin_users.get_user_devices(
        user_id=USER_ID, admin=SimpleNamespace(id=1), db=AsyncMock(), subscription_id=None
    )

    assert panel['find_by_telegram'] == []
    assert panel['devices_for'] == [4242]
