"""
Скрипт миграции пользователей из SHM (MySQL dump) в Bedolaga Telegram Bot (PostgreSQL).

Использование:
    python3 scripts/migrate_from_shm.py --sql /tmp/shm_old.sql [--dry-run]

Безопасен для повторного запуска (идемпотентен через external_id + payment_method).
"""

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


# --- Setup project path ---
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, PROJECT_ROOT)

import asyncio  # noqa: E402
import os  # noqa: E402


os.chdir(PROJECT_ROOT)

from sqlalchemy import select  # noqa: E402

from app.database.database import AsyncSessionLocal  # noqa: E402
from app.database.models import (  # noqa: E402
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
    UserStatus,
)


# ---------------------------------------------------------------------------
# SQL dump parser
# ---------------------------------------------------------------------------


def parse_sql_value(raw: str) -> str | None:
    """Parse a single SQL value token, returning Python string or None."""
    raw = raw.strip()
    if raw.upper() == 'NULL':
        return None
    if raw.startswith("'") and raw.endswith("'"):
        inner = raw[1:-1]
        # Unescape MySQL escapes
        inner = inner.replace("\\'", "'")
        inner = inner.replace('\\"', '"')
        inner = inner.replace('\\\\', '\\')
        inner = inner.replace('\\n', '\n')
        inner = inner.replace('\\r', '\r')
        inner = inner.replace('\\t', '\t')
        inner = inner.replace('\\0', '\0')
        return inner
    return raw


def tokenize_row(row_str: str) -> list[str | None]:
    """
    Tokenize a single VALUES row (without outer parens) into a list of values.
    Handles: NULL, numbers, quoted strings with escapes, JSON with nested braces.
    """
    tokens: list[str | None] = []
    i = 0
    n = len(row_str)

    while i < n:
        # Skip whitespace
        while i < n and row_str[i] in (' ', '\t', '\r', '\n'):
            i += 1
        if i >= n:
            break

        ch = row_str[i]

        if ch == ',':
            i += 1
            continue

        if ch == "'":
            # Quoted string — find closing quote, handling escapes
            j = i + 1
            while j < n:
                if row_str[j] == '\\':
                    j += 2  # skip escaped char
                    continue
                if row_str[j] == "'":
                    break
                j += 1
            token = row_str[i : j + 1]
            tokens.append(parse_sql_value(token))
            i = j + 1

        elif row_str[i : i + 4].upper() == 'NULL':
            tokens.append(None)
            i += 4

        else:
            # Unquoted value (number, etc.)
            j = i
            while j < n and row_str[j] not in (',', ')'):
                j += 1
            token = row_str[i:j].strip()
            tokens.append(parse_sql_value(token) if token else None)
            i = j

    return tokens


def extract_rows_from_values(values_str: str) -> list[list[str | None]]:
    """
    Given the part after VALUES in an INSERT statement,
    split into individual row tuples and tokenize each.
    """
    rows: list[list[str | None]] = []
    i = 0
    n = len(values_str)

    while i < n:
        # Find opening paren
        while i < n and values_str[i] != '(':
            i += 1
        if i >= n:
            break

        # Find matching closing paren
        i += 1  # skip '('
        depth = 1
        start = i
        in_quote = False
        escape_next = False

        while i < n and depth > 0:
            ch = values_str[i]
            if escape_next:
                escape_next = False
                i += 1
                continue
            if ch == '\\':
                escape_next = True
                i += 1
                continue
            if ch == "'" and not escape_next:
                in_quote = not in_quote
            elif not in_quote:
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
            i += 1

        row_content = values_str[start : i - 1]
        tokens = tokenize_row(row_content)
        if tokens:
            rows.append(tokens)

    return rows


def parse_sql_values(sql_content: str, table_name: str) -> list[list[str | None]]:
    """
    Parse all INSERT INTO `table_name` VALUES (...) statements from SQL dump.
    Returns list of rows, each row is a list of values.
    """
    all_rows: list[list[str | None]] = []

    # Match INSERT INTO `table_name` VALUES or INSERT INTO table_name VALUES
    pattern = re.compile(
        rf"INSERT\s+INTO\s+[`'\"]?{re.escape(table_name)}[`'\"]?\s+"
        rf'(?:\([^)]*\)\s+)?VALUES\s*',
        re.IGNORECASE,
    )

    for match in pattern.finditer(sql_content):
        start = match.end()
        # Find the end of the statement (semicolon at top level)
        idx = start
        n = len(sql_content)
        in_quote = False
        escape_next = False
        depth = 0

        while idx < n:
            ch = sql_content[idx]
            if escape_next:
                escape_next = False
                idx += 1
                continue
            if ch == '\\':
                escape_next = True
                idx += 1
                continue
            if ch == "'":
                in_quote = not in_quote
            elif not in_quote:
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                elif ch == ';' and depth == 0:
                    break
            idx += 1

        values_str = sql_content[start:idx]
        rows = extract_rows_from_values(values_str)
        all_rows.extend(rows)

    return all_rows


