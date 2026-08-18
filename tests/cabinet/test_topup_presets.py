"""Готовые суммы пополнения выводятся из цен действующих тарифов.

Смысл в том, чтобы человек пополнил ровно на тариф и купил без остатка на
балансе. Поэтому суммы — это реальные цены периодов, а не круглые числа.
"""

from app.cabinet.routes.topup_presets import build_presets


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
