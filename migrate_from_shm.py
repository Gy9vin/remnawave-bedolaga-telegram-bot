# ruff: noqa: PLC0415
"""
Миграция SHM (MySQL) -> Remnawave Bedolaga Bot (PostgreSQL / SQLite)

Что переносим:
  - Пользователей (баланс = balance + bonus, рубли -> копейки)
  - Реферальные связи (partner_id -> referred_by_id)
  - Подписки (лучшая на пользователя: ACTIVE > BLOCK > NOT PAID > REMOVED)
  - Историю пополнений (pays_history -> transactions DEPOSIT)
  - Реферальные начисления (bonus_history -> referral_earnings)

Запуск:
  # PostgreSQL (продакшен):
  python migrate_from_shm.py --from-dump backup.sql --pg "postgresql://user:pass@localhost:5432/dbname"

  # SQLite (тесты):
  python migrate_from_shm.py --from-dump backup.sql --sqlite-path ./test.db

  # Dry-run:
  python migrate_from_shm.py --from-dump backup.sql --pg "postgresql://..." --dry-run
"""

from __future__ import annotations

import abc
import argparse
import json
import random
import re
import sqlite3
import string
import sys
from datetime import UTC, datetime
from decimal import Decimal


try:
    import pymysql
    import pymysql.cursors
except ImportError:
    pymysql = None  # type: ignore[assignment]


# ============================================================================
# КОНФИГУРАЦИЯ
# ============================================================================


def get_mysql_config():
    """Получить конфиг MySQL (pymysql нужен только при прямом подключении)."""
    return {
        'host': '127.0.0.1',
        'port': 3307,
        'user': 'root',
        'password': 'root',
        'database': 'shm',
        'charset': 'utf8mb4',
        'cursorclass': pymysql.cursors.DictCursor,
    }


SQLITE_PATH = './data/bot.db'

# SHM service_id -> traffic_limit_gb (0 = не ограничен / по умолчанию)
SERVICE_TRAFFIC_MAP = {
    6: 100,  # Ultra 1 мес (100 ГБ)
    7: 100,  # Ultra 3 мес (100 ГБ)
    8: 100,  # Ultra 6 мес (100 ГБ)
    9: 300,  # Ultra 12 мес (300 ГБ)
}

# SHM service_id -> название для описания транзакций
SERVICE_NAME_MAP = {
    2: 'Бесплатный пробный период',
    3: 'VPN 1 мес',
    4: 'VPN 3 мес',
    5: 'VPN 6 мес',
    6: 'Ultra 1 мес (100 ГБ)',
    7: 'Ultra 3 мес (100 ГБ)',
    8: 'Ultra 6 мес (100 ГБ)',
    9: 'Ultra 1 мес (300 ГБ)',
    10: 'VPN 12 мес',
    11: 'Подарок 1 мес',
    12: 'Промо 1 мес',
    13: 'Промо 3 мес',
    14: 'Промо 6 мес',
    15: 'Промо 12 мес',
    16: 'Подарок 3 мес',
    17: 'Подарок 6 мес',
    18: 'Подарок 12 мес',
}

# service_id которые являются триалом
TRIAL_SERVICE_IDS = {2}

# Маппинг статусов
STATUS_MAP = {
    'ACTIVE': 'active',
    'BLOCK': 'disabled',
    'NOT PAID': 'expired',
    'REMOVED': 'expired',
}

# ID тарифа в Bedolaga для всех мигрированных подписок
MIGRATION_TARIFF_ID = 2  # "Стандарт"

# Маппинг платёжных систем
PAYMENT_METHOD_MAP = {
    'yoomoney': 'yookassa',
    'yoomoney-test': 'yookassa',
    'yookassa': 'yookassa',
    'manual': 'manual',
}


# ============================================================================
# УТИЛИТЫ
# ============================================================================


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def to_dt_str(dt) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.isoformat()
    return str(dt)


def rubles_to_kopeks(amount) -> int:
    if amount is None:
        return 0
    return int(Decimal(str(amount)) * 100)


def extract_telegram_id(login: str) -> int | None:
    """@852545813 -> 852545813"""
    if not login or not login.startswith('@'):
        return None
    try:
        return int(login[1:])
    except ValueError:
        return None


def extract_tg_data(settings_json) -> dict:
    """Извлечь Telegram-данные из SHM settings JSON."""
    if not settings_json:
        return {}
    try:
        if isinstance(settings_json, str):
            data = json.loads(settings_json)
        else:
            data = settings_json
        return data.get('telegram', {})
    except Exception:
        return {}


def gen_referral_code(used_codes: set) -> str:
    """Генерировать уникальный 8-символьный код."""
    chars = string.ascii_uppercase + string.digits
    for _ in range(100):
        code = ''.join(random.choices(chars, k=8))
        if code not in used_codes:
            used_codes.add(code)
            return code
    raise RuntimeError('Не удалось сгенерировать уникальный код')


def log(msg: str):
    print(msg, flush=True)


# ============================================================================
# АБСТРАКТНЫЙ АДАПТЕР ЦЕЛЕВОЙ БД
# ============================================================================


class DbAdapter(abc.ABC):
    """Абстрактный адаптер для целевой БД (PostgreSQL или SQLite)."""

    @abc.abstractmethod
    def execute(self, query: str, params: tuple | None = None):
        """Выполнить запрос и вернуть курсор."""

    @abc.abstractmethod
    def fetchone(self, query: str, params: tuple | None = None) -> tuple | None:
        """Выполнить запрос и вернуть одну строку."""

    @abc.abstractmethod
    def fetchall(self, query: str, params: tuple | None = None) -> list:
        """Выполнить запрос и вернуть все строки."""

    @abc.abstractmethod
    def commit(self):
        """Зафиксировать транзакцию."""

    @abc.abstractmethod
    def close(self):
        """Закрыть соединение."""

    @abc.abstractmethod
    def insert_returning_id(self, query: str, params: tuple | None = None) -> int:
        """INSERT и вернуть id вставленной строки."""

    @abc.abstractmethod
    def insert_on_conflict_ignore(self, query: str, params: tuple | None = None) -> int:
        """INSERT ON CONFLICT DO NOTHING, вернуть количество затронутых строк."""

    @abc.abstractmethod
    def table_exists(self, table_name: str) -> bool:
        """Проверить что таблица существует."""