# ---------------------------------------------------------------------------
# Data extraction helpers
# ---------------------------------------------------------------------------


def parse_datetime(val: str | None) -> datetime | None:
    """Parse MySQL datetime string to timezone-aware UTC datetime."""
    if not val or val.upper() == 'NULL' or val == '0000-00-00 00:00:00':
        return None
    try:
        dt = datetime.strptime(val.strip(), '%Y-%m-%d %H:%M:%S')
        return dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def parse_decimal(val: str | None) -> Decimal:
    """Parse decimal string, default 0."""
    if not val or val.upper() == 'NULL':
        return Decimal(0)
    try:
        return Decimal(val.strip())
    except InvalidOperation:
        return Decimal(0)


def parse_int(val: str | None, default: int = 0) -> int:
    """Parse int string, default 0."""
    if not val or val.upper() == 'NULL':
        return default
    try:
        return int(val.strip())
    except (ValueError, TypeError):
        return default


def parse_settings_json(val: str | None) -> dict | None:
    """Parse JSON settings field."""
    if not val:
        return None
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return None


def extract_telegram_info(settings: dict | None) -> dict | None:
    """
    Extract telegram info from SHM user settings.
    Returns dict with chat_id, user_id, login, first_name, last_name or None.
    """
    if not settings:
        return None
    tg = settings.get('telegram')
    if not tg:
        return None
    chat_id = tg.get('chat_id') or tg.get('user_id')
    if not chat_id:
        return None
    try:
        chat_id = int(chat_id)
    except (ValueError, TypeError):
        return None
    return {
        'chat_id': chat_id,
        'login': tg.get('login'),
        'first_name': tg.get('first_name'),
        'last_name': tg.get('last_name'),
    }


# ---------------------------------------------------------------------------
# Main migration logic
# ---------------------------------------------------------------------------


