"""Готовые суммы пополнения выводятся из цен действующих тарифов.

Смысл в том, чтобы человек пополнил ровно на тариф и купил без остатка на
балансе. Поэтому суммы — это реальные цены периодов, а не круглые числа.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.cabinet.routes import topup_presets as topup_presets_module
from app.cabinet.routes.topup_presets import build_presets, get_topup_presets


def test_presets_are_sorted_and_deduplicated():
    presets = build_presets({30: 24900, 90: 64900, 180: 119000})
    assert [p['amount_kopeks'] for p in presets] == [24900, 64900, 119000]
    assert [p['label_days'] for p in presets] == [30, 90, 180]


def test_identical_prices_collapse_keeping_shortest_period():
    """Две одинаковые цены — одна кнопка, и подписана коротким периодом."""
    presets = build_presets({30: 24900, 60: 24900})
    assert presets == [{'amount_kopeks': 24900, 'label_days': 30}]


def test_zero_and_negative_prices_are_dropped():
    presets = build_presets({30: 0, 90: -100, 180: 119000})
    assert presets == [{'amount_kopeks': 119000, 'label_days': 180}]


def test_empty_input_gives_empty_list():
    assert build_presets({}) == []
    assert build_presets(None) == []


def test_string_keys_from_json_are_accepted():
    """period_prices приходит из JSON-колонки, где ключи строковые."""
    presets = build_presets({'30': 24900, '90': 64900})
    assert [p['label_days'] for p in presets] == [30, 90]


def test_garbage_entries_are_ignored_not_fatal():
    presets = build_presets({'abc': 1000, 30: 'free', 90: 64900})
    assert presets == [{'amount_kopeks': 64900, 'label_days': 90}]


def test_more_than_four_presets_are_trimmed():
    """Больше четырёх кнопок в ряд не помещается — берём самые ходовые периоды."""
    presets = build_presets({30: 100, 90: 200, 180: 300, 360: 400, 720: 500})
    assert len(presets) == 4
    assert [p['label_days'] for p in presets] == [30, 90, 180, 360]


class _FakeUser:
    """Достаточно для get_topup_presets: id + резолвер промогруппы."""

    def __init__(self):
        self.id = 1
        self.balance_kopeks = 0
        self.promo_group = None
        self.user_promo_groups = []

    def get_primary_promo_group(self):
        return None


def _tariff(*, period_prices=None, is_daily=False, daily_price_kopeks=0):
    return SimpleNamespace(
        period_prices=period_prices or {},
        is_daily=is_daily,
        daily_price_kopeks=daily_price_kopeks,
        get_purchasable_periods=lambda: (
            [1] if is_daily and daily_price_kopeks else sorted(int(p) for p in (period_prices or {}))
        ),
        get_purchasable_price_for_period=lambda days: (
            daily_price_kopeks
            if is_daily and days == 1
            else (period_prices or {}).get(str(days))
        ),
    )


def _call_get_topup_presets(tariffs):
    orig_mode = topup_presets_module.settings.SALES_MODE
    topup_presets_module.settings.SALES_MODE = 'tariffs'
    try:
        with patch(
            'app.database.crud.tariff.get_tariffs_for_user',
            new=AsyncMock(return_value=tariffs),
        ):
            return asyncio.run(get_topup_presets(user=_FakeUser(), db=None))
    finally:
        topup_presets_module.settings.SALES_MODE = orig_mode


def test_daily_only_tariff_still_produces_a_preset():
    """Регрессия: если из доступных тарифов только суточный, period_prices у
    него пуст (цена лежит в daily_price_kopeks) — раньше это читалось
    напрямую из колонки и суточный тариф пропадал из готовых сумм целиком,
    хотя он вполне живой и покупаемый (см. get_purchasable_periods)."""
    tariff = _tariff(is_daily=True, daily_price_kopeks=5000)
    result = _call_get_topup_presets([tariff])
    assert result['presets'] == [{'amount_kopeks': 5000, 'label_days': 1}]


def test_mixed_daily_and_regular_tariffs_combine_presets():
    daily = _tariff(is_daily=True, daily_price_kopeks=5000)
    regular = _tariff(period_prices={'30': 24900, '90': 64900})
    result = _call_get_topup_presets([daily, regular])
    assert result['presets'] == [
        {'amount_kopeks': 5000, 'label_days': 1},
        {'amount_kopeks': 24900, 'label_days': 30},
        {'amount_kopeks': 64900, 'label_days': 90},
    ]