class SqliteAdapter(DbAdapter):
    """Адаптер для SQLite (для тестов)."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    @property
    def raw_connection(self) -> sqlite3.Connection:
        return self._conn

    def _convert_query(self, query: str) -> str:
        """Конвертировать %s плейсхолдеры в ? для SQLite."""
        return query.replace('%s', '?')

    def _convert_conflict(self, query: str) -> str:
        """Конвертировать INSERT ... ON CONFLICT DO NOTHING -> INSERT OR IGNORE."""
        # Убрать ON CONFLICT ... DO NOTHING и добавить OR IGNORE
        pattern = r'INSERT\s+INTO'
        replacement = 'INSERT OR IGNORE INTO'
        converted = re.sub(pattern, replacement, query, count=1, flags=re.IGNORECASE)
        # Убрать ON CONFLICT (...) DO NOTHING
        converted = re.sub(r'\s*ON\s+CONFLICT\s*\([^)]*\)\s*DO\s+NOTHING', '', converted, flags=re.IGNORECASE)
        return converted

    def execute(self, query: str, params: tuple | None = None):
        q = self._convert_query(query)
        if params:
            return self._conn.execute(q, params)
        return self._conn.execute(q)

    def fetchone(self, query: str, params: tuple | None = None) -> tuple | None:
        q = self._convert_query(query)
        if params:
            return self._conn.execute(q, params).fetchone()
        return self._conn.execute(q).fetchone()

    def fetchall(self, query: str, params: tuple | None = None) -> list:
        q = self._convert_query(query)
        if params:
            return self._conn.execute(q, params).fetchall()
        return self._conn.execute(q).fetchall()

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def insert_returning_id(self, query: str, params: tuple | None = None) -> int:
        q = self._convert_query(query)
        if params:
            self._conn.execute(q, params)
        else:
            self._conn.execute(q)
        return self._conn.execute('SELECT last_insert_rowid()').fetchone()[0]

    def insert_on_conflict_ignore(self, query: str, params: tuple | None = None) -> int:
        q = self._convert_conflict(self._convert_query(query))
        cur = self._conn.execute(q, params) if params else self._conn.execute(q)
        return cur.rowcount

    def table_exists(self, table_name: str) -> bool:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row[0] > 0


class PgAdapter(DbAdapter):
    """Адаптер для PostgreSQL (продакшен)."""

    def __init__(self, dsn: str):
        import psycopg2

        self._conn = psycopg2.connect(dsn)
        self._conn.autocommit = False
        self._savepoint_counter = 0

    def execute(self, query: str, params: tuple | None = None):
        """Execute с SAVEPOINT для write-запросов."""
        q_upper = query.strip().upper()
        is_write = q_upper.startswith(('INSERT', 'UPDATE', 'DELETE'))

        cur = self._conn.cursor()
        if is_write:
            self._savepoint_counter += 1
            sp = f'sp_{self._savepoint_counter}'
            cur.execute(f'SAVEPOINT {sp}')
            try:
                cur.execute(query, params)
                cur.execute(f'RELEASE SAVEPOINT {sp}')
                return cur
            except Exception:
                cur.execute(f'ROLLBACK TO SAVEPOINT {sp}')
                raise
        else:
            cur.execute(query, params)
            return cur

    def fetchone(self, query: str, params: tuple | None = None) -> tuple | None:
        cur = self._conn.cursor()
        cur.execute(query, params)
        return cur.fetchone()

    def fetchall(self, query: str, params: tuple | None = None) -> list:
        cur = self._conn.cursor()
        cur.execute(query, params)
        return cur.fetchall()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def insert_returning_id(self, query: str, params: tuple | None = None) -> int:
        # Добавить RETURNING id если ещё нет
        q = query.rstrip().rstrip(';')
        if 'RETURNING' not in q.upper():
            q += ' RETURNING id'
        self._savepoint_counter += 1
        sp = f'sp_{self._savepoint_counter}'
        cur = self._conn.cursor()
        cur.execute(f'SAVEPOINT {sp}')
        try:
            cur.execute(q, params)
            row = cur.fetchone()
            cur.execute(f'RELEASE SAVEPOINT {sp}')
            return row[0]
        except Exception:
            cur.execute(f'ROLLBACK TO SAVEPOINT {sp}')
            raise

    def insert_on_conflict_ignore(self, query: str, params: tuple | None = None) -> int:
        self._savepoint_counter += 1
        sp = f'sp_{self._savepoint_counter}'
        cur = self._conn.cursor()
        cur.execute(f'SAVEPOINT {sp}')
        try:
            cur.execute(query, params)
            rc = cur.rowcount
            cur.execute(f'RELEASE SAVEPOINT {sp}')
            return rc
        except Exception:
            cur.execute(f'ROLLBACK TO SAVEPOINT {sp}')
            return 0

    def safe_execute(self, query: str, params: tuple | None = None) -> bool:
        """Выполнить запрос с SAVEPOINT — при ошибке откатить только этот запрос."""
        self._savepoint_counter += 1
        sp = f'sp_{self._savepoint_counter}'
        cur = self._conn.cursor()
        cur.execute(f'SAVEPOINT {sp}')
        try:
            cur.execute(query, params)
            cur.execute(f'RELEASE SAVEPOINT {sp}')
            return True
        except Exception:
            cur.execute(f'ROLLBACK TO SAVEPOINT {sp}')
            return False

    def table_exists(self, table_name: str) -> bool:
        cur = self._conn.cursor()
        cur.execute(
            'SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name = %s)',
            (table_name,),
        )
        return cur.fetchone()[0]


# ============================================================================
# ПАРСЕР SQL-ДАМПА (для --from-dump режима)
# ============================================================================


class SqlDumpConnection:
    """Эмулирует pymysql connection/cursor через парсинг SQL-дампа."""

    def __init__(self, dump_path: str, *, parse_all: bool = False):
        self.dump_path = dump_path
        self._tables: dict[str, list[dict]] = {}
        self._columns: dict[str, list[str]] = {}
        self._parse_all = parse_all
        self._parse_dump()

    # Таблицы нужные для миграции — остальные пропускаем (экономия памяти)
    NEEDED_TABLES = {
        'users',
        'user_services',
        'services',
        'pays_history',
        'withdraw_history',
        'bonus_history',
    }

    def _parse_dump(self):
        log(f'  📖 Парсинг SQL-дампа: {self.dump_path}')

        # Построчный парсинг — не грузим весь файл в память
        create_buffer = []
        create_table_name = None
        in_create = False

        with open(self.dump_path, 'rb') as f:
            for raw_line in f:
                line = raw_line.decode('utf-8', errors='replace')

                # CREATE TABLE — может быть многострочным
                cm = re.match(r'CREATE TABLE `(\w+)` \(', line)
                if cm:
                    create_table_name = cm.group(1)
                    in_create = True
                    create_buffer = [line]
                    continue

                if in_create:
                    create_buffer.append(line)
                    if ') ENGINE' in line:
                        in_create = False
                        if self._parse_all or create_table_name in self.NEEDED_TABLES:
                            self._parse_create_table(create_table_name, ''.join(create_buffer))
                        create_buffer = []
                    continue

                # INSERT INTO — всегда однострочный в mysqldump
                im = re.match(r'INSERT INTO `(\w+)` VALUES ', line)
                if im:
                    table_name = im.group(1)
                    if not self._parse_all and table_name not in self.NEEDED_TABLES:
                        continue
                    if table_name not in self._columns:
                        continue

                    # Извлечь VALUES часть (после "INSERT INTO `table` VALUES ")
                    values_start = line.index('VALUES ') + 7
                    values_str = line[values_start:].rstrip().rstrip(';')

                    columns = self._columns[table_name]
                    rows = self._parse_values(values_str, columns)
                    if table_name not in self._tables:
                        self._tables[table_name] = []
                    self._tables[table_name].extend(rows)

        tables_info = ', '.join(f'{t}({len(r)})' for t, r in sorted(self._tables.items()))
        log(f'  ✅ Загружено: {tables_info}')

    def _parse_create_table(self, table_name: str, create_sql: str):
        """Извлечь колонки из CREATE TABLE."""
        columns = []
        for line in create_sql.split('\n'):
            line = line.strip()
            cm = re.match(r'`(\w+)`', line)
            if cm:
                columns.append(cm.group(1))
        self._columns[table_name] = columns

    def _parse_values(self, values_str: str, columns: list[str]) -> list[dict]:
        """Парсить VALUES (...),(...),... в список dict."""
        rows = []
        i = 0
        n = len(values_str)

        while i < n:
            # Найти начало записи '('
            if values_str[i] != '(':
                i += 1
                continue

            # Парсить одну запись
            i += 1  # пропустить '('
            fields = []
            while i < n:
                # Пропустить пробелы
                while i < n and values_str[i] == ' ':
                    i += 1

                if i >= n:
                    break

                if values_str[i] == ')':
                    i += 1  # пропустить ')'
                    break

                if values_str[i] == ',':
                    i += 1  # пропустить ','
                    continue

                if values_str[i] == "'" or (i + 1 < n and values_str[i] == '\\' and values_str[i + 1] == "'"):
                    # Строковое значение
                    i += 1  # пропустить открывающую кавычку
                    val_parts = []
                    while i < n:
                        if values_str[i] == '\\' and i + 1 < n:
                            val_parts.append(values_str[i + 1])
                            i += 2
                        elif values_str[i] == "'":
                            # Проверить на ''
                            if i + 1 < n and values_str[i + 1] == "'":
                                val_parts.append("'")
                                i += 2
                            else:
                                i += 1  # пропустить закрывающую кавычку
                                break
                        else:
                            val_parts.append(values_str[i])
                            i += 1
                    fields.append(''.join(val_parts))
                elif values_str[i : i + 4] == 'NULL':
                    fields.append(None)
                    i += 4
                else:
                    # Числовое значение
                    j = i
                    while i < n and values_str[i] not in (',', ')'):
                        i += 1
                    raw = values_str[j:i].strip()
                    # Конвертировать в число
                    try:
                        if '.' in raw:
                            fields.append(float(raw))
                        else:
                            fields.append(int(raw))
                    except ValueError:
                        fields.append(raw)

            if fields:
                row = {}
                for idx, col in enumerate(columns):
                    row[col] = fields[idx] if idx < len(fields) else None
                rows.append(row)

        return rows

    def cursor(self):
        return SqlDumpCursor(self._tables)

    def close(self):
        pass


class SqlDumpCursor:
    """Эмулирует pymysql cursor с поддержкой простых SELECT запросов."""

    def __init__(self, tables: dict[str, list[dict]]):
        self._tables = tables
        self._results: list[dict] = []

    def execute(self, query: str, *_args):
        """Поддерживает базовые SELECT FROM table WHERE ... ORDER BY."""
        query = query.strip()

        # Определить таблицу и алиасы
        from_match = re.search(r'FROM\s+(\w+)(?:\s+(\w+))?', query, re.IGNORECASE)
        if not from_match:
            self._results = []
            return

        table = from_match.group(1)
        rows = list(self._tables.get(table, []))

        # JOIN — для user_services JOIN services
        join_match = re.search(
            r'JOIN\s+(\w+)\s+(\w+)\s+ON\s+(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)',
            query,
            re.IGNORECASE,
        )
        if join_match:
            join_table = join_match.group(1)
            join_rows = self._tables.get(join_table, [])
            join_key_right = join_match.group(6)
            join_key_left = join_match.group(4)

            # Построить индекс
            join_index = {}
            for jr in join_rows:
                key = jr.get(join_key_right)
                if key is not None:
                    join_index[key] = jr

            joined = []
            for r in rows:
                left_key = r.get(join_key_left)
                if left_key in join_index:
                    merged = {**r, **join_index[left_key]}
                    joined.append(merged)
            rows = joined

        # WHERE
        where_match = re.search(r'WHERE\s+(.+?)(?:ORDER|GROUP|LIMIT|$)', query, re.IGNORECASE | re.DOTALL)
        if where_match:
            where_clause = where_match.group(1).strip()
            rows = self._apply_where(rows, where_clause)

        # ORDER BY
        order_match = re.search(r'ORDER BY\s+(.+?)(?:LIMIT|$)', query, re.IGNORECASE)
        if order_match:
            order_clause = order_match.group(1).strip()
            rows = self._apply_order(rows, order_clause)

        # Алиасы в SELECT — обработать "us.user_id as shm_user_id"
        alias_matches = re.findall(r'(\w+)\.(\w+)\s+as\s+(\w+)', query, re.IGNORECASE)
        if alias_matches:
            for r in rows:
                for _alias_table, col, alias in alias_matches:
                    if col in r:
                        r[alias] = r[col]

        self._results = rows

    def _apply_where(self, rows: list[dict], clause: str) -> list[dict]:
        conditions = re.split(r'\s+AND\s+', clause, flags=re.IGNORECASE)
        filtered = rows
        for cond in conditions:
            cond = cond.strip()

            # field LIKE 'pattern'
            m = re.match(r"(\w+\.)?(\w+)\s+LIKE\s+'([^']*)'", cond, re.IGNORECASE)
            if m:
                field = m.group(2)
                pattern = m.group(3)
                if pattern.startswith('%') and pattern.endswith('%'):
                    substr = pattern[1:-1]
                    filtered = [r for r in filtered if r.get(field) and substr in str(r[field])]
                elif pattern.endswith('%'):
                    prefix = pattern[:-1]
                    filtered = [r for r in filtered if r.get(field) and str(r[field]).startswith(prefix)]
                continue

            # field NOT LIKE 'pattern'
            m = re.match(r"(\w+\.)?(\w+)\s+NOT\s+LIKE\s+'([^']*)'", cond, re.IGNORECASE)
            if m:
                field = m.group(2)
                pattern = m.group(3)
                if pattern.startswith('%') and pattern.endswith('%'):
                    substr = pattern[1:-1]
                    filtered = [r for r in filtered if not (r.get(field) and substr in str(r[field]))]
                continue

            # field IS NOT NULL
            m = re.match(r'(\w+\.)?(\w+)\s+IS\s+NOT\s+NULL', cond, re.IGNORECASE)
            if m:
                field = m.group(2)
                filtered = [r for r in filtered if r.get(field) is not None]
                continue

            # field > value
            m = re.match(r'(\w+\.)?(\w+)\s*>\s*([\d.]+)', cond)
            if m:
                field = m.group(2)
                val = float(m.group(3))
                filtered = [r for r in filtered if r.get(field) is not None and float(r[field]) > val]
                continue

        return filtered

    def _apply_order(self, rows: list[dict], clause: str) -> list[dict]:
        parts = [p.strip() for p in clause.split(',')]
        # Применяем в обратном порядке (stable sort)
        for part in reversed(parts):
            tokens = part.split()
            # Убрать алиас таблицы
            field = tokens[0].split('.')[-1]
            desc = len(tokens) > 1 and tokens[1].upper() == 'DESC'

            # Для CASE WHEN ... выражений — пропускаем сложные
            if 'CASE' in part.upper():
                continue

            rows.sort(
                key=lambda r, f=field: (r.get(f) is None, r.get(f) or 0),
                reverse=desc,
            )
        return rows

    def fetchall(self) -> list[dict]:
        return self._results


# ============================================================================
# ИНИЦИАЛИЗАЦИЯ СХЕМЫ SQLite (для тестов)
# ============================================================================

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    auth_type TEXT NOT NULL DEFAULT 'telegram',
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    language TEXT NOT NULL DEFAULT 'ru',
    balance_kopeks INTEGER NOT NULL DEFAULT 0,
    used_promocodes INTEGER NOT NULL DEFAULT 0,
    has_had_paid_subscription INTEGER NOT NULL DEFAULT 0,
    referred_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    referral_code TEXT UNIQUE,
    referral_commission_percent INTEGER NOT NULL DEFAULT 0,
    email TEXT UNIQUE,
    email_verified INTEGER NOT NULL DEFAULT 0,
    email_verified_at TEXT,
    password_hash TEXT,
    email_verification_token TEXT,
    email_verification_expires TEXT,
    password_reset_token TEXT,
    password_reset_expires TEXT,
    cabinet_last_login TEXT,
    email_change_new TEXT,
    email_change_code TEXT,
    email_change_expires TEXT,
    google_id TEXT UNIQUE,
    yandex_id TEXT UNIQUE,
    discord_id TEXT UNIQUE,
    vk_id INTEGER UNIQUE,
    remnawave_uuid TEXT UNIQUE,
    trojan_password TEXT,
    vless_uuid TEXT,
    ss_password TEXT,
    last_remnawave_sync TEXT,
    promo_group_id INTEGER REFERENCES promo_groups(id) ON DELETE SET NULL,
    promo_offer_discount_percent INTEGER NOT NULL DEFAULT 0,
    promo_offer_discount_source TEXT,
    promo_offer_discount_expires_at TEXT,
    auto_promo_group_assigned INTEGER NOT NULL DEFAULT 0,
    auto_promo_group_threshold_kopeks INTEGER NOT NULL DEFAULT 0,
    personal_price_multiplier REAL NOT NULL DEFAULT 1.0,
    restriction_topup INTEGER NOT NULL DEFAULT 0,
    restriction_subscription INTEGER NOT NULL DEFAULT 0,
    restriction_reason TEXT,
    partner_status TEXT NOT NULL DEFAULT 'none',
    has_made_first_topup INTEGER NOT NULL DEFAULT 0,
    notification_settings TEXT,
    lifetime_used_traffic_bytes INTEGER NOT NULL DEFAULT 0,
    last_pinned_message_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_activity TEXT
);

CREATE TABLE IF NOT EXISTS promo_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    priority INTEGER NOT NULL DEFAULT 0,
    server_discount_percent INTEGER NOT NULL DEFAULT 0,
    traffic_discount_percent INTEGER NOT NULL DEFAULT 0,
    device_discount_percent INTEGER NOT NULL DEFAULT 0,
    period_discounts TEXT,
    auto_assign_total_spent_kopeks INTEGER,
    apply_discounts_to_addons INTEGER NOT NULL DEFAULT 0,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tariffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    display_order INTEGER NOT NULL DEFAULT 0,
    traffic_limit_gb INTEGER NOT NULL DEFAULT 100,
    allow_traffic_topup INTEGER NOT NULL DEFAULT 0,
    traffic_topup_enabled INTEGER NOT NULL DEFAULT 0,
    traffic_topup_packages TEXT,
    max_topup_traffic_gb INTEGER,
    server_traffic_limits TEXT,
    period_prices TEXT,
    device_limit INTEGER NOT NULL DEFAULT 1,
    device_price_kopeks INTEGER,
    max_device_limit INTEGER,
    allowed_squads TEXT,
    tier_level INTEGER NOT NULL DEFAULT 1,
    is_trial_available INTEGER NOT NULL DEFAULT 0,
    is_daily INTEGER NOT NULL DEFAULT 0,
    daily_price_kopeks INTEGER,
    custom_days_enabled INTEGER NOT NULL DEFAULT 0,
    price_per_day_kopeks INTEGER,
    min_days INTEGER,
    max_days INTEGER,
    custom_traffic_enabled INTEGER NOT NULL DEFAULT 0,
    traffic_price_per_gb_kopeks INTEGER,
    min_traffic_gb INTEGER,
    max_traffic_gb INTEGER,
    traffic_reset_mode TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active',
    is_trial INTEGER NOT NULL DEFAULT 0,
    start_date TEXT,
    end_date TEXT,
    is_daily_paused INTEGER NOT NULL DEFAULT 0,
    last_daily_charge_at TEXT,
    traffic_limit_gb INTEGER NOT NULL DEFAULT 0,
    traffic_used_gb REAL NOT NULL DEFAULT 0.0,
    purchased_traffic_gb INTEGER,
    traffic_reset_at TEXT,
    device_limit INTEGER NOT NULL DEFAULT 1,
    modem_enabled INTEGER NOT NULL DEFAULT 0,
    connected_squads TEXT,
    autopay_enabled INTEGER NOT NULL DEFAULT 0,
    autopay_days_before INTEGER NOT NULL DEFAULT 3,
    auto_renewed_before_expiry INTEGER NOT NULL DEFAULT 0,
    remnawave_short_id TEXT NOT NULL DEFAULT '',
    tariff_id INTEGER REFERENCES tariffs(id) ON DELETE SET NULL,
    subscription_url TEXT,
    subscription_crypto_link TEXT,
    remnawave_short_uuid TEXT,
    last_webhook_update_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    amount_kopeks INTEGER NOT NULL,
    description TEXT,
    payment_method TEXT,
    external_id TEXT,
    is_completed INTEGER NOT NULL DEFAULT 1,
    receipt_uuid TEXT,
    receipt_created_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    UNIQUE(external_id, payment_method)
);

CREATE TABLE IF NOT EXISTS referral_earnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    referral_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount_kopeks INTEGER NOT NULL,
    reason TEXT,
    referral_transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    campaign_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS withdrawal_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    amount_kopeks INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    payment_details TEXT,
    risk_score INTEGER,
    risk_analysis TEXT,
    processed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    processed_at TEXT,
    admin_comment TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS partner_applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    company_name TEXT,
    website_url TEXT,
    telegram_channel TEXT,
    description TEXT,
    expected_monthly_referrals INTEGER,
    desired_commission_percent INTEGER,
    status TEXT NOT NULL DEFAULT 'none',
    admin_comment TEXT,
    approved_commission_percent INTEGER,
    processed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    processed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS promocodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    balance_bonus_kopeks INTEGER,
    subscription_days INTEGER,
    max_uses INTEGER,
    current_uses INTEGER NOT NULL DEFAULT 0,
    valid_from TEXT,
    valid_until TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    first_purchase_only INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    promo_group_id INTEGER REFERENCES promo_groups(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS promocode_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    promocode_id INTEGER NOT NULL REFERENCES promocodes(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, promocode_id)
);

CREATE TABLE IF NOT EXISTS server_squads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    squad_uuid TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    original_name TEXT,
    country_code TEXT,
    is_available INTEGER NOT NULL DEFAULT 1,
    is_trial_eligible INTEGER NOT NULL DEFAULT 0,
    price_kopeks INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    max_users INTEGER,
    current_users INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS server_squad_promo_groups (
    server_squad_id INTEGER NOT NULL REFERENCES server_squads(id) ON DELETE CASCADE,
    promo_group_id INTEGER NOT NULL REFERENCES promo_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (server_squad_id, promo_group_id)
);

CREATE TABLE IF NOT EXISTS subscription_servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    server_squad_id INTEGER NOT NULL REFERENCES server_squads(id) ON DELETE CASCADE,
    connected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_price_kopeks INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS discount_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    notification_type TEXT,
    discount_percent INTEGER NOT NULL DEFAULT 0,
    bonus_amount_kopeks INTEGER NOT NULL DEFAULT 0,
    effect_type TEXT NOT NULL DEFAULT 'percent_discount',
    expires_at TEXT,
    claimed_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    extra_data TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS subscription_temporary_access (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    offer_id INTEGER NOT NULL REFERENCES discount_offers(id) ON DELETE CASCADE,
    squad_uuid TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deactivated_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    was_already_connected INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS traffic_purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    traffic_gb INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tariff_promo_groups (
    tariff_id INTEGER NOT NULL REFERENCES tariffs(id) ON DELETE CASCADE,
    promo_group_id INTEGER NOT NULL REFERENCES promo_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (tariff_id, promo_group_id)
);

CREATE TABLE IF NOT EXISTS user_promo_groups (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    promo_group_id INTEGER NOT NULL REFERENCES promo_groups(id) ON DELETE CASCADE,
    assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    assigned_by TEXT NOT NULL DEFAULT 'system',
    PRIMARY KEY (user_id, promo_group_id)
);

CREATE TABLE IF NOT EXISTS payment_method_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    method_id TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    display_name TEXT,
    sub_options TEXT,
    min_amount_kopeks INTEGER,
    max_amount_kopeks INTEGER,
    user_type_filter TEXT NOT NULL DEFAULT 'all',
    first_topup_filter TEXT NOT NULL DEFAULT 'any',
    promo_group_filter_mode TEXT NOT NULL DEFAULT 'all',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payment_method_promo_groups (
    payment_method_config_id INTEGER NOT NULL REFERENCES payment_method_configs(id) ON DELETE CASCADE,
    promo_group_id INTEGER NOT NULL REFERENCES promo_groups(id) ON DELETE CASCADE,
    PRIMARY KEY (payment_method_config_id, promo_group_id)
);

CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    priority TEXT NOT NULL DEFAULT 'normal',
    user_reply_block_permanent INTEGER NOT NULL DEFAULT 0,
    user_reply_block_until TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT,
    last_sla_reminder_at TEXT
);

CREATE TABLE IF NOT EXISTS ticket_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_text TEXT NOT NULL,
    is_from_admin INTEGER NOT NULL DEFAULT 0,
    has_media INTEGER NOT NULL DEFAULT 0,
    media_type TEXT,
    media_file_id TEXT,
    media_caption TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ticket_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    notification_type TEXT NOT NULL,
    message TEXT,
    is_for_admin INTEGER NOT NULL DEFAULT 0,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    read_at TEXT
);

CREATE TABLE IF NOT EXISTS polls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    reward_enabled INTEGER NOT NULL DEFAULT 0,
    reward_amount_kopeks INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS poll_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    "order" INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS poll_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL REFERENCES poll_questions(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    "order" INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS poll_responses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    poll_id INTEGER NOT NULL REFERENCES polls(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sent_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    reward_given INTEGER NOT NULL DEFAULT 0,
    reward_amount_kopeks INTEGER NOT NULL DEFAULT 0,
    UNIQUE(poll_id, user_id)
);

CREATE TABLE IF NOT EXISTS poll_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    response_id INTEGER NOT NULL REFERENCES poll_responses(id) ON DELETE CASCADE,
    question_id INTEGER NOT NULL REFERENCES poll_questions(id) ON DELETE CASCADE,
    option_id INTEGER NOT NULL REFERENCES poll_options(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(response_id, question_id)
);

CREATE TABLE IF NOT EXISTS contest_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    description TEXT,
    prize_type TEXT NOT NULL DEFAULT 'days',
    prize_value TEXT NOT NULL DEFAULT '1',
    max_winners INTEGER NOT NULL DEFAULT 1,
    attempts_per_user INTEGER NOT NULL DEFAULT 1,
    times_per_day INTEGER NOT NULL DEFAULT 1,
    schedule_times TEXT,
    cooldown_hours INTEGER NOT NULL DEFAULT 24,
    payload TEXT,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contest_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES contest_templates(id) ON DELETE CASCADE,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    payload TEXT,
    winners_count INTEGER NOT NULL DEFAULT 0,
    max_winners INTEGER NOT NULL DEFAULT 1,
    attempts_per_user INTEGER NOT NULL DEFAULT 1,
    message_id INTEGER,
    chat_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contest_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES contest_rounds(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    answer TEXT,
    is_winner INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(round_id, user_id)
);

CREATE TABLE IF NOT EXISTS referral_contests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    prize_text TEXT,
    contest_type TEXT NOT NULL DEFAULT 'referral_paid',
    start_at TEXT,
    end_at TEXT,
    daily_summary_time TEXT NOT NULL DEFAULT '12:00:00',
    daily_summary_times TEXT,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    is_active INTEGER NOT NULL DEFAULT 1,
    last_daily_summary_date TEXT,
    last_daily_summary_at TEXT,
    final_summary_sent INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS referral_contest_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id INTEGER NOT NULL REFERENCES referral_contests(id) ON DELETE CASCADE,
    referrer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    referral_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    amount_kopeks INTEGER,
    occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(contest_id, referral_id)
);

CREATE TABLE IF NOT EXISTS referral_contest_virtual_participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id INTEGER NOT NULL REFERENCES referral_contests(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    referral_count INTEGER NOT NULL DEFAULT 0,
    total_amount_kopeks INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wheel_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    is_enabled INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL DEFAULT 'Колесо удачи',
    spin_cost_stars INTEGER NOT NULL DEFAULT 50,
    spin_cost_days INTEGER NOT NULL DEFAULT 1,
    spin_cost_stars_enabled INTEGER NOT NULL DEFAULT 1,
    spin_cost_days_enabled INTEGER NOT NULL DEFAULT 0,
    rtp_percent INTEGER NOT NULL DEFAULT 70,
    daily_spin_limit INTEGER NOT NULL DEFAULT 1,
    min_subscription_days_for_day_payment INTEGER NOT NULL DEFAULT 5,
    promo_prefix TEXT NOT NULL DEFAULT 'WHEEL',
    promo_validity_days INTEGER NOT NULL DEFAULT 7,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wheel_prizes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id INTEGER NOT NULL REFERENCES wheel_configs(id) ON DELETE CASCADE,
    prize_type TEXT NOT NULL,
    prize_value INTEGER NOT NULL DEFAULT 0,
    display_name TEXT NOT NULL,
    emoji TEXT NOT NULL DEFAULT '🎁',
    color TEXT NOT NULL DEFAULT '#FFD700',
    prize_value_kopeks INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    manual_probability REAL,
    is_active INTEGER NOT NULL DEFAULT 1,
    promo_balance_bonus_kopeks INTEGER,
    promo_subscription_days INTEGER,
    promo_traffic_gb INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wheel_spins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    prize_id INTEGER NOT NULL REFERENCES wheel_prizes(id) ON DELETE CASCADE,
    payment_type TEXT NOT NULL,
    payment_amount INTEGER NOT NULL DEFAULT 0,
    payment_value_kopeks INTEGER NOT NULL DEFAULT 0,
    prize_type TEXT NOT NULL,
    prize_value INTEGER NOT NULL DEFAULT 0,
    prize_display_name TEXT NOT NULL,
    prize_value_kopeks INTEGER NOT NULL DEFAULT 0,
    generated_promocode_id INTEGER REFERENCES promocodes(id) ON DELETE SET NULL,
    is_applied INTEGER NOT NULL DEFAULT 0,
    applied_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS subscription_conversions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    converted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    trial_duration_days INTEGER,
    payment_method TEXT,
    first_payment_amount_kopeks INTEGER,
    first_paid_period_days INTEGER
);

CREATE TABLE IF NOT EXISTS subscription_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS promo_offer_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    offer_type TEXT NOT NULL,
    message_text TEXT,
    button_text TEXT,
    valid_hours INTEGER NOT NULL DEFAULT 24,
    discount_percent INTEGER NOT NULL DEFAULT 0,
    bonus_amount_kopeks INTEGER NOT NULL DEFAULT 0,
    active_discount_hours INTEGER NOT NULL DEFAULT 1,
    test_duration_hours INTEGER NOT NULL DEFAULT 1,
    test_squad_uuids TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS promo_offer_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    offer_id INTEGER NOT NULL REFERENCES discount_offers(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    source TEXT,
    percent INTEGER,
    effect_type TEXT,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS advertising_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    start_parameter TEXT NOT NULL UNIQUE,
    bonus_type TEXT,
    balance_bonus_kopeks INTEGER NOT NULL DEFAULT 0,
    subscription_duration_days INTEGER,
    subscription_traffic_gb INTEGER,
    subscription_device_limit INTEGER,
    subscription_squads TEXT,
    tariff_id INTEGER REFERENCES tariffs(id) ON DELETE SET NULL,
    tariff_duration_days INTEGER,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS broadcast_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    message_text TEXT,
    has_media INTEGER NOT NULL DEFAULT 0,
    media_type TEXT,
    media_file_id TEXT,
    media_caption TEXT,
    total_count INTEGER NOT NULL DEFAULT 0,
    sent_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    blocked_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    admin_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    admin_name TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS admin_roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    level INTEGER NOT NULL DEFAULT 0,
    permissions TEXT NOT NULL DEFAULT '[]',
    color TEXT,
    icon TEXT,
    is_system INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES admin_roles(id) ON DELETE CASCADE,
    assigned_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(user_id, role_id)
);

CREATE TABLE IF NOT EXISTS access_policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    role_id INTEGER NOT NULL REFERENCES admin_roles(id) ON DELETE CASCADE,
    priority INTEGER NOT NULL DEFAULT 0,
    effect TEXT NOT NULL DEFAULT 'allow',
    conditions TEXT NOT NULL DEFAULT '{}',
    resource TEXT,
    actions TEXT NOT NULL DEFAULT '[]',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    details TEXT,
    ip_address TEXT,
    user_agent TEXT,
    status TEXT,
    request_method TEXT,
    request_path TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS support_audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER,
    actor_telegram_id INTEGER,
    is_moderator INTEGER NOT NULL DEFAULT 0,
    action TEXT NOT NULL,
    ticket_id INTEGER,
    target_user_id INTEGER,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS web_api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT,
    last_used_at TEXT,
    last_used_ip TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_by TEXT
);

CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    event_type TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_id INTEGER NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
    payload TEXT,
    response_status INTEGER,
    response_body TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cabinet_refresh_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS required_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL UNIQUE,
    channel_title TEXT NOT NULL,
    channel_username TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_channel_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES required_channels(id) ON DELETE CASCADE,
    is_subscribed INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS main_menu_buttons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    action_type TEXT NOT NULL,
    action_value TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'all',
    is_active INTEGER NOT NULL DEFAULT 1,
    display_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS welcome_texts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text_content TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pinned_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    media_type TEXT,
    media_file_id TEXT,
    send_before_menu INTEGER NOT NULL DEFAULT 0,
    send_on_every_start INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_text TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS monitoring_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS privacy_policies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    language TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS public_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    language TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS faq_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    language TEXT NOT NULL UNIQUE,
    is_enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS faq_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    language TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    display_order INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS service_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    "order" INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    language TEXT NOT NULL DEFAULT 'ru',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS menu_layout_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layout_json TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS button_click_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    button_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sent_notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    notification_type TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS squads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    country_code TEXT,
    is_available INTEGER NOT NULL DEFAULT 1,
    price_kopeks INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Индексы для производительности
CREATE INDEX IF NOT EXISTS ix_users_telegram_id ON users(telegram_id);
CREATE INDEX IF NOT EXISTS ix_users_status ON users(status);
CREATE INDEX IF NOT EXISTS ix_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS ix_subscriptions_status ON subscriptions(status, is_trial);
CREATE INDEX IF NOT EXISTS ix_transactions_user_id ON transactions(user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_transactions_type ON transactions(type, created_at, is_completed);
CREATE INDEX IF NOT EXISTS ix_referral_earnings_user_id ON referral_earnings(user_id);
CREATE INDEX IF NOT EXISTS ix_admin_audit_log_user ON admin_audit_log(user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_admin_audit_log_resource ON admin_audit_log(resource_type, resource_id);
"""


