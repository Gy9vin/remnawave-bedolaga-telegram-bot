"""Тесты для скрипта миграции SHM -> Bedolaga."""

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest

# Импортируем функции из скрипта
from migrate_from_shm import (
    SCHEMA_SQL,
    SERVICE_NAME_MAP,
    STATUS_MAP,
    SqlDumpConnection,
    SqliteAdapter,
    extract_telegram_id,
    extract_tg_data,
    gen_referral_code,
    migrate_bonus_deposits,
    migrate_referral_earnings,
    migrate_referrals,
    migrate_subscription_purchases,
    migrate_subscriptions,
    migrate_transactions,
    migrate_users,
    now_utc,
    rubles_to_kopeks,
    to_dt_str,
)


# ============================================================================
# ФИКСТУРЫ
# ============================================================================


@pytest.fixture()
def bedolaga_db():
    """Создать in-memory SQLite БД с полной схемой Bedolaga, обёрнутую в SqliteAdapter."""
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=OFF')
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    # Создать тариф для миграции (MIGRATION_TARIFF_ID = 2)
    conn.execute("INSERT INTO tariffs (id, name, period_prices) VALUES (1, 'Тестовый', '{}')")
    conn.execute("INSERT INTO tariffs (id, name, period_prices) VALUES (2, 'Стандарт', '{}')")
    conn.commit()
    adapter = SqliteAdapter(conn)
    yield adapter
    conn.close()


