"""Юнит-тесты чистой логики repair_stuck_fallback_compensate.py.

Проверяют calc_lost_days и choose_target_squads без поднятия БД/панели.
Запуск:
    .venv/bin/python -m pytest tests/test_repair_fallback_compensate.py -v
"""

from datetime import UTC, datetime, timedelta

import pytest

from scripts.repair_stuck_fallback_compensate import calc_lost_days, choose_target_squads


# ============================================================================
# calc_lost_days
# ============================================================================


class TestCalcLostDays:
    def test_typical_two_weeks(self):
        now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
        paid = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)
        assert calc_lost_days(paid, now) == 14

    def test_minimum_one_day_when_less_than_a_day(self):
        now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
        # 30 минут назад — меньше суток → floor 0 → max(1, 0) == 1
        paid = now - timedelta(minutes=30)
        assert calc_lost_days(paid, now) == 1

    def test_minimum_one_day_when_same_moment(self):
        now = datetime(2026, 8, 20, 0, 0, 0, tzinfo=UTC)
        assert calc_lost_days(now, now) == 1

    def test_exactly_one_day(self):
        now = datetime(2026, 8, 20, 0, 0, 0, tzinfo=UTC)
        paid = now - timedelta(days=1)
        assert calc_lost_days(paid, now) == 1

    def test_long_period(self):
        now = datetime(2026, 8, 20, tzinfo=UTC)
        paid = datetime(2026, 5, 1, tzinfo=UTC)
        expected = (now - paid).days  # ~111
        assert calc_lost_days(paid, now) == expected

    def test_default_now_works(self):
        # Без явного now — должна вернуть >= 1 для исторической даты
        paid = datetime(2020, 1, 1, tzinfo=UTC)
        result = calc_lost_days(paid)
        assert result >= 1

    def test_thirty_days(self):
        now = datetime(2026, 8, 20, tzinfo=UTC)
        paid = now - timedelta(days=30)
        assert calc_lost_days(paid, now) == 30

    def test_23_hours_59_minutes(self):
        now = datetime(2026, 8, 20, 23, 59, 0, tzinfo=UTC)
        paid = now - timedelta(hours=23, minutes=59)
        # timedelta.days == 0 → max(1, 0) == 1
        assert calc_lost_days(paid, now) == 1


# ============================================================================
# choose_target_squads
# ============================================================================


class TestChooseTargetSquads:
    def test_connected_squads_wins_over_default(self):
        result = choose_target_squads(['uuid-a', 'uuid-b'], 'default-uuid')
        assert result == ['uuid-a', 'uuid-b']

    def test_empty_list_falls_to_default(self):
        result = choose_target_squads([], 'default-uuid')
        assert result == ['default-uuid']

    def test_none_falls_to_default(self):
        result = choose_target_squads(None, 'default-uuid')
        assert result == ['default-uuid']

    def test_no_squads_no_default_returns_empty(self):
        result = choose_target_squads(None, None)
        assert result == []

    def test_empty_list_no_default_returns_empty(self):
        result = choose_target_squads([], None)
        assert result == []

    def test_empty_string_default_treated_as_falsy(self):
        # Пустая строка — не задан DEFAULT_SQUAD_UUID
        result = choose_target_squads([], '')
        assert result == []

    def test_single_squad_in_connected(self):
        result = choose_target_squads(['only-one'], None)
        assert result == ['only-one']

    def test_connected_squads_is_copied_not_mutated(self):
        original = ['uuid-x']
        result = choose_target_squads(original, 'default-uuid')
        assert result == ['uuid-x']
        # Убедимся, что это не тот же объект (copy)
        result.append('extra')
        assert original == ['uuid-x']

    def test_default_squad_when_connected_squads_not_provided(self):
        result = choose_target_squads(None, 'fallback-squad-uuid')
        assert result == ['fallback-squad-uuid']
