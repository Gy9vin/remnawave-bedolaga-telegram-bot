"""Пропорциональный возврат за отключаемое место.

Истории покупок устройств в базе нет — Transaction.description это свободный
текст. Поэтому возврат считаем от ТЕКУЩЕЙ цены места на оставшийся срок, той же
формулой, что и покупку. Купил на 30 дней за 60 рублей, отказался через день —
вернём 58. Разница в цену одного дня, в пользу клиента.
"""

import pytest

from app.utils.device_refund import calculate_device_refund_kopeks


def test_full_period_refunds_full_price():
    assert calculate_device_refund_kopeks(6000, slots=1, days_left=30) == 6000


def test_half_period_refunds_half():
    assert calculate_device_refund_kopeks(6000, slots=1, days_left=15) == 3000


def test_several_slots_multiply():
    assert calculate_device_refund_kopeks(6000, slots=2, days_left=24) == 9600


def test_longer_than_month_is_not_capped():
    """Годовая подписка — место оплачено на весь срок, возврат тоже за весь."""
    assert calculate_device_refund_kopeks(6000, slots=1, days_left=90) == 18000


@pytest.mark.parametrize('days_left', [0, -1, -100])
def test_expired_subscription_refunds_nothing(days_left):
    assert calculate_device_refund_kopeks(6000, slots=1, days_left=days_left) == 0


@pytest.mark.parametrize('slots', [0, -1])
def test_no_slots_refunds_nothing(slots):
    assert calculate_device_refund_kopeks(6000, slots=slots, days_left=30) == 0


def test_zero_device_price_refunds_nothing():
    """Место бесплатно — возвращать нечего, а не «минимум один рубль»."""
    assert calculate_device_refund_kopeks(0, slots=2, days_left=30) == 0


def test_result_is_rounded_down_never_up():
    """Округление в пользу сервиса на копейки, чтобы возврат не превысил уплаченное."""
    assert calculate_device_refund_kopeks(6000, slots=1, days_left=7) == 1400


def test_garbage_price_is_treated_as_zero():
    assert calculate_device_refund_kopeks(None, slots=1, days_left=30) == 0
    assert calculate_device_refund_kopeks(-500, slots=1, days_left=30) == 0