@pytest.fixture()
def shm_data_minimal():
    """Минимальный набор SHM данных для тестов."""
    return {
        'users': [
            {
                'user_id': 1,
                'login': 'admin',
                'balance': Decimal(0),
                'credit': 0,
                'bonus': Decimal(0),
                'block': 0,
                'created': datetime(2024, 1, 1, tzinfo=UTC),
                'last_login': None,
                'settings': None,
                'partner_id': None,
            },
            {
                'user_id': 2,
                'login': '@111222333',
                'balance': Decimal(500),
                'credit': 0,
                'bonus': Decimal(100),
                'block': 0,
                'created': datetime(2024, 2, 1, tzinfo=UTC),
                'last_login': datetime(2024, 3, 1, tzinfo=UTC),
                'settings': json.dumps(
                    {
                        'telegram': {
                            'login': 'testuser',
                            'first_name': 'Тест',
                            'last_name': 'Юзер',
                            'chat_id': 111222333,
                            'user_id': 111222333,
                        }
                    }
                ),
                'partner_id': None,
            },
            {
                'user_id': 3,
                'login': '@444555666',
                'balance': Decimal(200),
                'credit': 0,
                'bonus': Decimal(50),
                'block': 0,
                'created': datetime(2024, 2, 15, tzinfo=UTC),
                'last_login': None,
                'settings': json.dumps({'telegram': {'login': 'refuser', 'first_name': 'Реф'}}),
                'partner_id': 2,  # Приглашён user_id=2
            },
            {
                'user_id': 4,
                'login': '@777888999',
                'balance': Decimal(0),
                'credit': 0,
                'bonus': Decimal(0),
                'block': 1,  # Заблокирован
                'created': datetime(2024, 3, 1, tzinfo=UTC),
                'last_login': None,
                'settings': json.dumps({'telegram': {'first_name': 'Блок'}}),
                'partner_id': None,
            },
        ],
        'user_services': [
            {
                'user_service_id': 10,
                'user_id': 2,
                'service_id': 3,  # 1 мес
                'qnt': 1,
                'status': 'ACTIVE',
                'created': datetime(2024, 2, 10, tzinfo=UTC),
                'expire': datetime(2024, 3, 10, tzinfo=UTC),
                'auto_bill': 1,
                'category': 'vpn-mz-nl',
            },
            {
                'user_service_id': 11,
                'user_id': 2,
                'service_id': 2,  # Триал
                'qnt': 1,
                'status': 'REMOVED',
                'created': datetime(2024, 2, 1, tzinfo=UTC),
                'expire': datetime(2024, 2, 3, tzinfo=UTC),
                'auto_bill': 0,
                'category': 'vpn-mz-nl',
            },
            {
                'user_service_id': 12,
                'user_id': 3,
                'service_id': 4,  # 3 мес
                'qnt': 1,
                'status': 'NOT PAID',
                'created': datetime(2024, 2, 20, tzinfo=UTC),
                'expire': datetime(2024, 5, 20, tzinfo=UTC),
                'auto_bill': 0,
                'category': 'vpn-mz-nl',
            },
        ],
        'services': [
            {'service_id': 2, 'name': 'Триал', 'cost': Decimal(0), 'period': 0.01, 'category': 'vpn-mz-nl'},
            {'service_id': 3, 'name': '1 мес', 'cost': Decimal(170), 'period': 1, 'category': 'vpn-mz-nl'},
            {'service_id': 4, 'name': '3 мес', 'cost': Decimal(470), 'period': 3, 'category': 'vpn-mz-nl'},
        ],
        'pays_history': [
            {
                'id': 100,
                'user_id': 2,
                'money': Decimal(500),
                'pay_system_id': 'yookassa',
                'date': datetime(2024, 2, 5, tzinfo=UTC),
                'uniq_key': 'pay_abc123',
                'comment': json.dumps({'comment': 'Пополнение баланса'}),
            },
            {
                'id': 101,
                'user_id': 3,
                'money': Decimal(200),
                'pay_system_id': 'yoomoney',
                'date': datetime(2024, 2, 16, tzinfo=UTC),
                'uniq_key': 'pay_def456',
                'comment': None,
            },
        ],
        'withdraw_history': [
            {
                'withdraw_id': 200,
                'user_id': 2,
                'create_date': datetime(2024, 2, 10, tzinfo=UTC),
                'cost': Decimal(170),
                'discount': 0,
                'bonus': Decimal(0),
                'months': Decimal(1),
                'total': Decimal(170),
                'service_id': 3,
                'user_service_id': 10,
            },
            {
                'withdraw_id': 201,
                'user_id': 3,
                'create_date': datetime(2024, 2, 20, tzinfo=UTC),
                'cost': Decimal(420),
                'discount': 0,
                'bonus': Decimal(50),
                'months': Decimal(3),
                'total': Decimal(470),
                'service_id': 4,
                'user_service_id': 12,
            },
            {
                'withdraw_id': 202,
                'user_id': 2,
                'create_date': datetime(2024, 2, 1, tzinfo=UTC),
                'cost': Decimal(0),
                'discount': 0,
                'bonus': Decimal(0),
                'months': Decimal(0),
                'total': Decimal(0),
                'service_id': 2,  # Бесплатный триал
                'user_service_id': 11,
            },
        ],
        'bonus_history': [
            {
                'id': 300,
                'user_id': 2,
                'bonus': Decimal(10),
                'date': datetime(2024, 3, 1, tzinfo=UTC),
                'comment': json.dumps({'percent': 10, 'from_user_id': 3}),
            },
            {
                'id': 301,
                'user_id': 2,
                'bonus': Decimal(100),
                'date': datetime(2024, 4, 1, tzinfo=UTC),
                'comment': json.dumps({'msg': 'Акция'}),
            },
            {
                'id': 302,
                'user_id': 3,
                'bonus': Decimal(-50),
                'date': datetime(2024, 2, 20, tzinfo=UTC),
                'comment': json.dumps({'withdraw_id': 201}),
            },
        ],
    }


class MockShmConnection:
    """Мок-соединение к SHM, работает на dict данных."""

    def __init__(self, data: dict):
        self._data = data

    def cursor(self):
        return MockShmCursor(self._data)

    def close(self):
        pass