async def run_migration(sql_path: str, dry_run: bool = False) -> None:
    print(f'=== Миграция из SHM {"(DRY RUN)" if dry_run else ""} ===')
    print(f'SQL dump: {sql_path}')
    print()

    # --- Read SQL dump ---
    print('Чтение SQL dump...')
    sql_content = Path(sql_path).read_text(encoding='utf-8', errors='replace')
    print(f'  Размер файла: {len(sql_content):,} байт')
    print()

    # --- Parse tables ---
    print('Парсинг таблицы users...')
    shm_users = parse_sql_values(sql_content, 'users')
    print(f'  Найдено записей: {len(shm_users)}')

    print('Парсинг таблицы user_services...')
    shm_services = parse_sql_values(sql_content, 'user_services')
    print(f'  Найдено записей: {len(shm_services)}')

    print('Парсинг таблицы pays_history...')
    shm_pays = parse_sql_values(sql_content, 'pays_history')
    print(f'  Найдено записей: {len(shm_pays)}')

    print('Парсинг таблицы withdraw_history...')
    shm_withdraws = parse_sql_values(sql_content, 'withdraw_history')
    print(f'  Найдено записей: {len(shm_withdraws)}')
    print()

    # --- Build service index: shm_user_id -> best service record ---
    print('Построение индекса подписок (user_services)...')
    services_by_user: dict[int, list] = {}
    for row in shm_services:
        if len(row) < 12:
            continue
        uid = parse_int(row[1])
        if uid <= 0:
            continue
        services_by_user.setdefault(uid, []).append(row)

    # For each user, pick the service with MAX(expire)
    best_service: dict[int, list] = {}
    for uid, svc_list in services_by_user.items():
        best = None
        best_expire = None
        for svc in svc_list:
            expire_dt = parse_datetime(svc[6])
            if best is None or (expire_dt is not None and (best_expire is None or expire_dt > best_expire)):
                best = svc
                best_expire = expire_dt
        if best is not None:
            best_service[uid] = best

    # --- Build payment/withdrawal indices ---
    pays_by_user: dict[int, list] = {}
    for row in shm_pays:
        if len(row) < 7:
            continue
        uid = parse_int(row[1])
        money = parse_decimal(row[3])
        if uid <= 0 or money <= 0:
            continue
        pays_by_user.setdefault(uid, []).append(row)

    withdraws_by_user: dict[int, list] = {}
    for row in shm_withdraws:
        if len(row) < 13:
            continue
        uid = parse_int(row[1])
        total = parse_decimal(row[9])
        if uid <= 0 or total <= 0:
            continue
        withdraws_by_user.setdefault(uid, []).append(row)

    # --- Filter valid users ---
    print('Фильтрация пользователей с Telegram...')
    valid_users: list[tuple[list, dict]] = []  # (shm_row, tg_info)
    seen_chat_ids: set[int] = set()
    skipped_no_tg = 0
    skipped_admin = 0
    skipped_dup_tg = 0

    for row in shm_users:
        if len(row) < 22:
            continue
        shm_user_id = parse_int(row[0])
        if shm_user_id == 1:
            skipped_admin += 1
            continue

        settings = parse_settings_json(row[21])
        tg_info = extract_telegram_info(settings)
        if not tg_info:
            skipped_no_tg += 1
            continue

        chat_id = tg_info['chat_id']
        if chat_id in seen_chat_ids:
            skipped_dup_tg += 1
            print(f'  WARN: Дублирующий telegram_id={chat_id} у SHM user_id={shm_user_id}, пропуск')
            continue

        seen_chat_ids.add(chat_id)
        valid_users.append((row, tg_info))

    print(f'  Валидных пользователей: {len(valid_users)}')
    print(f'  Пропущено (нет Telegram): {skipped_no_tg}')
    print(f'  Пропущено (admin user_id=1): {skipped_admin}')
    print(f'  Пропущено (дубликат telegram_id): {skipped_dup_tg}')
    print()

    if dry_run:
        # --- Dry run stats ---
        total_deposits = sum(len(pays_by_user.get(parse_int(r[0]), [])) for r, _ in valid_users)
        total_withdrawals = sum(len(withdraws_by_user.get(parse_int(r[0]), [])) for r, _ in valid_users)
        total_subs = sum(1 for r, _ in valid_users if parse_int(r[0]) in best_service)
        referrals_count = sum(1 for r, _ in valid_users if parse_int(r[1]) > 0 and parse_int(r[1]) != 1)

        print('=== DRY RUN: Статистика ===')
        print(f'  Пользователей к миграции: {len(valid_users)}')
        print(f'  Подписок к миграции: {total_subs}')
        print(f'  Транзакций (депозиты): {total_deposits}')
        print(f'  Транзакций (списания): {total_withdrawals}')
        print(f'  Реферальных связей: {referrals_count}')
        print('=== Завершено (без записи в БД) ===')
        return

    # --- Actual migration ---
    now = datetime.now(UTC)

    stats = {
        'users_created': 0,
        'users_skipped': 0,
        'subscriptions_created': 0,
        'subscriptions_skipped': 0,
        'deposits_created': 0,
        'deposits_skipped': 0,
        'withdrawals_created': 0,
        'withdrawals_skipped': 0,
        'referrals_set': 0,
        'errors': 0,
    }

    # Mapping: shm_user_id -> bedolaga_user_id
    shm_to_bedolaga: dict[int, int] = {}
    # Mapping: shm_user_id -> partner_id (for referral second pass)
    shm_partner_map: dict[int, int] = {}

    print('=== Фаза 1: Создание пользователей, подписок и транзакций ===')

    async with AsyncSessionLocal() as db:
        for idx, (row, tg_info) in enumerate(valid_users):
            shm_user_id = parse_int(row[0])
            partner_id = parse_int(row[1])
            chat_id = tg_info['chat_id']

            if (idx + 1) % 100 == 0:
                print(f'  Обработано: {idx + 1}/{len(valid_users)}')

            try:
                # Check if user already exists
                existing = await db.execute(select(User).where(User.telegram_id == chat_id))
                existing_user = existing.scalar_one_or_none()

                if existing_user:
                    stats['users_skipped'] += 1
                    shm_to_bedolaga[shm_user_id] = existing_user.id
                    if partner_id > 0 and partner_id != 1:
                        shm_partner_map[shm_user_id] = partner_id
                    # Still try to create missing transactions for existing users
                    await _create_transactions(
                        db, existing_user.id, shm_user_id, pays_by_user, withdraws_by_user, stats
                    )
                    continue

                # Parse user fields
                shm_balance = parse_decimal(row[8])
                shm_bonus = parse_decimal(row[17])
                balance_kopeks = int((shm_balance + shm_bonus) * 100)
                block = parse_int(row[12])
                created_at = parse_datetime(row[5]) or now

                username = tg_info.get('login')
                if username:
                    username = username.lstrip('@')[:255]

                first_name = tg_info.get('first_name')
                if first_name:
                    first_name = first_name[:255]

                last_name = tg_info.get('last_name')
                if last_name:
                    last_name = last_name[:255]

                user = User(
                    telegram_id=chat_id,
                    username=username or None,
                    first_name=first_name or None,
                    last_name=last_name or None,
                    balance_kopeks=balance_kopeks,
                    status=UserStatus.ACTIVE.value if block == 0 else UserStatus.BLOCKED.value,
                    created_at=created_at,
                    language='ru',
                )
                db.add(user)
                await db.flush()  # Get user.id

                shm_to_bedolaga[shm_user_id] = user.id
                stats['users_created'] += 1

                if partner_id > 0 and partner_id != 1:
                    shm_partner_map[shm_user_id] = partner_id

                # --- Create subscription ---
                svc = best_service.get(shm_user_id)
                if svc:
                    expire_dt = parse_datetime(svc[6])
                    svc_created = parse_datetime(svc[4]) or created_at

                    if expire_dt:
                        sub_status = (
                            SubscriptionStatus.ACTIVE.value if expire_dt > now else SubscriptionStatus.EXPIRED.value
                        )

                        # Check existing subscription
                        existing_sub = await db.execute(select(Subscription).where(Subscription.user_id == user.id))
                        if existing_sub.scalar_one_or_none() is None:
                            subscription = Subscription(
                                user_id=user.id,
                                status=sub_status,
                                end_date=expire_dt,
                                device_limit=3,
                                is_trial=False,
                                created_at=svc_created,
                                traffic_limit_gb=0,
                            )
                            db.add(subscription)
                            stats['subscriptions_created'] += 1
                        else:
                            stats['subscriptions_skipped'] += 1
                    else:
                        stats['subscriptions_skipped'] += 1
                else:
                    stats['subscriptions_skipped'] += 1

                # --- Create transactions ---
                await _create_transactions(db, user.id, shm_user_id, pays_by_user, withdraws_by_user, stats)

            except Exception as e:
                stats['errors'] += 1
                print(f'  ОШИБКА при обработке SHM user_id={shm_user_id}: {e}')
                continue

        # Commit after all users
        print('  Коммит в БД...')
        await db.commit()

    print(f'  Создано пользователей: {stats["users_created"]}')
    print(f'  Пропущено (уже существуют): {stats["users_skipped"]}')
    print()

    # --- Phase 2: Referrals ---
    print('=== Фаза 2: Установка реферальных связей ===')
    async with AsyncSessionLocal() as db:
        for shm_user_id, partner_shm_id in shm_partner_map.items():
            bedolaga_user_id = shm_to_bedolaga.get(shm_user_id)
            bedolaga_referrer_id = shm_to_bedolaga.get(partner_shm_id)

            if not bedolaga_user_id or not bedolaga_referrer_id:
                continue
            if bedolaga_user_id == bedolaga_referrer_id:
                continue

            try:
                result = await db.execute(select(User).where(User.id == bedolaga_user_id))
                user = result.scalar_one_or_none()
                if user and not user.referred_by_id:
                    user.referred_by_id = bedolaga_referrer_id
                    stats['referrals_set'] += 1
            except Exception as e:
                print(f'  ОШИБКА referral SHM {shm_user_id} -> {partner_shm_id}: {e}')

        await db.commit()

    print(f'  Реферальных связей установлено: {stats["referrals_set"]}')
    print()

    # --- Summary ---
    print('=' * 50)
    print('=== ИТОГИ МИГРАЦИИ ===')
    print('=' * 50)
    print(f'  Пользователей создано:        {stats["users_created"]}')
    print(f'  Пользователей пропущено:      {stats["users_skipped"]}')
    print(f'  Подписок создано:             {stats["subscriptions_created"]}')
    print(f'  Подписок пропущено:           {stats["subscriptions_skipped"]}')
    print(f'  Транзакций (депозиты):        {stats["deposits_created"]}')
    print(f'  Транзакций (деп. пропущено):  {stats["deposits_skipped"]}')
    print(f'  Транзакций (списания):        {stats["withdrawals_created"]}')
    print(f'  Транзакций (спис. пропущено): {stats["withdrawals_skipped"]}')
    print(f'  Реферальных связей:           {stats["referrals_set"]}')
    print(f'  Ошибок:                       {stats["errors"]}')
    print('=' * 50)


