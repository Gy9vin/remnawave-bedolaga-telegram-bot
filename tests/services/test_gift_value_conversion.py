"""Сумма подарка превращается в дни по цене получателя.

Допустройства тарифицируются за каждый период (pricing_engine: extra_devices ×
device_price × months), поэтому «месяц подписки» для человека с одним
устройством и с десятью — разный товар за разные деньги. Подарок по коду
покупается, когда получатель ещё неизвестен: применить фиксированный пакет к
произвольному человеку нельзя, не отдав лишнее даром или не отобрав оплаченное.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import gift_value_service as svc


def _sub(periods=None):
    tariff = SimpleNamespace(is_active=True, period_prices={'30': 20000} if periods is None else periods)
    return SimpleNamespace(id=1, tariff_id=7, tariff=tariff)


def _price(final_total: int):
    return AsyncMock(return_value=SimpleNamespace(final_total=final_total))


@pytest.mark.asyncio
async def test_same_price_gives_the_bought_period(monkeypatch):
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', _price(20000))

    value = await svc.convert_gift_to_days(
        AsyncMock(),
        subscription=_sub(),
        user=SimpleNamespace(id=2),
        amount_kopeks=20000,
        preferred_period_days=30,
    )

    assert value.days == 30
    assert value.remainder_kopeks == 0


@pytest.mark.asyncio
async def test_expensive_recipient_gets_fewer_days(monkeypatch):
    """10 устройств → 450 ₽/мес. Подарок за 200 ₽ = 13 дней."""
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', _price(45000))

    value = await svc.convert_gift_to_days(
        AsyncMock(),
        subscription=_sub(),
        user=SimpleNamespace(id=2),
        amount_kopeks=20000,
        preferred_period_days=30,
    )

    assert value.days == 13
    assert value.remainder_kopeks == 500  # 200 ₽ − 13 дней × 15 ₽


@pytest.mark.asyncio
async def test_cheap_recipient_gets_more_days(monkeypatch):
    """Мы получили 200 ₽ и отдали товара на 200 ₽ — это тоже честно."""
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', _price(10000))

    value = await svc.convert_gift_to_days(
        AsyncMock(),
        subscription=_sub(),
        user=SimpleNamespace(id=2),
        amount_kopeks=20000,
        preferred_period_days=30,
    )

    assert value.days == 60
    assert value.remainder_kopeks == 0


@pytest.mark.asyncio
async def test_gift_smaller_than_a_day_goes_entirely_to_balance(monkeypatch):
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', _price(45000))

    value = await svc.convert_gift_to_days(
        AsyncMock(),
        subscription=_sub(),
        user=SimpleNamespace(id=2),
        amount_kopeks=1000,
        preferred_period_days=30,
    )

    assert value.days == 0
    assert value.remainder_kopeks == 1000


@pytest.mark.asyncio
async def test_free_recipient_falls_back_to_bought_period(monkeypatch):
    """У получателя 100% скидка — делить не на что."""
    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', _price(0))

    value = await svc.convert_gift_to_days(
        AsyncMock(),
        subscription=_sub(),
        user=SimpleNamespace(id=2),
        amount_kopeks=20000,
        preferred_period_days=30,
    )

    assert value.days == 30
    assert value.remainder_kopeks == 0


@pytest.mark.asyncio
async def test_basis_period_falls_back_to_smallest_available(monkeypatch):
    """У тарифа получателя нет периода подарка — движок цен по нему откажет."""
    captured = {}

    async def fake_price(db, subscription, period_days, *, user=None):
        captured['period'] = period_days
        return SimpleNamespace(final_total=10000)

    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', fake_price)

    value = await svc.convert_gift_to_days(
        AsyncMock(),
        subscription=_sub({'90': 30000, '180': 50000}),
        user=SimpleNamespace(id=2),
        amount_kopeks=20000,
        preferred_period_days=30,
    )

    assert captured['period'] == 90
    assert value.basis_period_days == 90


def test_available_periods_ignores_inactive_tariff():
    sub = SimpleNamespace(tariff_id=7, tariff=SimpleNamespace(is_active=False, period_prices={'30': 1}))

    assert svc.available_periods_for(sub) == []
