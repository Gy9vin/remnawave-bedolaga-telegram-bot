"""Уменьшение лимита: выбор отключаемых устройств и возврат денег.

Два изменения в одной транзакции. Раньше обработчик сам выбирал жертв, сортируя
по активности и удаляя САМЫЕ СВЕЖИЕ, и не возвращал денег вовсе.

Ключевой инвариант: количество отключаемых устройств и количество освобождаемых
мест — разные величины. Мест освобождается «старый лимит минус новый».
Устройств отключать надо «сколько подключено минус новый лимит», и это число
бывает меньше, а бывает нулём.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, NonCallableMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import devices as devices_routes


class _Api:
    def __init__(self, devices):
        self.devices = devices
        self.removed: list[str] = []

    async def get_user_devices_all(self, panel_user_id):
        return {'devices': self.devices, 'total': len(self.devices)}

    async def remove_device(self, panel_user_id, hwid):
        self.removed.append(hwid)
        return True


def _patch_common(monkeypatch, api, subscription, added_balance):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=api)
    client.__aexit__ = AsyncMock(return_value=False)
    service = MagicMock()
    service.get_api_client = MagicMock(return_value=client)
    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', MagicMock(return_value=service))

    monkeypatch.setattr(devices_routes, 'resolve_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(devices_routes, '_ensure_panel_user_id', AsyncMock(return_value=5))

    # `db=AsyncMock()` без spec делает АБСОЛЮТНО ВСЕ дочерние атрибуты, включая
    # синхронный `Result.scalar_one_or_none`, тоже AsyncMock — вызов вернёт
    # корутину вместо значения, и production-код (который её не ждёт) упадёт.
    # Подменяем создание именно дочернего мока `execute`, чтобы он возвращал
    # результат с синхронным `scalar_one_or_none`, отдающим нашу подписку —
    # так же, как реальный `AsyncSession.execute(...).scalar_one_or_none()`.
    _original_get_child_mock = NonCallableMock._get_child_mock

    def _patched_get_child_mock(self, /, **kw):
        if kw.get('_new_name') == 'execute':
            return AsyncMock(
                return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=subscription))
            )
        return _original_get_child_mock(self, **kw)

    monkeypatch.setattr(NonCallableMock, '_get_child_mock', _patched_get_child_mock)

    async def _fake_add_balance(**kwargs):
        added_balance.append(kwargs)
        return True

    monkeypatch.setattr(devices_routes, 'add_user_balance', _fake_add_balance)


@pytest.fixture
def reduce_env(monkeypatch):
    added: list[dict] = []

    def _build(devices, device_limit=3, min_limit=1, device_price=6000, days_left=30):
        from datetime import UTC, datetime, timedelta

        api = _Api(devices)
        subscription = SimpleNamespace(
            id=1,
            user_id=1,
            remnawave_id=5,
            device_limit=device_limit,
            is_trial=False,
            tariff_id=None,
            end_date=datetime.now(UTC) + timedelta(days=days_left),
            updated_at=None,
        )
        _patch_common(monkeypatch, api, subscription, added)
        monkeypatch.setattr(devices_routes, 'resolve_min_device_limit', lambda tariff: min_limit)
        monkeypatch.setattr(
            devices_routes, '_resolve_device_price_kopeks', AsyncMock(return_value=device_price)
        )

        class _SubService:
            async def update_remnawave_user(self, db, subscription):
                return True

        monkeypatch.setattr(devices_routes, 'SubscriptionService', lambda: _SubService())
        return api, subscription, added

    return _build


@pytest.mark.asyncio
async def test_removes_exactly_chosen_devices(monkeypatch, reduce_env):
    """Отключается то, что выбрал человек, а не то, что выбрал алгоритм."""
    api, subscription, added = reduce_env(devices=[{'hwid': 'a'}, {'hwid': 'b'}, {'hwid': 'c'}])
    await devices_routes.reduce_devices(
        devices_routes.ReduceDevicesRequest(new_device_limit=2, hwids_to_remove=['a']),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert api.removed == ['a']


@pytest.mark.asyncio
async def test_refund_is_credited_with_transaction(reduce_env):
    """Возврат обязан оставить след в истории операций, иначе деньги из ниоткуда."""
    api, subscription, added = reduce_env(devices=[{'hwid': 'a'}, {'hwid': 'b'}, {'hwid': 'c'}])
    result = await devices_routes.reduce_devices(
        devices_routes.ReduceDevicesRequest(new_device_limit=2, hwids_to_remove=['a']),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert result['refund_kopeks'] > 0
    assert len(added) == 1
    assert added[0]['create_transaction'] is True


@pytest.mark.asyncio
async def test_no_devices_to_remove_when_under_limit(reduce_env):
    """Подключено меньше нового лимита — отключать нечего, но места освобождаются."""
    api, subscription, added = reduce_env(devices=[{'hwid': 'a'}])
    result = await devices_routes.reduce_devices(
        devices_routes.ReduceDevicesRequest(new_device_limit=2, hwids_to_remove=None),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert api.removed == []
    assert result['refund_kopeks'] > 0


@pytest.mark.asyncio
async def test_wrong_number_of_hwids_is_rejected(reduce_env):
    """Прислали не тех и не столько — отказываем, а не досоображаем за человека."""
    api, subscription, added = reduce_env(devices=[{'hwid': 'a'}, {'hwid': 'b'}, {'hwid': 'c'}])
    with pytest.raises(HTTPException) as exc:
        await devices_routes.reduce_devices(
            devices_routes.ReduceDevicesRequest(new_device_limit=1, hwids_to_remove=['a']),
            subscription_id=None,
            user=SimpleNamespace(id=1),
            db=AsyncMock(),
        )
    assert exc.value.status_code == 400
    assert added == []


@pytest.mark.asyncio
async def test_unknown_hwid_is_rejected(reduce_env):
    api, subscription, added = reduce_env(devices=[{'hwid': 'a'}, {'hwid': 'b'}, {'hwid': 'c'}])
    with pytest.raises(HTTPException) as exc:
        await devices_routes.reduce_devices(
            devices_routes.ReduceDevicesRequest(new_device_limit=2, hwids_to_remove=['zzz']),
            subscription_id=None,
            user=SimpleNamespace(id=1),
            db=AsyncMock(),
        )
    assert exc.value.status_code == 400
    assert api.removed == []


@pytest.mark.asyncio
async def test_no_refund_when_limit_did_not_change(reduce_env):
    """Повтор запроса не должен вернуть деньги дважды."""
    api, subscription, added = reduce_env(devices=[{'hwid': 'a'}], device_limit=2)
    with pytest.raises(HTTPException):
        await devices_routes.reduce_devices(
            devices_routes.ReduceDevicesRequest(new_device_limit=2, hwids_to_remove=None),
            subscription_id=None,
            user=SimpleNamespace(id=1),
            db=AsyncMock(),
        )
    assert added == []
