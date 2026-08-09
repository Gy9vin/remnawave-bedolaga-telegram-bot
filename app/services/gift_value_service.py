"""Перевод суммы подарка в дни подписки по цене получателя.

Допустройства тарифицируются за каждый период (в pricing_engine —
``extra_devices × device_price × months``), поэтому «месяц подписки» стоит у
разных людей по-разному. Подарок по коду покупается, когда получатель ещё
неизвестен, и применить его к конкретному человеку честно можно лишь одним
способом: перевести уплаченную сумму в дни по ЕГО цене.

Прежняя схема — «подарок = тариф подарка на купленный период» — ломалась в обе
стороны. Получателю с десятью устройствами она обнуляла лимит до тарифного и
стирала привязки; а если бы лимит сохраняли, он получил бы девять оплачиваемых
устройств даром на весь подаренный срок.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.pricing_engine import pricing_engine


logger = structlog.get_logger(__name__)


@dataclass
class GiftValue:
    """Во что превратилась сумма подарка для конкретного получателя."""

    days: int
    remainder_kopeks: int
    basis_period_days: int
    price_per_period: int


def available_periods_for(subscription) -> list[int]:
    """Периоды, которые получатель реально может купить.

    Зеркалит выбор периодов при продлении (``routes/subscription_modules/
    renewal.py``): в тарифном режиме это ключи ``period_prices`` активного
    тарифа. Пустой список означает «ограничений нет» — считаем по периоду
    подарка как есть.
    """
    tariff = getattr(subscription, 'tariff', None)
    if getattr(subscription, 'tariff_id', None) and tariff and tariff.is_active and tariff.period_prices:
        return sorted(int(period) for period in tariff.period_prices)
    return []


async def convert_gift_to_days(
    db: AsyncSession,
    *,
    subscription,
    user,
    amount_kopeks: int,
    preferred_period_days: int,
) -> GiftValue:
    """Сколько дней даст сумма подарка этому получателю.

    Округляем вниз, а остаток возвращаем копейками на баланс: так ничего не
    испаряется и не нужно объяснять человеку, куда делись деньги.
    """
    periods = available_periods_for(subscription)
    if preferred_period_days in periods or not periods:
        basis = preferred_period_days
    else:
        # Периода подарка у тарифа получателя нет — движок цен по нему откажет,
        # поэтому считаем дневную цену по наименьшему доступному.
        basis = periods[0]

    pricing = await pricing_engine.calculate_renewal_price(db, subscription, basis, user=user)
    price = max(0, int(pricing.final_total))

    if price <= 0:
        # У получателя стопроцентная скидка: делить не на что, отдаём
        # купленный период целиком.
        return GiftValue(
            days=preferred_period_days,
            remainder_kopeks=0,
            basis_period_days=basis,
            price_per_period=0,
        )

    days = (amount_kopeks * basis) // price
    spent = (days * price) // basis

    return GiftValue(
        days=int(days),
        remainder_kopeks=max(0, amount_kopeks - spent),
        basis_period_days=basis,
        price_per_period=price,
    )
