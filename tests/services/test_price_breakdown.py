"""Тесты для сборщика расшифровки цены (app/services/price_breakdown.py).

Цель: люди не понимают, из чего складывается цена, и думают, что доп.
устройства оплачиваются разово. build_price_lines() — единственное место,
где собирается расшифровка, поэтому здесь проверяется в первую очередь,
что стоимость доп. устройств явно помечена как ежемесячная.
"""

from app.config import settings
from app.services.price_breakdown import PriceLine, build_price_lines, format_price_lines
from app.services.pricing_engine import RenewalPricing


def make_pricing(
    *,
    base_price: int = 100000,
    servers_price: int = 0,
    traffic_price: int = 0,
    devices_price: int = 0,
    promo_group_discount: int = 0,
    promo_offer_discount: int = 0,
    final_total: int,
    period_days: int = 30,
    is_tariff_mode: bool = True,
    extra_devices: int = 0,
    months_in_period: int = 1,
) -> RenewalPricing:
    return RenewalPricing(
        base_price=base_price,
        servers_price=servers_price,
        traffic_price=traffic_price,
        devices_price=devices_price,
        promo_group_discount=promo_group_discount,
        promo_offer_discount=promo_offer_discount,
        final_total=final_total,
        period_days=period_days,
        is_tariff_mode=is_tariff_mode,
        breakdown={
            'extra_devices': extra_devices,
            'months_in_period': months_in_period,
        },
    )


class TestNoExtraDevices:
    def test_no_devices_line_without_extra_devices(self):
        pricing = make_pricing(base_price=100000, final_total=100000, extra_devices=0)

        lines = build_price_lines(pricing)

        assert not any('устрой' in line.label.lower() for line in lines)


class TestDevicesLineMonthlyHint:
    def test_devices_line_present_and_hint_mentions_per_month(self):
        # 3 доп. устройства по 100 ₽/мес, период = 1 месяц
        device_price = 10000
        devices_total = device_price * 3
        pricing = make_pricing(
            base_price=100000,
            devices_price=devices_total,
            final_total=100000 + devices_total,
            extra_devices=3,
            months_in_period=1,
        )

        lines = build_price_lines(pricing)
        device_lines = [line for line in lines if 'устрой' in line.label.lower()]

        assert len(device_lines) == 1
        device_line = device_lines[0]
        assert device_line.amount_kopeks == devices_total
        assert '3' in device_line.label
        assert device_line.hint is not None
        assert 'в месяц за устройство' in device_line.hint

    def test_devices_line_hint_shows_month_multiplication_for_longer_period(self):
        # 2 доп. устройства по 50 ₽/мес, период = 3 месяца
        device_price = 5000
        months = 3
        extra_devices = 2
        devices_total = device_price * extra_devices * months
        pricing = make_pricing(
            base_price=100000,
            devices_price=devices_total,
            final_total=100000 + devices_total,
            extra_devices=extra_devices,
            months_in_period=months,
        )

        lines = build_price_lines(pricing)
        device_line = next(line for line in lines if 'устрой' in line.label.lower())

        assert 'в месяц за устройство' in device_line.hint
        assert '3' in device_line.hint
        assert 'мес' in device_line.hint

    def test_device_price_kopeks_can_be_passed_explicitly(self):
        pricing = make_pricing(
            base_price=100000,
            devices_price=30000,
            final_total=130000,
            extra_devices=3,
            months_in_period=1,
        )

        lines = build_price_lines(pricing, device_price_kopeks=10000)
        device_line = next(line for line in lines if 'устрой' in line.label.lower())

        assert settings.format_price(10000) in device_line.hint


class TestDiscountLine:
    def test_discount_is_negative_amount(self):
        pricing = make_pricing(
            base_price=100000,
            promo_offer_discount=10000,
            final_total=90000,
        )

        lines = build_price_lines(pricing)
        discount_lines = [line for line in lines if line.amount_kopeks < 0]

        assert len(discount_lines) == 1
        assert discount_lines[0].amount_kopeks == -10000


class TestSumEqualsFinalTotal:
    def test_sum_of_lines_equals_final_total_simple(self):
        pricing = make_pricing(base_price=100000, final_total=100000)

        lines = build_price_lines(pricing)

        assert sum(line.amount_kopeks for line in lines) == pricing.final_total

    def test_sum_of_lines_equals_final_total_with_devices_and_discount(self):
        device_price = 10000
        extra_devices = 3
        months = 2
        devices_total = device_price * extra_devices * months
        base_price = 100000
        promo_offer_discount = 15000
        subtotal = base_price + devices_total
        final_total = subtotal - promo_offer_discount
        pricing = make_pricing(
            base_price=base_price,
            devices_price=devices_total,
            promo_offer_discount=promo_offer_discount,
            final_total=final_total,
            extra_devices=extra_devices,
            months_in_period=months,
        )

        lines = build_price_lines(pricing)

        assert sum(line.amount_kopeks for line in lines) == pricing.final_total


class TestZeroComponentsOmitted:
    def test_zero_servers_and_traffic_not_shown(self):
        pricing = make_pricing(
            base_price=100000,
            servers_price=0,
            traffic_price=0,
            final_total=100000,
        )

        lines = build_price_lines(pricing)

        assert not any('сервер' in line.label.lower() for line in lines)
        assert not any('трафик' in line.label.lower() for line in lines)

    def test_nonzero_servers_and_traffic_shown(self):
        pricing = make_pricing(
            base_price=100000,
            servers_price=20000,
            traffic_price=5000,
            final_total=125000,
        )

        lines = build_price_lines(pricing)

        assert any('сервер' in line.label.lower() for line in lines)
        assert any('трафик' in line.label.lower() for line in lines)


class TestFormatPriceLines:
    def test_format_contains_total(self):
        lines = [
            PriceLine(label='Тариф', amount_kopeks=100000, hint=None),
        ]

        text = format_price_lines(lines, total_kopeks=100000)

        assert settings.format_price(100000) in text
        assert 'Итого' in text

    def test_format_includes_hint_line(self):
        lines = [
            PriceLine(
                label='Доп. устройства (3 шт.)',
                amount_kopeks=30000,
                hint='100 ₽ в месяц за устройство × 3 шт.',
            ),
        ]

        text = format_price_lines(lines, total_kopeks=30000)

        assert 'в месяц за устройство' in text

    def test_format_negative_amount_rendered_with_minus(self):
        lines = [
            PriceLine(label='Тариф', amount_kopeks=100000, hint=None),
            PriceLine(label='Скидка', amount_kopeks=-10000, hint=None),
        ]

        text = format_price_lines(lines, total_kopeks=90000)

        assert settings.format_price(-10000) in text