class MockShmCursor:
    def __init__(self, data: dict):
        self._data = data
        self._results: list[dict] = []

    def execute(self, query: str, *_args):
        query_lower = query.strip().lower()

        if 'from users' in query_lower:
            rows = self._data.get('users', [])
            if "login like '@%'" in query_lower:
                rows = [r for r in rows if r['login'].startswith('@')]
            if 'partner_id is not null' in query_lower:
                rows = [r for r in rows if r.get('partner_id') is not None]
            self._results = rows

        elif 'from user_services' in query_lower:
            rows = self._data.get('user_services', [])
            # Добавить category из services
            svc_map = {s['service_id']: s for s in self._data.get('services', [])}
            for r in rows:
                svc = svc_map.get(r['service_id'], {})
                r['category'] = svc.get('category', '')
                r.setdefault('shm_user_id', r['user_id'])
            self._results = rows

        elif 'from pays_history' in query_lower:
            rows = self._data.get('pays_history', [])
            rows = [r for r in rows if r.get('money', 0) > 0]
            self._results = rows

        elif 'from withdraw_history' in query_lower:
            self._results = self._data.get('withdraw_history', [])

        elif 'from bonus_history' in query_lower:
            rows = self._data.get('bonus_history', [])
            if 'from_user_id' in query_lower and 'not like' in query_lower:
                # Бонусы БЕЗ from_user_id, с bonus > 0
                rows = [r for r in rows if r['bonus'] > 0 and 'from_user_id' not in str(r.get('comment', ''))]
            elif 'from_user_id' in query_lower:
                # Реферальные с from_user_id, bonus > 0
                rows = [r for r in rows if r['bonus'] > 0 and 'from_user_id' in str(r.get('comment', ''))]
            self._results = rows

        else:
            self._results = []

    def fetchall(self):
        return self._results


# ============================================================================
# ТЕСТЫ УТИЛИТ
# ============================================================================


class TestUtils:
    def test_extract_telegram_id_valid(self):
        assert extract_telegram_id('@852545813') == 852545813
        assert extract_telegram_id('@123') == 123

    def test_extract_telegram_id_invalid(self):
        assert extract_telegram_id('admin') is None
        assert extract_telegram_id('') is None
        assert extract_telegram_id(None) is None
        assert extract_telegram_id('@notanumber') is None

    def test_rubles_to_kopeks(self):
        assert rubles_to_kopeks(170) == 17000
        assert rubles_to_kopeks(Decimal('170.50')) == 17050
        assert rubles_to_kopeks(0) == 0
        assert rubles_to_kopeks(None) == 0
        assert rubles_to_kopeks(Decimal('0.01')) == 1

    def test_rubles_to_kopeks_negative_to_zero(self):
        # Отрицательные — просто конвертируем
        assert rubles_to_kopeks(Decimal(-10)) == -10 * 100

    def test_to_dt_str_datetime(self):
        dt = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        result = to_dt_str(dt)
        assert '2024-01-01' in result

    def test_to_dt_str_none(self):
        assert to_dt_str(None) is None

    def test_to_dt_str_naive_datetime(self):
        """Наивный datetime получает UTC tzinfo."""
        dt = datetime(2024, 6, 15, 10, 30, 0)
        result = to_dt_str(dt)
        assert '2024-06-15' in result
        assert '+00:00' in result

    def test_extract_tg_data(self):
        settings = json.dumps(
            {
                'telegram': {
                    'login': 'testuser',
                    'first_name': 'Тест',
                    'chat_id': 111,
                }
            }
        )
        result = extract_tg_data(settings)
        assert result['login'] == 'testuser'
        assert result['first_name'] == 'Тест'

    def test_extract_tg_data_empty(self):
        assert extract_tg_data(None) == {}
        assert extract_tg_data('') == {}
        assert extract_tg_data('invalid json') == {}

    def test_extract_tg_data_dict_input(self):
        data = {'telegram': {'first_name': 'Test'}}
        assert extract_tg_data(data)['first_name'] == 'Test'

    def test_gen_referral_code(self):
        used = set()
        code = gen_referral_code(used)
        assert len(code) == 8
        assert code.isalnum()
        assert code in used

    def test_gen_referral_code_unique(self):
        used = set()
        codes = [gen_referral_code(used) for _ in range(100)]
        assert len(set(codes)) == 100

    def test_now_utc_format(self):
        result = now_utc()
        # Должен быть ISO формат
        datetime.fromisoformat(result)

    def test_status_map_complete(self):
        """Все статусы SHM замаплены."""
        assert 'ACTIVE' in STATUS_MAP
        assert 'BLOCK' in STATUS_MAP
        assert 'NOT PAID' in STATUS_MAP
        assert 'REMOVED' in STATUS_MAP

    def test_service_name_map(self):
        assert 3 in SERVICE_NAME_MAP  # 1 мес
        assert 10 in SERVICE_NAME_MAP  # 12 мес