async def _create_transactions(
    db,
    bedolaga_user_id: int,
    shm_user_id: int,
    pays_by_user: dict[int, list],
    withdraws_by_user: dict[int, list],
    stats: dict[str, int],
) -> None:
    """Create deposit and withdrawal transactions for a user."""
    # Deposits
    for pay_row in pays_by_user.get(shm_user_id, []):
        pay_id = pay_row[0]
        pay_system_id = pay_row[2]
        money = parse_decimal(pay_row[3])
        pay_date = parse_datetime(pay_row[4])
        amount_kopeks = int(money * 100)
        ext_id = f'shm_dep_{pay_id}'

        # Check for existing
        existing = await db.execute(
            select(Transaction).where(
                Transaction.external_id == ext_id,
                Transaction.payment_method == 'shm_migration',
            )
        )
        if existing.scalar_one_or_none():
            stats['deposits_skipped'] += 1
            continue

        description = f'Миграция из SHM (pay_system_id={pay_system_id})'

        tx = Transaction(
            user_id=bedolaga_user_id,
            type=TransactionType.DEPOSIT.value,
            amount_kopeks=amount_kopeks,
            description=description,
            payment_method='shm_migration',
            external_id=ext_id,
            is_completed=True,
            created_at=pay_date or datetime.now(UTC),
            completed_at=pay_date or datetime.now(UTC),
        )
        db.add(tx)
        stats['deposits_created'] += 1

    # Withdrawals
    for wdw_row in withdraws_by_user.get(shm_user_id, []):
        wdw_id = wdw_row[0]
        total = parse_decimal(wdw_row[9])
        wdw_date = parse_datetime(wdw_row[2])
        amount_kopeks = int(total * 100)
        ext_id = f'shm_wdw_{wdw_id}'

        # Check for existing
        existing = await db.execute(
            select(Transaction).where(
                Transaction.external_id == ext_id,
                Transaction.payment_method == 'shm_migration',
            )
        )
        if existing.scalar_one_or_none():
            stats['withdrawals_skipped'] += 1
            continue

        tx = Transaction(
            user_id=bedolaga_user_id,
            type=TransactionType.SUBSCRIPTION_PAYMENT.value,
            amount_kopeks=amount_kopeks,
            description='Оплата подписки (SHM)',
            payment_method='shm_migration',
            external_id=ext_id,
            is_completed=True,
            created_at=wdw_date or datetime.now(UTC),
            completed_at=wdw_date or datetime.now(UTC),
        )
        db.add(tx)
        stats['withdrawals_created'] += 1


