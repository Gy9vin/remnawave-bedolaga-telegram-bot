"""Programmatic Alembic migration runner for bot startup."""

from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text


logger = structlog.get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _PROJECT_ROOT / 'alembic.ini'


def _get_alembic_config() -> Config:
    """Build Alembic Config pointing at the project root."""
    from app.config import settings

    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option('sqlalchemy.url', settings.get_database_url())
    return cfg


async def _get_current_db_revision() -> str | None:
    """Read current alembic revision from DB, or None if table doesn't exist."""
    from app.database.database import engine

    async with engine.connect() as conn:
        has_alembic = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table('alembic_version'))
        if not has_alembic:
            return None
        result = await conn.execute(text('SELECT version_num FROM alembic_version LIMIT 1'))
        row = result.first()
        return row[0] if row else None


async def _detect_db_state() -> str:
    """Detect database state: 'fresh', 'legacy', or 'managed'.

    - fresh: no tables at all — brand new database
    - legacy: has tables but no alembic_version (transition from universal_migration)
    - managed: has alembic_version — already managed by Alembic
    """
    from app.database.database import engine

    async with engine.connect() as conn:
        has_alembic = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table('alembic_version'))
        if has_alembic:
            return 'managed'
        has_users = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table('users'))
        return 'legacy' if has_users else 'fresh'


async def _has_orphaned_revision() -> bool:
    """Check if DB's alembic_version points to a revision that doesn't exist in migration files."""
    current = await _get_current_db_revision()
    if current is None:
        return False

    cfg = _get_alembic_config()
    script = ScriptDirectory.from_config(cfg)
    known_revisions = {rev.revision for rev in script.walk_revisions()}
    if current not in known_revisions:
        logger.warning(
            'Обнаружена устаревшая ревизия в alembic_version — ревизия не найдена в миграциях',
            db_revision=current,
            known_revisions=sorted(known_revisions),
        )
        return True
    return False


_INITIAL_REVISION = '0001'

# Ревизии нашего старого 0046/0047, которые upstream теперь использует для других миграций.
# Если DB на этих ревизиях, но news_articles не существует — значит нужно откатить stamp
# до 0045 чтобы upstream-миграции 0046-0049 запустились корректно.
_UPSTREAM_REBASE_REVISIONS = frozenset({'0046', '0047', '0048', '0049'})


async def _needs_news_migration_rebase() -> bool:
    """Проверить: DB на 0046/0047 (наши старые), но news_articles не существует.

    После переименования наших миграций в 9001/9002, upstream занял ревизии 0046-0049.
    Серверы, которые были на нашем старом 0047, имеют alembic_version='0047', но
    upstream-миграция 0046 (news_articles) никогда не запускалась. Нужно rebase до 0045.
    """
    from app.database.database import engine

    current = await _get_current_db_revision()
    if current not in _UPSTREAM_REBASE_REVISIONS:
        return False

    async with engine.connect() as conn:
        has_news = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table('news_articles'))

    if not has_news:
        return True
    return False


# Паттерн наших "правильных" файлов миграций: 0001_..., 0002_... и т.д.
# Наши кастомные миграции с высокими ID (9001+) никогда не совпадут с upstream.
import re as _re


_OUR_MIGRATION_PATTERN = _re.compile(r'^(00\d{2}_|9\d{3}_)')


def _cleanup_foreign_migration_files() -> int:
    """Удалить посторонние файлы миграций (upstream hash-based) из директории versions.

    Upstream BEDOLAGA-DEV использует hash-based ID (например cbd1be472f3d),
    а наш форк использует числовые ID (0001, 0002, ...).
    Наши кастомные миграции используют ID 9001+ чтобы никогда не конфликтовать с upstream.
    При деплое на сервер, куда ранее устанавливался upstream, эти файлы могут смешаться.
    """
    cfg = _get_alembic_config()
    script = ScriptDirectory.from_config(cfg)
    versions_dir = Path(script.dir) / 'versions'

    removed = 0
    for f in versions_dir.glob('*.py'):
        if f.name == '__init__.py':
            continue
        if not _OUR_MIGRATION_PATTERN.match(f.name):
            logger.warning('Удаляю посторонний файл миграции (upstream)', file=f.name)
            f.unlink()
            removed += 1

    if removed:
        logger.info('Очищено посторонних файлов миграций', count=removed)
    return removed


