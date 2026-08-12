"""Продление подписки должно уважать выбор пользователя, а не «кратчайший тариф».

Жалоба: человек пополняет баланс на 3 месяца вперёд, а автопродление тихо
списывает деньги только на 1 месяц (кратчайший период тарифа) — остаток
зависает на балансе, человек идёт в поддержку разбираться, куда делись деньги.

Причина была в трёх хардкодах ``tariff.get_shortest_period() or 30``:
- ``subscription_auto_purchase_service.try_auto_extend_expired_after_topup``
  (главный виновник — срабатывает после любого пополнения без сохранённой
  корзины);
- ``recurrent_payment_service._process_single_subscription``;
- и в двух джобах ``monitoring_service`` — они уже звали
  ``pricing_engine.select_affordable_renewal`` (максимум по деньгам), но
  игнорировали ``subscription.autopay_period_days`` (явный выбор человека).

Фикс — ``PricingEngine.select_renewal_period``: сперва выбор пользователя
(если по карману), иначе максимум по балансу, иначе — «нельзя продлить»,
как и раньше у ``select_affordable_renewal``.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services import recurrent_payment_service, subscription_auto_purchase_service
from app.services.pricing_engine import RenewalPricing, pricing_engine


PRICE_PER_DAY_KOPEKS = 100  # произвольная детерминированная «цена» для теста


class _FakeTariff:
    """Минимальная замена Tariff — только то, что использует select_affordable_renewal."""

    def __init__(self, periods):
        self._periods = list(periods)

    def get_available_periods(self):
        return list(self._periods)

    def get_shortest_period(self):  # pragma: no cover - не должен вызываться фиксом
        raise AssertionError('get_shortest_period() не должен вызываться выбором периода продления')


def _make_subscription(*, autopay_period_days=None, periods=(30, 90, 180)):
    return SimpleNamespace(
        tariff_id=1,
        tariff=_FakeTariff(periods),
        autopay_period_days=autopay_period_days,
    )


def _make_user(balance_kopeks: int):
    return SimpleNamespace(balance_kopeks=balance_kopeks)


async def _fake_calculate_renewal_price(db, subscription, period_days, *, user=None):
    price = period_days * PRICE_PER_DAY_KOPEKS
    return RenewalPricing(
        base_price=price,
        servers_price=0,
        traffic_price=0,
        devices_price=0,
        promo_group_discount=0,
        promo_offer_discount=0,
        final_total=price,
        period_days=period_days,
        is_tariff_mode=True,
    )


@pytest.fixture(autouse=True)
def _patch_pricing(monkeypatch):
    monkeypatch.setattr(pricing_engine, 'calculate_renewal_price', _fake_calculate_renewal_price)


@pytest.mark.asyncio
async def test_no_explicit_choice_picks_longest_affordable_not_shortest():
    """Ровно жалоба: денег хватает на 90 дней — выбираем 90, а не кратчайшие 30."""
    subscription = _make_subscription(autopay_period_days=None)
    user = _make_user(balance_kopeks=90 * PRICE_PER_DAY_KOPEKS)

    result = await pricing_engine.select_renewal_period(None, subscription, user)

    assert result == (90, 90 * PRICE_PER_DAY_KOPEKS)


@pytest.mark.asyncio
async def test_explicit_choice_wins_even_when_more_is_affordable():
    """Пользователь сам выбрал 30 дней автоплатежа — не продлеваем дольше без спроса."""
    subscription = _make_subscription(autopay_period_days=30)
    user = _make_user(balance_kopeks=90 * PRICE_PER_DAY_KOPEKS)  # хватило бы и на 90

    result = await pricing_engine.select_renewal_period(None, subscription, user)

    assert result == (30, 30 * PRICE_PER_DAY_KOPEKS)


@pytest.mark.asyncio
async def test_explicit_choice_ignored_when_unaffordable():
    """Выбрано 90, но денег хватает только на 30 — на выбранный период не хватает."""
    subscription = _make_subscription(autopay_period_days=90)
    user = _make_user(balance_kopeks=30 * PRICE_PER_DAY_KOPEKS)

    result = await pricing_engine.select_renewal_period(None, subscription, user)

    assert result == (30, 30 * PRICE_PER_DAY_KOPEKS)


@pytest.mark.asyncio
async def test_returns_same_contract_as_select_affordable_renewal_when_broke():
    """Не хватает даже на кратчайший период — контракт «нельзя продлить» не меняем."""
    subscription = _make_subscription(autopay_period_days=None)
    user = _make_user(balance_kopeks=1)

    result = await pricing_engine.select_renewal_period(None, subscription, user)
    baseline = await pricing_engine.select_affordable_renewal(None, subscription, user)

    assert result is None
    assert result == baseline


def test_try_auto_extend_expired_after_topup_no_longer_calls_get_shortest_period():
    source = inspect.getsource(subscription_auto_purchase_service.try_auto_extend_expired_after_topup)
    assert 'get_shortest_period' not in source


def test_recurrent_payment_service_no_longer_calls_get_shortest_period():
    source = inspect.getsource(recurrent_payment_service._process_single_subscription)
    assert 'get_shortest_period' not in source
