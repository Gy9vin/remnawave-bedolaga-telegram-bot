"""Единая сборка расшифровки цены для показа пользователю.

Все места в боте/кабинете, где нужно показать "из чего сложилась цена"
(продление, покупка, докупка устройств и т.п.), должны использовать
``build_price_lines`` / ``format_price_lines`` вместо того, чтобы каждый
раз заново собирать текст руками — иначе формулировки расходятся и люди
продолжают путать разовую и ежемесячную оплату доп. устройств.

Важно про строку доп. устройств: ``PriceLine.amount_kopeks`` для неё —
это полная сумма за ВЕСЬ период (как и остальные строки), а вот
``hint`` обязан явно проговаривать, что цена за устройство — ежемесячная,
и, если период дольше месяца, показывать умножение на число месяцев.

Про скидки: ``pricing.base_price`` / ``servers_price`` / ``traffic_price`` /
``devices_price`` в ``RenewalPricing`` уже посчитаны ПОСЛЕ применения
скидки промо-группы (см. app/services/pricing_engine.py) — то есть group
discount уже "зашит" в эти суммы. Поэтому отдельной строкой показывается
только скидка по промокоду (``promo_offer_discount``), которая применяется
поверх этих сумм: так сумма всех строк точно равна ``final_total`` и
скидка группы не вычитается дважды.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import settings
from app.services.pricing_engine import RenewalPricing


@dataclass(frozen=True)
class PriceLine:
    """Одна строка расшифровки цены."""

    label: str
    amount_kopeks: int
    hint: str | None = None


def build_price_lines(
    pricing: RenewalPricing,
    *,
    device_price_kopeks: int | None = None,
) -> list[PriceLine]:
    """Собирает построчную расшифровку итоговой цены.

    Args:
        pricing: результат расчёта из PricingEngine.
        device_price_kopeks: цена одного устройства в месяц для hint'а.
            Если не передана — вычисляется из pricing.devices_price,
            числа доп. устройств и числа месяцев в периоде.
    """

    lines: list[PriceLine] = []

    if pricing.base_price:
        base_label = 'Тариф' if pricing.is_tariff_mode else 'Период'
        lines.append(PriceLine(label=base_label, amount_kopeks=pricing.base_price))

    if pricing.servers_price:
        lines.append(PriceLine(label='Серверы', amount_kopeks=pricing.servers_price))

    if pricing.traffic_price:
        lines.append(PriceLine(label='Трафик', amount_kopeks=pricing.traffic_price))

    breakdown = pricing.breakdown or {}
    extra_devices = int(breakdown.get('extra_devices') or 0)

    if extra_devices > 0:
        months = int(breakdown.get('months_in_period') or 1) or 1

        if device_price_kopeks is None:
            denom = extra_devices * months
            device_price_kopeks = pricing.devices_price // denom if denom else 0

        price_label = settings.format_price(device_price_kopeks)
        if months > 1:
            hint = f'{price_label} в месяц за устройство × {extra_devices} шт. × {months} мес.'
        else:
            hint = f'{price_label} в месяц за устройство × {extra_devices} шт.'

        lines.append(
            PriceLine(
                label=f'Доп. устройства ({extra_devices} шт.)',
                amount_kopeks=pricing.devices_price,
                hint=hint,
            )
        )

    if pricing.promo_offer_discount:
        lines.append(
            PriceLine(label='Скидка по промокоду', amount_kopeks=-pricing.promo_offer_discount)
        )

    return lines


def format_price_lines(lines: list[PriceLine], total_kopeks: int) -> str:
    """Форматирует расшифровку цены в готовый HTML-текст для сообщений бота."""

    parts: list[str] = []
    for line in lines:
        amount_label = settings.format_price(line.amount_kopeks)
        parts.append(f'{line.label} — {amount_label}')
        if line.hint:
            parts.append(f'<i>{line.hint}</i>')

    parts.append('')
    parts.append(f'<b>Итого:</b> {settings.format_price(total_kopeks)}')

    return '\n'.join(parts)
