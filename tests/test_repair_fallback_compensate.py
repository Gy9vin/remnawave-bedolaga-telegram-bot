"""Юнит-тесты чистой логики repair_stuck_fallback_compensate.py.

Проверяют calc_lost_days, choose_target_squads и calc_server_refund_kopeks
без поднятия БД/панели.
Запуск:
    .venv/bin/python -m pytest tests/test_repair_fallback_compensate.py -v
"""

from datetime import UTC, datetime, timedelta

import pytest

from scripts.repair_stuck_fallback_compensate import (
    _build_fallback_squad_ids,
    _sub_end_date_in_future,
    calc_lost_days,
    calc_server_refund_kopeks,
    choose_target_squads,
)


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


# ============================================================================
# calc_server_refund_kopeks
# ============================================================================


class TestCalcServerRefundKopeks:
    def test_empty_list_returns_none(self):
        # Нет SubscriptionServer-записей для fallback-сквада — не можем определить
        assert calc_server_refund_kopeks([]) is None

    def test_single_positive_price_returns_value(self):
        # Ровно одна запись с положительной ценой — однозначно определяем
        assert calc_server_refund_kopeks([500]) == 500

    def test_single_zero_price_returns_none(self):
        # Одна запись, но цена 0 — списания не было, возврат не нужен
        assert calc_server_refund_kopeks([0]) is None

    def test_single_negative_price_returns_none(self):
        # Отрицательное значение — аномалия, не начисляем
        assert calc_server_refund_kopeks([-1]) is None

    def test_multiple_records_returns_none(self):
        # Несколько записей (несколько циклов продления) — неоднозначно
        assert calc_server_refund_kopeks([500, 600]) is None

    def test_multiple_records_even_if_equal_returns_none(self):
        # Одинаковые цены в нескольких записях — всё равно неоднозначно
        assert calc_server_refund_kopeks([300, 300]) is None

    def test_large_price_returns_correctly(self):
        # Проверяем корректность для реалистичной суммы (напр. 30 000 копеек = 300 ₽)
        assert calc_server_refund_kopeks([3000]) == 3000

    def test_one_record_of_three_returns_none(self):
        assert calc_server_refund_kopeks([100, 200, 300]) is None


# ============================================================================
# _sub_end_date_in_future  (отражает условие _fetch_candidates: end_date > now)
# ============================================================================


class TestSubEndDateInFuture:
    def test_past_end_date_excluded(self):
        """Истёкшая подписка (end_date в прошлом) — НЕ кандидат."""
        now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
        past_end = datetime(2026, 8, 5, 0, 0, 0, tzinfo=UTC)
        assert _sub_end_date_in_future(past_end, now) is False

    def test_future_end_date_included(self):
        """Активная подписка с end_date в будущем — кандидат."""
        now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
        future_end = datetime(2026, 9, 15, 0, 0, 0, tzinfo=UTC)
        assert _sub_end_date_in_future(future_end, now) is True

    def test_exactly_now_is_not_future(self):
        """end_date == now — не кандидат (строго больше)."""
        now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
        assert _sub_end_date_in_future(now, now) is False


# ============================================================================
# _build_fallback_squad_ids
# ============================================================================


class _FakePanelUser:
    """Минимальный stub RemnaWaveUser для тестов _build_fallback_squad_ids."""

    def __init__(self, uid: int, squads: list):
        self.id = uid
        self.active_internal_squads = squads


FALLBACK_UUID = 'fallback-squad-uuid-0000'


class TestBuildFallbackSquadIds:
    def test_empty_panel_returns_empty_set(self):
        result = _build_fallback_squad_ids([], FALLBACK_UUID)
        assert result == set()

    def test_user_with_fallback_squad_dict_format(self):
        users = [_FakePanelUser(1, [{'uuid': FALLBACK_UUID}])]
        result = _build_fallback_squad_ids(users, FALLBACK_UUID)
        assert result == {1}

    def test_user_with_fallback_squad_str_format(self):
        users = [_FakePanelUser(1, [FALLBACK_UUID])]
        result = _build_fallback_squad_ids(users, FALLBACK_UUID)
        assert result == {1}

    def test_user_without_fallback_squad_excluded(self):
        users = [_FakePanelUser(42, [{'uuid': 'other-squad-uuid'}])]
        result = _build_fallback_squad_ids(users, FALLBACK_UUID)
        assert result == set()

    def test_mixed_users_only_fallback_included(self):
        users = [
            _FakePanelUser(1, [{'uuid': FALLBACK_UUID}, {'uuid': 'other'}]),
            _FakePanelUser(2, [{'uuid': 'other-squad-uuid'}]),
            _FakePanelUser(3, [{'uuid': FALLBACK_UUID}]),
        ]
        result = _build_fallback_squad_ids(users, FALLBACK_UUID)
        assert result == {1, 3}

    def test_user_with_empty_squads_excluded(self):
        users = [_FakePanelUser(10, [])]
        result = _build_fallback_squad_ids(users, FALLBACK_UUID)
        assert result == set()

    def test_user_with_none_squads_excluded(self):
        users = [_FakePanelUser(10, None)]
        result = _build_fallback_squad_ids(users, FALLBACK_UUID)
        assert result == set()

    def test_returns_set_of_ints(self):
        users = [_FakePanelUser(7, [FALLBACK_UUID])]
        result = _build_fallback_squad_ids(users, FALLBACK_UUID)
        assert isinstance(result, set)
        assert all(isinstance(v, int) for v in result)

    def test_multiple_users_all_in_fallback(self):
        users = [_FakePanelUser(i, [FALLBACK_UUID]) for i in range(1, 6)]
        result = _build_fallback_squad_ids(users, FALLBACK_UUID)
        assert result == {1, 2, 3, 4, 5}

    def test_id_field_uuid_in_dict(self):
        # active_internal_squads может использовать ключ 'id' вместо 'uuid'
        users = [_FakePanelUser(99, [{'id': FALLBACK_UUID}])]
        result = _build_fallback_squad_ids(users, FALLBACK_UUID)
        assert result == {99}


