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

    def _build(
        devices,
        device_limit=3,
        min_limit=1,
        device_price=6000,
        days_left=30,
        remnawave_update_succeeds=True,
    ):
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
            # Параметризуемо: False имитирует отказ панели, чтобы проверить, что
            # откат в этом случае отменяет и начисление возврата, а не только
            # смену лимита.
            async def update_remnawave_user(self, db, subscription):
                return True if remnawave_update_succeeds else None

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
    """Возврат обязан оставить след в истории операций, иначе деньги из ниоткуда.

    Сумма проверяется ТОЧНО, а не просто «больше нуля»: с device_limit=3 (по
    умолчанию фикстуры) и new_device_limit=2 освобождается 1 место
    (freed_slots), а не 1 устройство (devices_removed_count — в этом тесте они
    случайно совпадают: подтверждено отдельным тестом
    test_refund_matches_freed_slots_not_removed_devices, где они различаются).
    Ожидание считаем руками из параметров фикстуры (device_price=6000,
    days_left=30, freed_slots=1), а не вызовом calculate_device_refund_kopeks —
    иначе тест проверял бы только сам себя.
    """
    api, subscription, added = reduce_env(devices=[{'hwid': 'a'}, {'hwid': 'b'}, {'hwid': 'c'}])
    result = await devices_routes.reduce_devices(
        devices_routes.ReduceDevicesRequest(new_device_limit=2, hwids_to_remove=['a']),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    expected_refund = 6000 * 1 * 30 // 30  # device_price * freed_slots * days_left // 30 = 6000
    assert result['refund_kopeks'] == expected_refund
    assert len(added) == 1
    assert added[0]['create_transaction'] is True
    assert added[0]['amount_kopeks'] == expected_refund


@pytest.mark.asyncio
async def test_refund_matches_freed_slots_not_removed_devices(reduce_env):
    """Единственная конфигурация, доказывающая, что в формулу идёт именно
    число ОСВОБОЖДЁННЫХ МЕСТ, а не число ОТКЛЮЧЁННЫХ УСТРОЙСТВ.

    Лимит 5, подключено 2 устройства, новый лимит 3: освобождается 2 места
    (5 - 3), а отключать не нужно ни одного (2 подключённых меньше нового
    лимита 3). Если бы обработчик по ошибке считал возврат по числу отключённых
    устройств (0), возврата бы не было вовсе; если бы считал по числу
    подключённых устройств (2) без учёта лимита — совпало бы случайно. Здесь
    freed_slots=2, devices_to_remove_count=0 — величины различаются, и только
    формула «по освобождённым местам» даёт корректный ненулевой результат.
    """
    api, subscription, added = reduce_env(
        devices=[{'hwid': 'a'}, {'hwid': 'b'}], device_limit=5
    )
    result = await devices_routes.reduce_devices(
        devices_routes.ReduceDevicesRequest(new_device_limit=3, hwids_to_remove=None),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    expected_refund = 6000 * 2 * 30 // 30  # device_price * freed_slots(2) * days_left // 30 = 12000
    assert api.removed == []
    assert result['refund_kopeks'] == expected_refund
    assert added[0]['amount_kopeks'] == expected_refund


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


def _fake_devices_discount_20pct(user, category, amount, period_days=None):
    """Имитация скидки промогруппы на устройства (20%) — та же форма ответа,
    что и настоящая `_apply_addon_discount` из helpers.py."""
    assert category == 'devices'
    if amount <= 0:
        return {'discounted': amount, 'discount': 0, 'percent': 0}
    discount = amount * 20 // 100
    return {'discounted': amount - discount, 'discount': discount, 'percent': 20}


@pytest.mark.asyncio
async def test_refund_applies_addon_discount_like_purchase(monkeypatch, reduce_env):
    """Возврат обязан учитывать ту же скидку промогруппы на устройства, что и
    покупка (см. purchase_devices, строка ~208 в devices.py) — иначе человек со
    скидкой платит за место дешевле, а получает возврат по полной цене.

    Без скидки (test_refund_is_credited_with_transaction, те же параметры
    фикстуры) возврат равен 6000. Ожидание здесь считаем руками: 20% от 6000 —
    это 1200, значит с учётом скидки возврат обязан быть 6000 - 1200 = 4800.
    Именно на эту величину — размер скидки — он должен быть МЕНЬШЕ, чем без
    скидки, а не на произвольную сумму.
    """
    api, subscription, added = reduce_env(devices=[{'hwid': 'a'}, {'hwid': 'b'}, {'hwid': 'c'}])
    monkeypatch.setattr(devices_routes, '_apply_addon_discount', _fake_devices_discount_20pct)

    result = await devices_routes.reduce_devices(
        devices_routes.ReduceDevicesRequest(new_device_limit=2, hwids_to_remove=['a']),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )

    refund_without_discount = 6000  # см. test_refund_is_credited_with_transaction
    expected_refund = 4800  # 6000 - 20%
    assert result['refund_kopeks'] == expected_refund
    assert refund_without_discount - result['refund_kopeks'] == 1200  # ровно 20% от 6000
    assert added[0]['amount_kopeks'] == expected_refund


@pytest.mark.asyncio
async def test_refund_kopeks_per_slot_matches_actual_refund(monkeypatch, reduce_env):
    """Сумма, которую reduction-info показывает человеку за одно место
    (`refund_kopeks_per_slot`), обязана совпадать с реально начисляемым
    возвратом при уменьшении лимита ровно на 1 место — иначе цифра на экране
    до нажатия кнопки разойдётся с тем, что придёт на баланс.
    """
    api, subscription, added = reduce_env(
        devices=[{'hwid': 'a'}], device_limit=3, min_limit=1, device_price=6000, days_left=30
    )
    monkeypatch.setattr(devices_routes, '_apply_addon_discount', _fake_devices_discount_20pct)

    info = await devices_routes.get_device_reduction_info(
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )

    result = await devices_routes.reduce_devices(
        devices_routes.ReduceDevicesRequest(new_device_limit=2, hwids_to_remove=None),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )

    # device_limit=3 -> new_device_limit=2: freed_slots=1, то есть ровно то же
    # количество мест, что и в reduction-info (slots=1).
    assert result['refund_kopeks'] == info['refund_kopeks_per_slot']


@pytest.mark.asyncio
async def test_panel_failure_rolls_back_refund_with_limit(reduce_env):
    """Отказ панели должен откатить И лимит, И начисление — одной транзакцией.

    commit=False на начислении и общий db.rollback() при отказе панели — то
    единственное, что отделяет человека от ситуации «оставил себе и деньги, и
    места». Начисление к этому моменту уже вызвано (оно идёт до похода в
    панель), но важно доказать сам факт отката: именно он отменяет начисление
    в общей транзакции. Проверяем и что был вызван db.rollback(), и что
    обработчик вернул 502, а не «успех» с деньгами на руках.
    """
    api, subscription, added = reduce_env(
        devices=[{'hwid': 'a'}, {'hwid': 'b'}, {'hwid': 'c'}],
        remnawave_update_succeeds=False,
    )
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await devices_routes.reduce_devices(
            devices_routes.ReduceDevicesRequest(new_device_limit=2, hwids_to_remove=['a']),
            subscription_id=None,
            user=SimpleNamespace(id=1),
            db=db,
        )
    assert exc.value.status_code == 502
    db.rollback.assert_awaited_once()