async def _bootstrap_fresh_db() -> None:
    """Bootstrap a fresh database: create all tables from models and stamp at head.

    On a fresh DB, running all migrations sequentially would fail because
    migration 0001 uses Base.metadata.create_all() which creates ALL tables
    from the current models.py (including columns/constraints/indexes added
    by later migrations), and then those later migrations try to re-create
    the same objects.  Instead, we create the full schema directly and stamp
    the migration history at HEAD so Alembic considers all migrations applied.
    """
    from app.database.database import engine
    from app.database.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info('Свежая БД: все таблицы созданы из моделей')


async def _ensure_critical_columns() -> None:
    """Добавить критичные колонки напрямую через SQL (ADD COLUMN IF NOT EXISTS).

    Страховочный механизм: запускается после alembic upgrade, гарантирует наличие
    колонок из upstream 0042/0045 которые могли не применяться при проблемах с миграцией.
    Использует PostgreSQL-синтаксис IF NOT EXISTS — идемпотентно, безопасно.
    """
    from app.database.database import engine

    critical = [
        # (таблица, колонка, тип PostgreSQL)
        ('guest_purchases', 'retry_count', 'INTEGER NOT NULL DEFAULT 0'),
        ('guest_purchases', 'receipt_uuid', 'VARCHAR(255)'),
        ('guest_purchases', 'receipt_created_at', 'TIMESTAMPTZ'),
    ]

    try:
        async with engine.begin() as conn:
            dialect = engine.dialect.name
            if dialect != 'postgresql':
                return  # SQLite: миграция справится сама

            for table, column, col_type in critical:
                try:
                    await conn.execute(text(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_type}'))
                except Exception as e:
                    logger.warning(
                        'Не удалось добавить страховочную колонку',
                        table=table,
                        column=column,
                        error=str(e),
                    )

        logger.info('Страховочные колонки проверены/добавлены')
    except Exception as e:
        logger.error('Ошибка при добавлении страховочных колонок', error=str(e))


async def run_alembic_upgrade() -> None:
    """Run ``alembic upgrade head``, handling fresh and legacy databases."""
    import asyncio

    # Сначала чистим посторонние файлы (могут появиться при деплое поверх upstream)
    _cleanup_foreign_migration_files()

    db_state = await _detect_db_state()

    if db_state == 'fresh':
        logger.warning('Обнаружена пустая БД — создание схемы из моделей + stamp head')
        await _bootstrap_fresh_db()
        await _stamp_alembic_revision('head')
        await _ensure_critical_columns()
        return

    if db_state == 'legacy':
        logger.warning(
            'Обнаружена существующая БД без alembic_version — автоматический stamp 0001 (переход с universal_migration)'
        )
        await _stamp_alembic_revision(_INITIAL_REVISION)
    elif await _has_orphaned_revision():
        logger.warning('Принудительный stamp head — старая ревизия несовместима с текущими миграциями')
        await _stamp_alembic_revision('head')
    elif await _needs_news_migration_rebase():
        logger.warning(
            'Обнаружена БД на ревизии 0046/0047 без таблицы news_articles — '
            'stamp 0045 для повторного запуска upstream-миграций 0046-0049'
        )
        await _stamp_alembic_revision('0045')

    cfg = _get_alembic_config()
    loop = asyncio.get_running_loop()
    # run_in_executor offloads to a thread where env.py can safely
    # call asyncio.run() to create its own event loop.
    await loop.run_in_executor(None, command.upgrade, cfg, 'head')
    logger.info('Alembic миграции применены')

    # Страховочная проверка: гарантируем наличие критичных колонок
    await _ensure_critical_columns()


async def stamp_alembic_head() -> None:
    """Stamp the DB as being at head without running migrations (for existing DBs)."""
    await _stamp_alembic_revision('head')


async def _stamp_alembic_revision(revision: str) -> None:
    """Stamp the DB at a specific revision without running migrations."""
    import asyncio

    cfg = _get_alembic_config()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, command.stamp, cfg, revision)
    logger.info('Alembic: база отмечена как актуальная', revision=revision)