async def sync_with_remnawave() -> None:
    """Фаза 3: Синхронизация с Remnawave — тянет remnawave_uuid и реальные даты подписок."""
    print()
    print('=== Фаза 3: Синхронизация с Remnawave ===')
    print('  Загружаем всех пользователей из Remnawave...')

    try:
        from app.services.remnawave_service import RemnaWaveService

        service = RemnaWaveService()
        async with AsyncSessionLocal() as db:
            result = await service.sync_users_from_panel(db, sync_type='all')
            print(f'  Создано новых: {result.get("created", 0)}')
            print(f'  Обновлено:     {result.get("updated", 0)}')
            print(f'  Ошибок:        {result.get("errors", 0)}')
        print('  ✅ Синхронизация с Remnawave завершена!')
        print('     remnawave_uuid и даты подписок обновлены из реальных данных панели.')
    except Exception as e:
        print(f'  ❌ Ошибка синхронизации с Remnawave: {e}')
        print('     Запустите sync вручную из бота (Админ → Remnawave → Синхронизация).')


def main() -> None:
    parser = argparse.ArgumentParser(description='Миграция пользователей из SHM в Bedolaga Bot')
    parser.add_argument('--sql', required=True, help='Путь к SQL dump файлу SHM')
    parser.add_argument('--dry-run', action='store_true', help='Показать статистику без записи в БД')
    parser.add_argument('--no-remnawave-sync', action='store_true', help='Пропустить синхронизацию с Remnawave')
    args = parser.parse_args()

    if not Path(args.sql).exists():
        print(f'ОШИБКА: Файл не найден: {args.sql}')
        sys.exit(1)

    asyncio.run(run_migration(args.sql, dry_run=args.dry_run))

    if not args.dry_run and not args.no_remnawave_sync:
        asyncio.run(sync_with_remnawave())


if __name__ == '__main__':
    main()
