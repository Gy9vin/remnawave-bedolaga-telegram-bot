"""Безлимит устройств (``device_limit == 0``) должен переживать бизнес-логику,
а не только чтение/сериализацию значения.

Панель RemnaWave трактует ``hwidDeviceLimit == 0`` как «лимит HWID отключён»:
``subscription.service.ts`` на этой ветке регистрирует устройство и возвращает
``limitBypassed: true``. Поэтому ноль — не «лимит не задан» (это ``None``,
трактуется как 1) и не «ноль устройств» — это ЯВНЫЙ безлимит, и любая бизнес-
операция, которая опирается на текущий ``device_limit``, обязана:

- не превращать 0 в ``devices`` при докупке (``add_subscription_devices``);
- отказывать в докупке/уменьшении лимита в кабинете, а не считать цену/минимум
  от нуля так, будто это «ноль устройств»;
- не трогать безлимит при переключении модема в админке (0 ± 1 == 0, а не 1).

Файлы tests/test_device_limit_unlimited.py (резолв в payload панели) и
tests/test_device_limit_resolution.py заняты другой частью покрытия — здесь
только пути, где ноль проходит через мутирующую бизнес-логику.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users
from app.cabinet.routes.subscription_modules import devices as devices_routes
from app.cabinet.schemas.subscription import DevicePurchaseRequest, ReduceDevicesRequest
from app.cabinet.schemas.users import UpdateSubscriptionRequest
from app.config import settings
from app.database.crud.subscription import add_subscription_devices


# ---------------------------------------------------------------------------
# 1. app/database/crud/subscription.py::add_subscription_devices
# ---------------------------------------------------------------------------


def _lock_env(monkeypatch, subscription):
    """Двойник окружения для add_subscription_devices: подменяем блокирующий
    SELECT (``with_for_update``) на возврат готового объекта подписки, как это
    сделано в tests/database/crud/test_subscription.py::_patch_reset_env."""
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = subscription
    db = MagicMock()
    db.execute = AsyncMock(return_value=result_mock)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    monkeypatch.setattr(
        'app.services.recurrent_amount.sync_recurrent_bindings_after_price_change',
        AsyncMock(),
    )
    return db


def _subscription(*, device_limit, tariff=None):
    return SimpleNamespace(
        id=1547,
        user_id=11,
        device_limit=device_limit,
        tariff=tariff,
        updated_at=None,
    )


async def test_add_devices_keeps_unlimited_at_zero(monkeypatch):
    """Докупка устройств поверх безлимита не превращает 0 в число купленных
    устройств — панель уже трактует 0 как «лимит HWID отключён», добавлять
    туда фиксированное N бессмысленно и опасно (случайно СНИМЕТ безлимит)."""
    subscription = _subscription(device_limit=0)
    db = _lock_env(monkeypatch, subscription)

    result = await add_subscription_devices(db, subscription, devices=5)

    assert result.device_limit == 0


async def test_add_devices_none_treated_as_one(monkeypatch):
    """Отсутствующий лимит (``None``) — это НЕ безлимит, а «лимит не задан»,
    трактуется как 1 устройство (прежнее поведение сохранено)."""
    subscription = _subscription(device_limit=None)
    db = _lock_env(monkeypatch, subscription)

    result = await add_subscription_devices(db, subscription, devices=2)

    assert result.device_limit == 3  # 1 (базовый) + 2 купленных


async def test_add_devices_positive_limit_increments_as_before(monkeypatch):
    """Обычный положительный лимит по-прежнему просто увеличивается на N."""
    subscription = _subscription(device_limit=3)
    db = _lock_env(monkeypatch, subscription)

    result = await add_subscription_devices(db, subscription, devices=2)

    assert result.device_limit == 5


async def test_add_devices_capped_by_global_max(monkeypatch):
    """Положительный лимит по-прежнему упирается в потолок MAX_DEVICES_LIMIT —
    безлимитная ветка (0) этот потолок не проверяет вовсе, а обычная всё ещё
    должна."""
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 20)
    subscription = _subscription(device_limit=18)
    db = _lock_env(monkeypatch, subscription)

    result = await add_subscription_devices(db, subscription, devices=5)

    assert result.device_limit == 20


async def test_add_devices_capped_by_tariff_max(monkeypatch):
    """Тарифный потолок max_device_limit тоже применяется только к обычным
    (не нулевым) лимитам."""
    monkeypatch.setattr(settings, 'MAX_DEVICES_LIMIT', 20)
    tariff = SimpleNamespace(max_device_limit=6)
    subscription = _subscription(device_limit=5, tariff=tariff)
    db = _lock_env(monkeypatch, subscription)

    result = await add_subscription_devices(db, subscription, devices=3)

    assert result.device_limit == 6


# ---------------------------------------------------------------------------
# 2. app/cabinet/routes/subscription_modules/devices.py
# ---------------------------------------------------------------------------


def _cabinet_subscription(**overrides):
    base = dict(
        id=42,
        status='active',
        is_trial=False,
        tariff_id=None,
        device_limit=0,
        end_date=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_get_device_price_refuses_when_unlimited(monkeypatch):
    """Докупка устройств поверх безлимита бессмысленна — цену считать не
    из чего (нет «текущего числа», от которого прибавлять), поэтому эндпоинт
    честно отвечает available=False с причиной, а не молча продаёт слоты."""
    subscription = _cabinet_subscription()
    monkeypatch.setattr(devices_routes, 'resolve_subscription', AsyncMock(return_value=subscription))

    response = await devices_routes.get_device_price(
        devices=1,
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )

    assert response['available'] is False
    assert 'безлимит' in response['reason'].lower()


async def test_get_device_reduction_info_refuses_when_unlimited(monkeypatch):
    """Уменьшать лимит устройств от безлимита тоже некуда: min/max-математика
    ниже по функции рассчитана на положительные значения и не имеет смысла
    для 0. Эндпоинт обязан вернуть чёткий отказ, а не 1-based расчёт от нуля."""
    subscription = _cabinet_subscription()
    monkeypatch.setattr(devices_routes, 'resolve_subscription', AsyncMock(return_value=subscription))

    response = await devices_routes.get_device_reduction_info(
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )

    assert response == {
        'available': False,
        'reason': 'Device limit is unlimited',
        'current_device_limit': 0,
        'min_device_limit': 1,
        'can_reduce': 0,
        'connected_devices_count': 0,
    }


def _locked_db(subscription):
    """MagicMock db, где ``with_for_update``-SELECT возвращает готовую
    подписку — как в докупочных эндпоинтах devices.py."""
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = subscription
    db = MagicMock()
    db.execute = AsyncMock(return_value=result_mock)
    return db


async def test_purchase_devices_legacy_rejects_unlimited(monkeypatch):
    """Легаси-эндпоинт докупки (`POST /devices`) отказывает 400-й на
    безлимите ещё до расчёта прорейта цены."""
    resolved = SimpleNamespace(id=42)
    monkeypatch.setattr(devices_routes, 'resolve_subscription', AsyncMock(return_value=resolved))
    subscription = _cabinet_subscription()
    db = _locked_db(subscription)

    with pytest.raises(HTTPException) as exc:
        await devices_routes.purchase_devices_legacy(
            DevicePurchaseRequest(devices=1),
            subscription_id=None,
            user=SimpleNamespace(id=1, restriction_subscription=False),
            db=db,
        )

    assert exc.value.status_code == 400
    assert 'безлимит' in str(exc.value.detail).lower()


async def test_purchase_devices_rejects_unlimited(monkeypatch):
    """Основной эндпоинт докупки (`POST /devices/purchase`) отказывает
    так же, как легаси-версия — ветки не должны разойтись."""
    resolved = SimpleNamespace(id=42)
    monkeypatch.setattr(devices_routes, 'resolve_subscription', AsyncMock(return_value=resolved))
    subscription = _cabinet_subscription()
    db = _locked_db(subscription)

    with pytest.raises(HTTPException) as exc:
        await devices_routes.purchase_devices(
            DevicePurchaseRequest(devices=1),
            subscription_id=None,
            user=SimpleNamespace(id=1, restriction_subscription=False),
            db=db,
        )

    assert exc.value.status_code == 400
    assert 'безлимит' in str(exc.value.detail).lower()


async def test_save_devices_cart_rejects_unlimited(monkeypatch):
    """Сохранение корзины докупки (для автопокупки после пополнения) тоже
    не должно откладывать бессмысленную покупку "на потом" — отказ сразу."""
    subscription = _cabinet_subscription()
    monkeypatch.setattr(devices_routes, 'resolve_subscription', AsyncMock(return_value=subscription))

    with pytest.raises(HTTPException) as exc:
        await devices_routes.save_devices_cart(
            DevicePurchaseRequest(devices=1),
            subscription_id=None,
            user=SimpleNamespace(id=1),
            db=AsyncMock(),
        )

    assert exc.value.status_code == 400
    assert 'безлимит' in str(exc.value.detail).lower()


async def test_reduce_devices_rejects_unlimited(monkeypatch):
    """Уменьшение лимита (`POST /devices/reduce`) отказывает 400-й: у
    безлимита нет «текущего значения», от которого можно вычесть."""
    resolved = SimpleNamespace(id=42)
    monkeypatch.setattr(devices_routes, 'resolve_subscription', AsyncMock(return_value=resolved))
    subscription = _cabinet_subscription()
    db = _locked_db(subscription)

    with pytest.raises(HTTPException) as exc:
        await devices_routes.reduce_devices(
            ReduceDevicesRequest(new_device_limit=1),
            subscription_id=None,
            user=SimpleNamespace(id=1),
            db=db,
        )

    assert exc.value.status_code == 400
    assert 'unlimited' in str(exc.value.detail).lower()


# ---------------------------------------------------------------------------
# 3. app/cabinet/routes/admin_users.py — переключение модема (~строка 1930)
# ---------------------------------------------------------------------------


def _admin_subscription(*, device_limit, modem_enabled=False):
    return SimpleNamespace(
        id=901,
        user_id=10,
        is_active=True,
        status='active',
        modem_enabled=modem_enabled,
        device_limit=device_limit,
        remnawave_id=555,
    )


def _admin_user(subscription):
    return SimpleNamespace(id=10, telegram_id=1000, email=None, remnawave_id=555, subscriptions=[subscription])


async def _run_toggle_modem(monkeypatch, subscription, *, modem_enabled: bool):
    user = _admin_user(subscription)
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', AsyncMock())
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))
    db = AsyncMock()

    request = UpdateSubscriptionRequest(action='toggle_modem', modem_enabled=modem_enabled)
    result = await admin_users.update_user_subscription(user.id, request, admin=SimpleNamespace(id=1), db=db)

    assert result.success is True
    return subscription


async def test_admin_toggle_modem_on_keeps_unlimited(monkeypatch):
    """Включение модема на безлимитной подписке (device_limit=0) не должно
    превращать её в "1" — прибавление +1 к нулю выглядело бы как ``1``, а это
    уже НЕ безлимит для панели."""
    subscription = _admin_subscription(device_limit=0, modem_enabled=False)

    await _run_toggle_modem(monkeypatch, subscription, modem_enabled=True)

    assert subscription.device_limit == 0
    assert subscription.modem_enabled is True


async def test_admin_toggle_modem_off_keeps_unlimited(monkeypatch):
    """Выключение модема тоже не трогает безлимит — вычитание 1 из 0 не
    должно давать -1 и тем более не должно "почему-то" давать 1."""
    subscription = _admin_subscription(device_limit=0, modem_enabled=True)

    await _run_toggle_modem(monkeypatch, subscription, modem_enabled=False)

    assert subscription.device_limit == 0
    assert subscription.modem_enabled is False


async def test_admin_toggle_modem_on_increments_positive_limit(monkeypatch):
    """Прежнее поведение для обычного лимита: включение модема добавляет 1
    устройство."""
    subscription = _admin_subscription(device_limit=3, modem_enabled=False)

    await _run_toggle_modem(monkeypatch, subscription, modem_enabled=True)

    assert subscription.device_limit == 4


async def test_admin_toggle_modem_off_decrements_positive_limit(monkeypatch):
    """Прежнее поведение: выключение модема отнимает 1 устройство."""
    subscription = _admin_subscription(device_limit=4, modem_enabled=True)

    await _run_toggle_modem(monkeypatch, subscription, modem_enabled=False)

    assert subscription.device_limit == 3


async def test_admin_toggle_modem_off_does_not_go_below_one(monkeypatch):
    """Выключение модема не должно опускать положительный лимит ниже 1 —
    это порог отдельно от семантики нуля-как-безлимита."""
    subscription = _admin_subscription(device_limit=1, modem_enabled=True)

    await _run_toggle_modem(monkeypatch, subscription, modem_enabled=False)

    assert subscription.device_limit == 1
