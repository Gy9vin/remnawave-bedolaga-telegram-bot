"""Безлимит устройств должен переживать всю цепочку бот → панель.

``subscription.device_limit == 0`` — это «лимит HWID отключён», а не «лимит
не задан». Так это понимает и панель: в ``subscription.service.ts`` ветка
``if (user.hwidDeviceLimit === 0)`` регистрирует устройство и возвращает
``limitBypassed: true``. Значит ноль обязан доезжать до панели, а не
подменяться единицей и не выбрасываться как «невалидный».
"""

import pytest

from app.utils import subscription_utils
from app.utils.subscription_utils import (
    resolve_hwid_device_limit,
    resolve_hwid_device_limit_for_payload,
)


class DummySubscription:
    def __init__(self, device_limit=None):
        self.device_limit = device_limit
        self.id = 1547


class StubSettings:
    def __init__(self, enabled: bool, disabled_amount=None):
        self._enabled = enabled
        self._disabled_amount = disabled_amount
        self.SIMPLE_SUBSCRIPTION_DEVICE_LIMIT = 3

    def is_devices_selection_enabled(self) -> bool:
        return self._enabled

    def get_disabled_mode_device_limit(self):
        return self._disabled_amount

    def get_devices_selection_disabled_amount(self):
        return self._disabled_amount


@pytest.mark.parametrize('selection_enabled', [True, False])
def test_zero_is_sent_to_panel_as_unlimited(monkeypatch, selection_enabled):
    """Ноль — валидное значение и должен уходить в панель как есть."""
    monkeypatch.setattr(
        subscription_utils,
        'settings',
        StubSettings(enabled=selection_enabled),
    )
    subscription = DummySubscription(device_limit=0)

    assert resolve_hwid_device_limit(subscription) == 0
    assert resolve_hwid_device_limit_for_payload(subscription) == 0


def test_unlimited_beats_forced_limit(monkeypatch):
    """Принудительный лимит режима не должен отбирать уже выданный безлимит."""
    monkeypatch.setattr(
        subscription_utils,
        'settings',
        StubSettings(enabled=False, disabled_amount=2),
    )
    subscription = DummySubscription(device_limit=0)

    assert resolve_hwid_device_limit(subscription) == 0
    assert resolve_hwid_device_limit_for_payload(subscription) == 0


@pytest.mark.parametrize('broken', [None, -1, -5])
def test_broken_values_still_produce_no_payload(monkeypatch, broken):
    """Отсутствующий и отрицательный лимит по-прежнему не отправляем."""
    monkeypatch.setattr(
        subscription_utils,
        'settings',
        StubSettings(enabled=True),
    )
    subscription = DummySubscription(device_limit=broken)

    assert resolve_hwid_device_limit(subscription) is None
    assert resolve_hwid_device_limit_for_payload(subscription) is None


def test_positive_limit_is_unchanged(monkeypatch):
    """Обычный числовой лимит работает как раньше."""
    monkeypatch.setattr(
        subscription_utils,
        'settings',
        StubSettings(enabled=True),
    )
    subscription = DummySubscription(device_limit=5)

    assert resolve_hwid_device_limit(subscription) == 5
    assert resolve_hwid_device_limit_for_payload(subscription) == 5