def init_schema(bedolaga: DbAdapter):
    """Создать все таблицы если не существуют (только для SQLite)."""
    if isinstance(bedolaga, SqliteAdapter):
        log('📦 Инициализация схемы БД (SQLite)...')
        bedolaga.raw_connection.executescript(SCHEMA_SQL)
        bedolaga.commit()
        log('✅ Схема создана')
    else:
        # PostgreSQL — таблицы уже созданы ботом, проверяем наличие
        log('📦 Проверка схемы БД (PostgreSQL)...')
        if not bedolaga.table_exists('users'):
            log('❌ Таблица users не найдена! Запустите бота для инициализации схемы.')
            sys.exit(1)
        log('✅ Схема PostgreSQL уже существует')

        # Расширить balance_kopeks до BIGINT (для больших балансов > 21M₽)
        log('🔧 Расширение balance_kopeks до BIGINT...')
        bedolaga.execute('ALTER TABLE users ALTER COLUMN balance_kopeks TYPE BIGINT')
        bedolaga.commit()
        log('  ✅ balance_kopeks → BIGINT')


# ============================================================================
# МИГРАЦИЯ ПОЛЬЗОВАТЕЛЕЙ
# ============================================================================


def migrate_users(shm_conn, bedolaga: DbAdapter, dry_run: bool) -> dict[int, int]:
    """
    Мигрировать пользователей.
    Возвращает маппинг shm_user_id -> bedolaga_user.id
    """
    log('\n👥 Миграция пользователей...')

    cursor = shm_conn.cursor()
    cursor.execute("""
        SELECT
            u.user_id, u.login, u.balance, u.credit, u.bonus,
            u.block, u.created, u.last_login, u.settings, u.partner_id
        FROM users u
        WHERE u.login LIKE '@%'
        ORDER BY u.user_id
    """)
    shm_users = cursor.fetchall()

    log(f'  Найдено {len(shm_users)} пользователей с Telegram ID')

    used_codes: set[str] = set()
    # Загрузить уже существующие коды
    existing_codes = bedolaga.fetchall('SELECT referral_code FROM users WHERE referral_code IS NOT NULL')
    for row in existing_codes:
        used_codes.add(row[0])

    shm_to_bedolaga: dict[int, int] = {}  # shm_user_id -> bedolaga user.id
    telegram_to_shm: dict[int, int] = {}  # telegram_id -> shm_user_id (для дедупликации)

    inserted = 0
    skipped = 0
    errors = 0

    for user in shm_users:
        shm_id = user['user_id']
        login = user['login']
        telegram_id = extract_telegram_id(login)
        if not telegram_id:
            skipped += 1
            continue

        # Дедупликация по telegram_id
        if telegram_id in telegram_to_shm:
            skipped += 1
            continue
        telegram_to_shm[telegram_id] = shm_id

        # Извлечь Telegram данные из settings
        tg = extract_tg_data(user['settings'])
        first_name = tg.get('first_name') or tg.get('name')
        last_name = tg.get('last_name')
        username = tg.get('login') or tg.get('username')
        # Убрать @ из username если есть
        if username and username.startswith('@'):
            username = username[1:]

        # Баланс: balance + bonus (рубли -> копейки), credit всегда 0
        balance_rub = Decimal(str(user['balance'] or 0)) + Decimal(str(user['bonus'] or 0))
        balance_kopeks = int(balance_rub * 100)
        balance_kopeks = max(balance_kopeks, 0)

        status = 'blocked' if user['block'] else 'active'
        referral_code = gen_referral_code(used_codes)
        created_at = to_dt_str(user['created']) or now_utc()
        last_activity = to_dt_str(user['last_login'])

        # Проверить есть ли уже в Bedolaga
        existing = bedolaga.fetchone('SELECT id FROM users WHERE telegram_id = %s', (telegram_id,))
        if existing:
            shm_to_bedolaga[shm_id] = existing[0]
            skipped += 1
            continue

        try:
            if not dry_run:
                new_id = bedolaga.insert_returning_id(
                    """
                    INSERT INTO users (
                        telegram_id, auth_type, username, first_name, last_name,
                        status, language, balance_kopeks,
                        referral_code, created_at, updated_at, last_activity,
                        has_had_paid_subscription, email_verified,
                        auto_promo_group_assigned, auto_promo_group_threshold_kopeks,
                        promo_offer_discount_percent, personal_price_multiplier,
                        has_made_first_topup, restriction_topup, restriction_subscription,
                        partner_status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                              %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                    (
                        telegram_id,
                        'telegram',
                        username,
                        first_name,
                        last_name,
                        status,
                        'ru',
                        balance_kopeks,
                        referral_code,
                        created_at,
                        now_utc(),
                        last_activity,
                        False,  # has_had_paid_subscription (обновится в migrate_subscriptions)
                        False,  # email_verified
                        False,  # auto_promo_group_assigned
                        0,  # auto_promo_group_threshold_kopeks
                        0,  # promo_offer_discount_percent
                        1.0,  # personal_price_multiplier
                        False,  # has_made_first_topup
                        False,  # restriction_topup
                        False,  # restriction_subscription
                        'none',  # partner_status
                    ),
                )
                shm_to_bedolaga[shm_id] = new_id
            else:
                # В dry_run режиме назначаем временный ID
                shm_to_bedolaga[shm_id] = shm_id
            inserted += 1
        except Exception as e:
            log(f'  ⚠️  Пропуск user {telegram_id}: {e}')
            errors += 1

    if not dry_run:
        bedolaga.commit()

    log(f'  ✅ Вставлено: {inserted}, Пропущено: {skipped}, Ошибок: {errors}')
    return shm_to_bedolaga


# ============================================================================
# ОБНОВЛЕНИЕ РЕФЕРАЛЬНЫХ СВЯЗЕЙ
# ============================================================================


def migrate_referrals(shm_conn, bedolaga: DbAdapter, shm_to_bedolaga: dict[int, int], dry_run: bool):
    """Заполнить referred_by_id для пользователей у которых есть partner_id."""
    log('\n🔗 Миграция реферальных связей...')

    cursor = shm_conn.cursor()
    cursor.execute("""
        SELECT user_id, partner_id FROM users
        WHERE login LIKE '@%' AND partner_id IS NOT NULL
    """)
    referral_pairs = cursor.fetchall()

    updated = 0
    skipped = 0

    for row in referral_pairs:
        shm_user_id = row['user_id']
        shm_partner_id = row['partner_id']

        bedolaga_user_id = shm_to_bedolaga.get(shm_user_id)
        bedolaga_partner_id = shm_to_bedolaga.get(shm_partner_id)

        if not bedolaga_user_id or not bedolaga_partner_id:
            skipped += 1
            continue

        if not dry_run:
            bedolaga.execute(
                'UPDATE users SET referred_by_id = %s WHERE id = %s',
                (bedolaga_partner_id, bedolaga_user_id),
            )
        updated += 1

    if not dry_run:
        bedolaga.commit()

    log(f'  ✅ Обновлено referred_by_id: {updated}, Пропущено: {skipped}')


# ============================================================================
# МИГРАЦИЯ ПОДПИСОК
# ============================================================================


def migrate_subscriptions(shm_conn, bedolaga: DbAdapter, shm_to_bedolaga: dict[int, int], dry_run: bool):
    """Мигрировать ВСЕ подписки из SHM, tariff_id = Стандарт."""
    log('\n📋 Миграция подписок (все)...')

    cursor = shm_conn.cursor()
    cursor.execute("""
        SELECT
            us.user_id as shm_user_id,
            us.user_service_id,
            us.service_id,
            us.status,
            us.created,
            us.expire,
            us.auto_bill,
            s.category
        FROM user_services us
        JOIN services s ON us.service_id = s.service_id
        ORDER BY us.user_service_id
    """)
    all_subs = cursor.fetchall()

    unique_users = len({s['shm_user_id'] for s in all_subs})
    log(f'  Всего подписок: {len(all_subs)}, уникальных юзеров: {unique_users}')

    inserted = 0
    skipped = 0
    errors = 0
    has_paid_users: set[int] = set()

    for sub in all_subs:
        shm_uid = sub['shm_user_id']
        bedolaga_uid = shm_to_bedolaga.get(shm_uid)
        if not bedolaga_uid:
            skipped += 1
            continue

        raw_status = sub['status']
        bedolaga_status = STATUS_MAP.get(raw_status, 'expired')

        is_trial = sub['service_id'] in TRIAL_SERVICE_IDS
        traffic_limit_gb = SERVICE_TRAFFIC_MAP.get(sub['service_id'], 0)

        start_date = to_dt_str(sub['created'])
        end_date = to_dt_str(sub['expire']) or start_date or now_utc()
        autopay = False

        if not is_trial:
            has_paid_users.add(bedolaga_uid)

        try:
            if not dry_run:
                short_id = f'shm_{sub.get("user_service_id", shm_uid)}'

                bedolaga.execute(
                    """
                    INSERT INTO subscriptions (
                        user_id, status, is_trial, start_date, end_date,
                        traffic_limit_gb, traffic_used_gb, device_limit,
                        autopay_enabled, auto_renewed_before_expiry, is_daily_paused,
                        remnawave_short_id, tariff_id,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                    (
                        bedolaga_uid,
                        bedolaga_status,
                        is_trial,
                        start_date,
                        end_date,
                        traffic_limit_gb,
                        0.0,
                        3,
                        autopay,
                        False,
                        False,
                        short_id,
                        MIGRATION_TARIFF_ID,
                        start_date or now_utc(),
                        now_utc(),
                    ),
                )
            inserted += 1
        except Exception as e:
            log(f'  ⚠️  Пропуск подписки {sub.get("user_service_id")}: {e}')
            errors += 1

    # Обновить has_had_paid_subscription одним батчем
    if not dry_run and has_paid_users:
        for uid in has_paid_users:
            bedolaga.execute(
                'UPDATE users SET has_had_paid_subscription = TRUE WHERE id = %s',
                (uid,),
            )

    if not dry_run:
        bedolaga.commit()

    log(f'  ✅ Вставлено: {inserted}, Пропущено: {skipped}, Ошибок: {errors}')

    if not dry_run:
        bedolaga.commit()

    log(f'  ✅ Вставлено: {inserted}, Пропущено: {skipped}, Ошибок: {errors}')


# ============================================================================
# МИГРАЦИЯ ТРАНЗАКЦИЙ (ПОПОЛНЕНИЯ)
# ============================================================================


def migrate_transactions(shm_conn, bedolaga: DbAdapter, shm_to_bedolaga: dict[int, int], dry_run: bool):
    """Мигрировать pays_history -> transactions (DEPOSIT)."""
    log('\n💳 Миграция истории платежей...')

    cursor = shm_conn.cursor()
    cursor.execute("""
        SELECT
            ph.id, ph.user_id, ph.money, ph.pay_system_id,
            ph.date, ph.uniq_key, ph.comment
        FROM pays_history ph
        WHERE ph.money > 0
        ORDER BY ph.id
    """)
    payments = cursor.fetchall()

    log(f'  Найдено {len(payments)} платежей')

    inserted = 0
    skipped = 0
    errors = 0

    for pay in payments:
        shm_uid = pay['user_id']
        bedolaga_uid = shm_to_bedolaga.get(shm_uid)
        if not bedolaga_uid:
            skipped += 1
            continue

        amount_kopeks = rubles_to_kopeks(pay['money'])
        if amount_kopeks <= 0:
            skipped += 1
            continue

        payment_method = PAYMENT_METHOD_MAP.get(pay['pay_system_id'], 'manual')
        external_id = pay['uniq_key'] or f'shm_{pay["id"]}'
        created_at = to_dt_str(pay['date']) or now_utc()

        # Описание из comment
        comment_data = pay['comment']
        if isinstance(comment_data, str):
            try:
                comment_data = json.loads(comment_data)
            except Exception:
                comment_data = {}
        description = None
        if isinstance(comment_data, dict):
            description = comment_data.get('comment')

        try:
            if not dry_run:
                affected = bedolaga.insert_on_conflict_ignore(
                    """
                    INSERT INTO transactions (
                        user_id, type, amount_kopeks, payment_method,
                        external_id, is_completed, description,
                        created_at, completed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (external_id, payment_method) DO NOTHING
                """,
                    (
                        bedolaga_uid,
                        'deposit',
                        amount_kopeks,
                        payment_method,
                        external_id,
                        True,
                        description,
                        created_at,
                        created_at,
                    ),
                )
                if affected > 0:
                    inserted += 1
                else:
                    skipped += 1
            else:
                inserted += 1
        except Exception as e:
            log(f'  ⚠️  Пропуск транзакции {pay["id"]}: {e}')
            errors += 1

    if not dry_run:
        bedolaga.commit()

    log(f'  ✅ Вставлено: {inserted}, Пропущено/дубли: {skipped}, Ошибок: {errors}')


# ============================================================================
# МИГРАЦИЯ ПОКУПОК ПОДПИСОК (withdraw_history)
# ============================================================================


def migrate_subscription_purchases(shm_conn, bedolaga: DbAdapter, shm_to_bedolaga: dict[int, int], dry_run: bool):
    """Мигрировать withdraw_history -> transactions (subscription_payment)."""
    log('\n🛒 Миграция покупок подписок (withdraw_history)...')

    cursor = shm_conn.cursor()
    cursor.execute("""
        SELECT
            wh.withdraw_id, wh.user_id, wh.create_date, wh.cost,
            wh.discount, wh.bonus, wh.months, wh.total,
            wh.service_id, wh.user_service_id
        FROM withdraw_history wh
        ORDER BY wh.withdraw_id
    """)
    withdrawals = cursor.fetchall()

    log(f'  Найдено {len(withdrawals)} записей покупок')

    inserted = 0
    skipped = 0
    errors = 0

    for w in withdrawals:
        shm_uid = w['user_id']
        bedolaga_uid = shm_to_bedolaga.get(shm_uid)
        if not bedolaga_uid:
            skipped += 1
            continue

        cost = Decimal(str(w['cost'] or 0))
        bonus_used = Decimal(str(w['bonus'] or 0))
        total = cost + bonus_used  # Общая стоимость (деньги + бонус)

        if total <= 0:
            skipped += 1
            continue

        amount_kopeks = int(total * 100)
        service_id = w['service_id']
        service_name = SERVICE_NAME_MAP.get(service_id, f'Услуга #{service_id}')
        created_at = to_dt_str(w['create_date']) or now_utc()
        external_id = f'shm_withdraw_{w["withdraw_id"]}'

        description = f'Покупка: {service_name}'
        if bonus_used > 0:
            description += f' (бонус: {bonus_used}₽)'

        try:
            if not dry_run:
                affected = bedolaga.insert_on_conflict_ignore(
                    """
                    INSERT INTO transactions (
                        user_id, type, amount_kopeks, payment_method,
                        external_id, is_completed, description,
                        created_at, completed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (external_id, payment_method) DO NOTHING
                """,
                    (
                        bedolaga_uid,
                        'subscription_payment',
                        amount_kopeks,
                        'balance',
                        external_id,
                        True,
                        description,
                        created_at,
                        created_at,
                    ),
                )
                if affected > 0:
                    inserted += 1
                else:
                    skipped += 1
            else:
                inserted += 1
        except Exception as e:
            log(f'  ⚠️  Пропуск покупки {w["withdraw_id"]}: {e}')
            errors += 1

    if not dry_run:
        bedolaga.commit()

    log(f'  ✅ Вставлено: {inserted}, Пропущено/дубли: {skipped}, Ошибок: {errors}')


# ============================================================================
# МИГРАЦИЯ БОНУСНЫХ НАЧИСЛЕНИЙ (акции, ручные)
# ============================================================================


def migrate_bonus_deposits(shm_conn, bedolaga: DbAdapter, shm_to_bedolaga: dict[int, int], dry_run: bool):
    """Мигрировать bonus_history (акции/ручные) -> transactions (deposit)."""
    log('\n🎁 Миграция бонусных начислений...')

    cursor = shm_conn.cursor()
    # Все позитивные бонусы, КРОМЕ реферальных (from_user_id) — они уже в referral_earnings
    cursor.execute("""
        SELECT
            bh.id, bh.user_id, bh.bonus, bh.date, bh.comment
        FROM bonus_history bh
        WHERE bh.bonus > 0
          AND bh.comment NOT LIKE '%from_user_id%'
        ORDER BY bh.id
    """)
    bonuses = cursor.fetchall()

    log(f'  Найдено {len(bonuses)} бонусных начислений')

    inserted = 0
    skipped = 0

    for b in bonuses:
        shm_uid = b['user_id']
        bedolaga_uid = shm_to_bedolaga.get(shm_uid)
        if not bedolaga_uid:
            skipped += 1
            continue

        amount_kopeks = rubles_to_kopeks(b['bonus'])
        if amount_kopeks <= 0:
            skipped += 1
            continue

        created_at = to_dt_str(b['date']) or now_utc()
        external_id = f'shm_bonus_{b["id"]}'

        # Определить описание
        comment = b['comment']
        description = 'Бонусное начисление (SHM)'
        if isinstance(comment, str):
            try:
                cdata = json.loads(comment)
                if isinstance(cdata, dict) and cdata.get('msg'):
                    description = f'Бонус: {cdata["msg"]}'
            except Exception:
                pass

        try:
            if not dry_run:
                affected = bedolaga.insert_on_conflict_ignore(
                    """
                    INSERT INTO transactions (
                        user_id, type, amount_kopeks, payment_method,
                        external_id, is_completed, description,
                        created_at, completed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (external_id, payment_method) DO NOTHING
                """,
                    (
                        bedolaga_uid,
                        'deposit',
                        amount_kopeks,
                        'bonus',
                        external_id,
                        True,
                        description,
                        created_at,
                        created_at,
                    ),
                )
                if affected > 0:
                    inserted += 1
                else:
                    skipped += 1
            else:
                inserted += 1
        except Exception:
            skipped += 1

    if not dry_run:
        bedolaga.commit()

    log(f'  ✅ Вставлено: {inserted}, Пропущено/дубли: {skipped}')


# ============================================================================
# МИГРАЦИЯ РЕФЕРАЛЬНЫХ НАЧИСЛЕНИЙ
# ============================================================================


def migrate_referral_earnings(shm_conn, bedolaga: DbAdapter, shm_to_bedolaga: dict[int, int], dry_run: bool):
    """Мигрировать bonus_history (referral) -> referral_earnings."""
    log('\n💰 Миграция реферальных начислений...')

    cursor = shm_conn.cursor()
    # Только позитивные реферальные начисления (from_user_id = тот кого пригласили)
    cursor.execute("""
        SELECT
            bh.id, bh.user_id, bh.bonus, bh.date, bh.comment
        FROM bonus_history bh
        WHERE bh.comment LIKE '%from_user_id%'
          AND bh.bonus > 0
        ORDER BY bh.id
    """)
    earnings = cursor.fetchall()

    log(f'  Найдено {len(earnings)} реферальных начислений')

    inserted = 0
    skipped = 0

    for earn in earnings:
        shm_uid = earn['user_id']  # Реферер (кто получает бонус)
        bedolaga_uid = shm_to_bedolaga.get(shm_uid)
        if not bedolaga_uid:
            skipped += 1
            continue

        # Извлечь from_user_id
        comment = earn['comment']
        if isinstance(comment, str):
            try:
                comment = json.loads(comment)
            except Exception:
                skipped += 1
                continue

        if not isinstance(comment, dict):
            skipped += 1
            continue

        shm_referral_id = comment.get('from_user_id')  # Тот кто был приглашён
        if not shm_referral_id:
            skipped += 1
            continue

        bedolaga_referral_id = shm_to_bedolaga.get(int(shm_referral_id))
        if not bedolaga_referral_id:
            skipped += 1
            continue

        amount_kopeks = rubles_to_kopeks(earn['bonus'])
        created_at = to_dt_str(earn['date']) or now_utc()
        percent = comment.get('percent', 0)

        try:
            if not dry_run:
                bedolaga.execute(
                    """
                    INSERT INTO referral_earnings (
                        user_id, referral_id, amount_kopeks, reason, created_at
                    ) VALUES (%s, %s, %s, %s, %s)
                """,
                    (
                        bedolaga_uid,
                        bedolaga_referral_id,
                        amount_kopeks,
                        f'referral_payment_{percent}pct',
                        created_at,
                    ),
                )
            inserted += 1
        except Exception as e:
            log(f'  ⚠️  Пропуск earning {earn["id"]}: {e}')
            skipped += 1

    if not dry_run:
        bedolaga.commit()

    log(f'  ✅ Вставлено: {inserted}, Пропущено: {skipped}')


# ============================================================================
# ИТОГОВАЯ СТАТИСТИКА
# ============================================================================


def print_stats(bedolaga: DbAdapter):
    log('\n📊 Итоговая статистика Bedolaga БД:')

    tables = [
        ('users', 'Пользователи'),
        ('subscriptions', 'Подписки'),
        ('transactions', 'Транзакции'),
        ('referral_earnings', 'Реферальные начисления'),
    ]

    for table, label in tables:
        count = bedolaga.fetchone(f'SELECT COUNT(*) FROM {table}')[0]
        log(f'  {label}: {count}')

    # Транзакции по типам
    for tx_type in ('deposit', 'subscription_payment'):
        tx_count = bedolaga.fetchone('SELECT COUNT(*) FROM transactions WHERE type = %s', (tx_type,))[0]
        tx_sum = bedolaga.fetchone(
            'SELECT COALESCE(SUM(amount_kopeks), 0) FROM transactions WHERE type = %s', (tx_type,)
        )[0]
        log(f'  Транзакции [{tx_type}]: {tx_count} шт, сумма: {tx_sum // 100}₽')

    # Активные подписки
    active = bedolaga.fetchone("SELECT COUNT(*) FROM subscriptions WHERE status = 'active'")[0]
    expired = bedolaga.fetchone("SELECT COUNT(*) FROM subscriptions WHERE status = 'expired'")[0]
    disabled = bedolaga.fetchone("SELECT COUNT(*) FROM subscriptions WHERE status = 'disabled'")[0]
    log(f'  Подписки по статусам: active={active}, expired={expired}, disabled={disabled}')

    # Баланс
    total_balance = bedolaga.fetchone('SELECT COALESCE(SUM(balance_kopeks), 0) FROM users')[0]
    log(f'  Суммарный баланс: {total_balance // 100}₽ {total_balance % 100}коп ({total_balance} коп)')

    # Пользователи с рефером
    with_referrer = bedolaga.fetchone('SELECT COUNT(*) FROM users WHERE referred_by_id IS NOT NULL')[0]
    log(f'  Пользователей с рефером: {with_referrer}')

    # Заблокированные
    blocked = bedolaga.fetchone("SELECT COUNT(*) FROM users WHERE status = 'blocked'")[0]
    log(f'  Заблокированных пользователей: {blocked}')


# ============================================================================
# MAIN
# ============================================================================


def main():
    parser = argparse.ArgumentParser(description='Миграция SHM -> Bedolaga')
    parser.add_argument('--dry-run', action='store_true', help='Тестовый прогон без записи в БД')
    parser.add_argument(
        '--from-dump', type=str, metavar='FILE', help='Путь к SQL-дампу MySQL (вместо подключения к MySQL)'
    )
    parser.add_argument(
        '--pg',
        type=str,
        metavar='DSN',
        help='PostgreSQL connection string (например postgresql://user:pass@host:5432/db)',
    )
    parser.add_argument(
        '--sqlite-path', type=str, default=SQLITE_PATH, help=f'Путь к SQLite БД Bedolaga (по умолчанию: {SQLITE_PATH})'
    )
    args = parser.parse_args()

    dry_run = args.dry_run
    if dry_run:
        log('🧪 DRY RUN режим — данные НЕ будут записаны в БД')

    # Подключение к SHM
    if args.from_dump:
        log(f'\n📄 Загрузка SHM из SQL-дампа: {args.from_dump}')
        try:
            shm_conn = SqlDumpConnection(args.from_dump)
        except Exception as e:
            log(f'  ❌ Ошибка парсинга дампа: {e}')
            sys.exit(1)
    else:
        log('\n🔌 Подключение к SHM MySQL...')
        if pymysql is None:
            log('  ❌ pymysql не установлен. Используйте --from-dump для работы с SQL-дампом.')
            sys.exit(1)
        try:
            shm_conn = pymysql.connect(**get_mysql_config())
            log('  ✅ MySQL подключён')
        except Exception as e:
            log(f'  ❌ Ошибка подключения к MySQL: {e}')
            sys.exit(1)

    # Подключение к целевой БД (PostgreSQL или SQLite)
    if args.pg:
        log(f'🗄️  Подключение к PostgreSQL: {args.pg.split("@")[-1] if "@" in args.pg else args.pg}')
        try:
            bedolaga = PgAdapter(args.pg)
            log('  ✅ PostgreSQL подключён')
        except Exception as e:
            log(f'  ❌ Ошибка подключения к PostgreSQL: {e}')
            sys.exit(1)
    else:
        sqlite_path = args.sqlite_path
        log(f'🗄️  Открытие Bedolaga SQLite: {sqlite_path}')
        conn = sqlite3.connect(sqlite_path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=OFF')  # OFF во время миграции для скорости
        bedolaga = SqliteAdapter(conn)

    # Инициализация схемы
    init_schema(bedolaga)

    try:
        # Шаг 1: Пользователи
        shm_to_bedolaga = migrate_users(shm_conn, bedolaga, dry_run)

        # Шаг 2: Реферальные связи
        migrate_referrals(shm_conn, bedolaga, shm_to_bedolaga, dry_run)

        # Шаг 3: Подписки
        migrate_subscriptions(shm_conn, bedolaga, shm_to_bedolaga, dry_run)

        # Шаг 4: Транзакции (пополнения)
        migrate_transactions(shm_conn, bedolaga, shm_to_bedolaga, dry_run)

        # Шаг 5: Покупки подписок (withdraw_history)
        migrate_subscription_purchases(shm_conn, bedolaga, shm_to_bedolaga, dry_run)

        # Шаг 6: Бонусные начисления (акции, ручные)
        migrate_bonus_deposits(shm_conn, bedolaga, shm_to_bedolaga, dry_run)

        # Шаг 7: Реферальные начисления
        migrate_referral_earnings(shm_conn, bedolaga, shm_to_bedolaga, dry_run)

        # Включить FK обратно (только для SQLite)
        if not dry_run and isinstance(bedolaga, SqliteAdapter):
            bedolaga.execute('PRAGMA foreign_keys=ON')

        # Статистика
        if not dry_run:
            print_stats(bedolaga)

        log('\n🎉 Миграция завершена!')
        if dry_run:
            log('   (DRY RUN — данные не записаны)')

    except Exception as e:
        log(f'\n❌ Ошибка миграции: {e}')
        import traceback

        traceback.print_exc()
        sys.exit(1)
    finally:
        shm_conn.close()
        bedolaga.close()


if __name__ == '__main__':
    main()
