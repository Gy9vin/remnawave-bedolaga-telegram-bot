"""Резолвинг эффективного режима кабинета.

Правило: явный выбор человека сильнее глобального дефолта. Глобальный флаг
действует только на тех, кто не выбирал (choice is None).
"""

import pytest

from app.utils.ui_mode import (
    UI_MODE_ADVANCED,
    UI_MODE_SIMPLE,
    normalize_ui_mode,
    resolve_ui_mode,
)


@pytest.mark.parametrize('lite_enabled', [True, False])
def test_explicit_choice_wins_over_global_default(lite_enabled):
    assert resolve_ui_mode(UI_MODE_SIMPLE, lite_mode_enabled=lite_enabled) == UI_MODE_SIMPLE
    assert resolve_ui_mode(UI_MODE_ADVANCED, lite_mode_enabled=lite_enabled) == UI_MODE_ADVANCED


def test_no_choice_follows_global_default():
    assert resolve_ui_mode(None, lite_mode_enabled=True) == UI_MODE_SIMPLE
    assert resolve_ui_mode(None, lite_mode_enabled=False) == UI_MODE_ADVANCED


@pytest.mark.parametrize('garbage', ['', '  ', 'lite', 'SIMPLE_MODE', 'null', 0, 1, [], {}])
def test_garbage_choice_falls_back_to_global_default(garbage):
    """Мусор в колонке не должен ронять кабинет и не должен молча значить 'simple'."""
    assert resolve_ui_mode(garbage, lite_mode_enabled=False) == UI_MODE_ADVANCED
    assert resolve_ui_mode(garbage, lite_mode_enabled=True) == UI_MODE_SIMPLE


@pytest.mark.parametrize(
    'raw, expected',
    [
        ('simple', UI_MODE_SIMPLE),
        ('advanced', UI_MODE_ADVANCED),
        ('  simple  ', UI_MODE_SIMPLE),
        ('SIMPLE', UI_MODE_SIMPLE),
        ('Advanced', UI_MODE_ADVANCED),
        (None, None),
        ('', None),
        ('lite', None),
        (5, None),
    ],
)
def test_normalize_ui_mode(raw, expected):
    assert normalize_ui_mode(raw) == expected