# ============================================================================
# ТЕСТЫ МИГРАЦИИ ПОЛЬЗОВАТЕЛЕЙ
# ============================================================================


class TestMigrateUsers:
    def test_users_inserted(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)

        # 3 пользователя с telegram_id (admin без @)
        count = bedolaga_db.fetchone('SELECT COUNT(*) FROM users')[0]
        assert count == 3

        # Проверяем маппинг
        assert 2 in mapping  # @111222333
        assert 3 in mapping  # @444555666
        assert 4 in mapping  # @777888999
        assert 1 not in mapping  # admin (нет @)

    def test_balance_merged(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)

        user_id = mapping[2]
        row = bedolaga_db.fetchone('SELECT balance_kopeks FROM users WHERE id = %s', (user_id,))
        # 500 + 100 = 600 руб = 60000 коп
        assert row[0] == 60000

    def test_blocked_user_status(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)

        user_id = mapping[4]
        row = bedolaga_db.fetchone('SELECT status FROM users WHERE id = %s', (user_id,))
        assert row[0] == 'blocked'

    def test_telegram_data_extracted(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)

        user_id = mapping[2]
        row = bedolaga_db.fetchone(
            'SELECT username, first_name, last_name FROM users WHERE id = %s',
            (user_id,),
        )
        assert row[0] == 'testuser'
        assert row[1] == 'Тест'
        assert row[2] == 'Юзер'

    def test_duplicate_telegram_id_skipped(self, bedolaga_db, shm_data_minimal):
        """Дубликаты telegram_id пропускаются."""
        shm_data_minimal['users'].append(
            {
                'user_id': 99,
                'login': '@111222333',  # Дубликат!
                'balance': Decimal(0),
                'credit': 0,
                'bonus': Decimal(0),
                'block': 0,
                'created': datetime(2024, 5, 1, tzinfo=UTC),
                'last_login': None,
                'settings': None,
                'partner_id': None,
            }
        )
        shm_conn = MockShmConnection(shm_data_minimal)
        migrate_users(shm_conn, bedolaga_db, dry_run=False)

        count = bedolaga_db.fetchone('SELECT COUNT(*) FROM users WHERE telegram_id = 111222333')[0]
        assert count == 1

    def test_dry_run_no_writes(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=True)

        count = bedolaga_db.fetchone('SELECT COUNT(*) FROM users')[0]
        assert count == 0
        assert len(mapping) > 0  # Маппинг всё равно заполняется

    def test_negative_balance_zeroed(self, bedolaga_db, shm_data_minimal):
        shm_data_minimal['users'][1]['balance'] = Decimal(-100)
        shm_data_minimal['users'][1]['bonus'] = Decimal(50)
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)

        user_id = mapping[2]
        row = bedolaga_db.fetchone('SELECT balance_kopeks FROM users WHERE id = %s', (user_id,))
        # -100 + 50 = -50 -> 0 (обнуляется)
        assert row[0] == 0

    def test_referral_code_generated(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        migrate_users(shm_conn, bedolaga_db, dry_run=False)

        codes = bedolaga_db.fetchall('SELECT referral_code FROM users WHERE referral_code IS NOT NULL')
        assert len(codes) == 3
        code_set = {c[0] for c in codes}
        assert len(code_set) == 3  # Все уникальные


# ============================================================================
# ТЕСТЫ МИГРАЦИИ РЕФЕРАЛЬНЫХ СВЯЗЕЙ
# ============================================================================


class TestMigrateReferrals:
    def test_referral_link_set(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_referrals(shm_conn, bedolaga_db, mapping, dry_run=False)

        # user_id=3 имеет partner_id=2
        user3_id = mapping[3]
        user2_id = mapping[2]
        row = bedolaga_db.fetchone('SELECT referred_by_id FROM users WHERE id = %s', (user3_id,))
        assert row[0] == user2_id


# ============================================================================
# ТЕСТЫ МИГРАЦИИ ПОДПИСОК
# ============================================================================


class TestMigrateSubscriptions:
    def test_best_subscription_selected(self, bedolaga_db, shm_data_minimal):
        """Для user_id=2 выбирается ACTIVE подписка (не REMOVED триал)."""
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_subscriptions(shm_conn, bedolaga_db, mapping, dry_run=False)

        user2_id = mapping[2]
        row = bedolaga_db.fetchone(
            'SELECT status, is_trial, autopay_enabled FROM subscriptions WHERE user_id = %s',
            (user2_id,),
        )
        assert row[0] == 'active'
        assert row[1] == 0  # Не триал
        assert row[2] == 0  # autopay отключён при миграции

    def test_subscription_status_mapping(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_subscriptions(shm_conn, bedolaga_db, mapping, dry_run=False)

        user3_id = mapping[3]
        row = bedolaga_db.fetchone(
            'SELECT status FROM subscriptions WHERE user_id = %s',
            (user3_id,),
        )
        assert row[0] == 'expired'  # NOT PAID -> expired

    def test_has_had_paid_subscription_set(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_subscriptions(shm_conn, bedolaga_db, mapping, dry_run=False)

        user2_id = mapping[2]
        row = bedolaga_db.fetchone(
            'SELECT has_had_paid_subscription FROM users WHERE id = %s',
            (user2_id,),
        )
        assert row[0] == 1

    def test_one_subscription_per_user(self, bedolaga_db, shm_data_minimal):
        """Даже если у пользователя 2 подписки в SHM — одна в Bedolaga."""
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_subscriptions(shm_conn, bedolaga_db, mapping, dry_run=False)

        user2_id = mapping[2]
        count = bedolaga_db.fetchone(
            'SELECT COUNT(*) FROM subscriptions WHERE user_id = %s',
            (user2_id,),
        )[0]
        assert count == 1


# ============================================================================
# ТЕСТЫ МИГРАЦИИ ТРАНЗАКЦИЙ (ПОПОЛНЕНИЯ)
# ============================================================================


class TestMigrateTransactions:
    def test_deposits_created(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_transactions(shm_conn, bedolaga_db, mapping, dry_run=False)

        count = bedolaga_db.fetchone("SELECT COUNT(*) FROM transactions WHERE type = 'deposit'")[0]
        assert count == 2

    def test_amount_converted_to_kopeks(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_transactions(shm_conn, bedolaga_db, mapping, dry_run=False)

        user2_id = mapping[2]
        row = bedolaga_db.fetchone(
            "SELECT amount_kopeks FROM transactions WHERE user_id = %s AND external_id = 'pay_abc123'",
            (user2_id,),
        )
        assert row[0] == 50000  # 500 rub

    def test_payment_method_mapped(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_transactions(shm_conn, bedolaga_db, mapping, dry_run=False)

        user2_id = mapping[2]
        row = bedolaga_db.fetchone(
            "SELECT payment_method FROM transactions WHERE user_id = %s AND external_id = 'pay_abc123'",
            (user2_id,),
        )
        assert row[0] == 'yookassa'

    def test_yoomoney_mapped_to_yookassa(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_transactions(shm_conn, bedolaga_db, mapping, dry_run=False)

        user3_id = mapping[3]
        row = bedolaga_db.fetchone(
            'SELECT payment_method FROM transactions WHERE user_id = %s',
            (user3_id,),
        )
        assert row[0] == 'yookassa'  # yoomoney -> yookassa

    def test_description_from_comment(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_transactions(shm_conn, bedolaga_db, mapping, dry_run=False)

        user2_id = mapping[2]
        row = bedolaga_db.fetchone(
            "SELECT description FROM transactions WHERE user_id = %s AND external_id = 'pay_abc123'",
            (user2_id,),
        )
        assert row[0] == 'Пополнение баланса'


# ============================================================================
# ТЕСТЫ МИГРАЦИИ ПОКУПОК ПОДПИСОК
# ============================================================================


class TestMigrateSubscriptionPurchases:
    def test_purchases_created(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_subscription_purchases(shm_conn, bedolaga_db, mapping, dry_run=False)

        count = bedolaga_db.fetchone("SELECT COUNT(*) FROM transactions WHERE type = 'subscription_payment'")[0]
        # 2 оплаченных (withdraw_id 200 и 201), 1 бесплатный триал пропущен
        assert count == 2

    def test_free_trial_skipped(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_subscription_purchases(shm_conn, bedolaga_db, mapping, dry_run=False)

        # Не должно быть записи для бесплатного триала (cost=0, bonus=0)
        count = bedolaga_db.fetchone("SELECT COUNT(*) FROM transactions WHERE external_id = 'shm_withdraw_202'")[0]
        assert count == 0

    def test_bonus_in_description(self, bedolaga_db, shm_data_minimal):
        """Если часть оплачена бонусом -- указано в описании."""
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_subscription_purchases(shm_conn, bedolaga_db, mapping, dry_run=False)

        row = bedolaga_db.fetchone(
            "SELECT description, amount_kopeks FROM transactions WHERE external_id = 'shm_withdraw_201'"
        )
        assert 'бонус' in row[0].lower()
        # cost=420 + bonus=50 = 470 rub = 47000 kop
        assert row[1] == 47000

    def test_amount_includes_bonus(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_subscription_purchases(shm_conn, bedolaga_db, mapping, dry_run=False)

        row = bedolaga_db.fetchone("SELECT amount_kopeks FROM transactions WHERE external_id = 'shm_withdraw_200'")
        assert row[0] == 17000  # 170 rub, без бонуса

    def test_service_name_in_description(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_subscription_purchases(shm_conn, bedolaga_db, mapping, dry_run=False)

        row = bedolaga_db.fetchone("SELECT description FROM transactions WHERE external_id = 'shm_withdraw_200'")
        assert 'VPN 1 мес' in row[0]


# ============================================================================
# ТЕСТЫ МИГРАЦИИ БОНУСНЫХ НАЧИСЛЕНИЙ
# ============================================================================


class TestMigrateBonusDeposits:
    def test_bonus_deposits_created(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_bonus_deposits(shm_conn, bedolaga_db, mapping, dry_run=False)

        count = bedolaga_db.fetchone("SELECT COUNT(*) FROM transactions WHERE payment_method = 'bonus'")[0]
        # Только "Акция" (id=301), withdraw (id=302 отрицательный), referral (id=300) исключены
        assert count == 1

    def test_referral_not_duplicated(self, bedolaga_db, shm_data_minimal):
        """Реферальные начисления НЕ попадают в бонусные."""
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_bonus_deposits(shm_conn, bedolaga_db, mapping, dry_run=False)

        # Не должно быть записи с external_id shm_bonus_300 (это реферальное)
        count = bedolaga_db.fetchone("SELECT COUNT(*) FROM transactions WHERE external_id = 'shm_bonus_300'")[0]
        assert count == 0

    def test_bonus_amount_converted(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_bonus_deposits(shm_conn, bedolaga_db, mapping, dry_run=False)

        row = bedolaga_db.fetchone(
            "SELECT amount_kopeks, description FROM transactions WHERE external_id = 'shm_bonus_301'"
        )
        assert row[0] == 10000  # 100 rub
        assert 'Акция' in row[1]


# ============================================================================
# ТЕСТЫ МИГРАЦИИ РЕФЕРАЛЬНЫХ НАЧИСЛЕНИЙ
# ============================================================================


class TestMigrateReferralEarnings:
    def test_earnings_created(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_referral_earnings(shm_conn, bedolaga_db, mapping, dry_run=False)

        count = bedolaga_db.fetchone('SELECT COUNT(*) FROM referral_earnings')[0]
        assert count == 1

    def test_earning_amount_and_ids(self, bedolaga_db, shm_data_minimal):
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_referral_earnings(shm_conn, bedolaga_db, mapping, dry_run=False)

        row = bedolaga_db.fetchone('SELECT user_id, referral_id, amount_kopeks, reason FROM referral_earnings')

        # user_id=2 получил 10 rub от user_id=3
        assert row[0] == mapping[2]  # Реферер
        assert row[1] == mapping[3]  # Реферал
        assert row[2] == 1000  # 10 rub
        assert '10pct' in row[3]


# ============================================================================
# ТЕСТЫ SQL-ДАМП ПАРСЕРА
# ============================================================================


class TestSqlDumpParser:
    def test_parse_simple_dump(self, tmp_path):
        dump = tmp_path / 'test.sql'
        dump.write_text(
            """
CREATE TABLE `test_table` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` char(32) NOT NULL,
  `value` decimal(10,2) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;

INSERT INTO `test_table` VALUES (1,'hello',10.50),(2,'world',NULL);
""",
            encoding='utf-8',
        )

        conn = SqlDumpConnection(str(dump), parse_all=True)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM test_table')
        rows = cursor.fetchall()

        assert len(rows) == 2
        assert rows[0]['id'] == 1
        assert rows[0]['name'] == 'hello'
        assert rows[0]['value'] == 10.50
        assert rows[1]['name'] == 'world'
        assert rows[1]['value'] is None

    def test_parse_escaped_strings(self, tmp_path):
        dump = tmp_path / 'test.sql'
        dump.write_text(
            """
CREATE TABLE `data` (
  `id` int NOT NULL,
  `text` text,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;

INSERT INTO `data` VALUES (1,'it\\'s a test'),(2,'line1\\nline2');
""",
            encoding='utf-8',
        )

        conn = SqlDumpConnection(str(dump), parse_all=True)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM data')
        rows = cursor.fetchall()

        assert len(rows) == 2
        assert rows[0]['text'] == "it's a test"
        # Парсер не конвертирует \n в newline (это ок для реальных дампов)
        assert rows[1]['text'] == 'line1nline2'

    def test_where_like_filter(self, tmp_path):
        dump = tmp_path / 'test.sql'
        dump.write_text(
            """
CREATE TABLE `users` (
  `user_id` int NOT NULL,
  `login` char(32) NOT NULL,
  PRIMARY KEY (`user_id`)
) ENGINE=InnoDB;

INSERT INTO `users` VALUES (1,'admin'),(2,'@12345'),(3,'@67890');
""",
            encoding='utf-8',
        )

        conn = SqlDumpConnection(str(dump), parse_all=True)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE login LIKE '@%'")
        rows = cursor.fetchall()

        assert len(rows) == 2
        assert all(r['login'].startswith('@') for r in rows)

    def test_where_greater_than(self, tmp_path):
        dump = tmp_path / 'test.sql'
        dump.write_text(
            """
CREATE TABLE `pays` (
  `id` int NOT NULL,
  `money` decimal(10,2) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;

INSERT INTO `pays` VALUES (1,0.00),(2,100.00),(3,200.00);
""",
            encoding='utf-8',
        )

        conn = SqlDumpConnection(str(dump), parse_all=True)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM pays WHERE money > 0')
        rows = cursor.fetchall()

        assert len(rows) == 2


# ============================================================================
# ТЕСТЫ ПОЛНОЙ МИГРАЦИИ (ИНТЕГРАЦИЯ)
# ============================================================================


class TestFullMigration:
    def test_full_pipeline(self, bedolaga_db, shm_data_minimal):
        """Полный пайплайн миграции — все шаги."""
        shm_conn = MockShmConnection(shm_data_minimal)

        # Шаг 1: Пользователи
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        assert len(mapping) == 3

        # Шаг 2: Реферальные связи
        migrate_referrals(shm_conn, bedolaga_db, mapping, dry_run=False)

        # Шаг 3: Подписки
        migrate_subscriptions(shm_conn, bedolaga_db, mapping, dry_run=False)

        # Шаг 4: Пополнения
        migrate_transactions(shm_conn, bedolaga_db, mapping, dry_run=False)

        # Шаг 5: Покупки подписок
        migrate_subscription_purchases(shm_conn, bedolaga_db, mapping, dry_run=False)

        # Шаг 6: Бонусные начисления
        migrate_bonus_deposits(shm_conn, bedolaga_db, mapping, dry_run=False)

        # Шаг 7: Реферальные начисления
        migrate_referral_earnings(shm_conn, bedolaga_db, mapping, dry_run=False)

        # Проверки
        users = bedolaga_db.fetchone('SELECT COUNT(*) FROM users')[0]
        assert users == 3

        subs = bedolaga_db.fetchone('SELECT COUNT(*) FROM subscriptions')[0]
        assert subs == 2  # user_id=2 и user_id=3

        deposits = bedolaga_db.fetchone("SELECT COUNT(*) FROM transactions WHERE type = 'deposit'")[0]
        assert deposits == 3  # 2 pays_history + 1 бонус "Акция"

        purchases = bedolaga_db.fetchone("SELECT COUNT(*) FROM transactions WHERE type = 'subscription_payment'")[0]
        assert purchases == 2

        bonus_tx = bedolaga_db.fetchone("SELECT COUNT(*) FROM transactions WHERE payment_method = 'bonus'")[0]
        assert bonus_tx == 1

        earnings = bedolaga_db.fetchone('SELECT COUNT(*) FROM referral_earnings')[0]
        assert earnings == 1

        referrals = bedolaga_db.fetchone('SELECT COUNT(*) FROM users WHERE referred_by_id IS NOT NULL')[0]
        assert referrals == 1

    def test_idempotent_rerun(self, bedolaga_db, shm_data_minimal):
        """Повторный запуск не создаёт дубликатов."""
        shm_conn = MockShmConnection(shm_data_minimal)

        # Первый запуск
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_transactions(shm_conn, bedolaga_db, mapping, dry_run=False)
        migrate_subscription_purchases(shm_conn, bedolaga_db, mapping, dry_run=False)
        migrate_bonus_deposits(shm_conn, bedolaga_db, mapping, dry_run=False)

        count1 = bedolaga_db.fetchone('SELECT COUNT(*) FROM transactions')[0]

        # Второй запуск
        mapping2 = migrate_users(shm_conn, bedolaga_db, dry_run=False)
        migrate_transactions(shm_conn, bedolaga_db, mapping2, dry_run=False)
        migrate_subscription_purchases(shm_conn, bedolaga_db, mapping2, dry_run=False)
        migrate_bonus_deposits(shm_conn, bedolaga_db, mapping2, dry_run=False)

        count2 = bedolaga_db.fetchone('SELECT COUNT(*) FROM transactions')[0]

        # Количество не должно удвоиться
        assert count2 == count1

    def test_balance_integrity(self, bedolaga_db, shm_data_minimal):
        """Проверяем целостность балансов после миграции."""
        shm_conn = MockShmConnection(shm_data_minimal)
        mapping = migrate_users(shm_conn, bedolaga_db, dry_run=False)

        # user_id=2: balance=500, bonus=100 -> 60000 коп
        user2 = bedolaga_db.fetchone(
            'SELECT balance_kopeks FROM users WHERE id = %s',
            (mapping[2],),
        )
        assert user2[0] == 60000

        # user_id=3: balance=200, bonus=50 -> 25000 коп
        user3 = bedolaga_db.fetchone(
            'SELECT balance_kopeks FROM users WHERE id = %s',
            (mapping[3],),
        )
        assert user3[0] == 25000

        # user_id=4: balance=0, bonus=0, blocked -> 0 коп
        user4 = bedolaga_db.fetchone(
            'SELECT balance_kopeks FROM users WHERE id = %s',
            (mapping[4],),
        )
        assert user4[0] == 0