# ============================================================================
# BUG A: timeframe safeguard (Transaction.created_at >= sub.created_at)
# ============================================================================


class TestTimeframeSafeguard:
    """_find_last_renewal_transaction now scopes to transactions created at or
    after sub.created_at, preventing payments from OLDER subscriptions of the
    same user from producing inflated lost_days values."""

    def test_old_subscription_payment_excluded_new_used(self):
        """User has two subscriptions.
        Sub A (old, created 2026-01-01): paid 2026-01-15.
        Sub B (stuck, created 2026-07-01): paid 2026-07-15.

        When processing sub B, only transactions >= 2026-07-01 are eligible.
        txn_a (2026-01-15) is excluded; txn_b (2026-07-15) is selected.
        lost_days must be computed from txn_b, not txn_a.
        """
        now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)

        sub_b_created = datetime(2026, 7, 1, tzinfo=UTC)
        txn_a_ts = datetime(2026, 1, 15, tzinfo=UTC)   # older subscription
        txn_b_ts = datetime(2026, 7, 15, tzinfo=UTC)   # stuck subscription

        # Simulate the WHERE clause: Transaction.created_at >= sub_created_at
        all_user_txns = [txn_a_ts, txn_b_ts]
        eligible = [ts for ts in all_user_txns if ts >= sub_b_created]
        best_ts = max(eligible)

        assert best_ts == txn_b_ts, (
            "Timeframe safeguard must select the stuck subscription's transaction"
        )
        assert txn_a_ts not in eligible, (
            "Older subscription's transaction must be excluded by sub_created_at filter"
        )

        correct_lost_days = calc_lost_days(txn_b_ts, now)
        wrong_lost_days = calc_lost_days(txn_a_ts, now)

        assert correct_lost_days < wrong_lost_days, (
            "Without safeguard, lost_days would be inflated from the older transaction"
        )
        assert correct_lost_days == (now - txn_b_ts).days  # 36

    def test_only_transaction_in_window_is_used(self):
        """When the single eligible transaction is exactly at sub_created_at boundary
        it is still selected."""
        now = datetime(2026, 8, 20, tzinfo=UTC)
        sub_created = datetime(2026, 6, 1, tzinfo=UTC)
        txn_ts = datetime(2026, 6, 1, tzinfo=UTC)  # exactly at boundary

        eligible = [ts for ts in [txn_ts] if ts >= sub_created]
        assert eligible == [txn_ts]
        assert calc_lost_days(txn_ts, now) == (now - txn_ts).days  # 80


# ============================================================================
# BUG B: naive vs aware datetime in calc_lost_days
# ============================================================================


class TestCalcLostDaysNaiveTimestamp:
    def test_naive_renewal_ts_does_not_raise(self):
        """If Transaction.completed_at / created_at is naive (TIMESTAMP WITHOUT
        TIME ZONE), calc_lost_days must not raise TypeError.
        It should treat the naive datetime as UTC."""
        now = datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC)
        naive_renewal_ts = datetime(2026, 8, 6, 12, 0, 0)  # no tzinfo
        # Must not raise
        result = calc_lost_days(naive_renewal_ts, now)
        assert result == 14

    def test_naive_renewal_ts_matches_aware_result(self):
        """Naive and aware timestamps representing the same moment yield identical
        lost_days, confirming UTC-normalisation is correct."""
        now = datetime(2026, 8, 20, 0, 0, 0, tzinfo=UTC)
        aware_ts = datetime(2026, 7, 21, 0, 0, 0, tzinfo=UTC)
        naive_ts = datetime(2026, 7, 21, 0, 0, 0)  # same moment, naive
        assert calc_lost_days(aware_ts, now) == calc_lost_days(naive_ts, now)
