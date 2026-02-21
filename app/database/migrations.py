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


async def _needs_auto_stamp() -> bool:
    """Check if DB has existing tables but no alembic_version (transition from universal_migration)."""
    from app.database.database import engine

    async with engine.connect() as conn:
        has_alembic = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table('alembic_version'))
        if has_alembic:
            return False
        has_users = await conn.run_sync(lambda sync_conn: inspect(sync_conn).has_table('users'))
        return has_users


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


async def run_alembic_upgrade() -> None:
    """Run ``alembic upgrade head``, auto-stamping existing databases first."""
    import asyncio

    if await _needs_auto_stamp():
        logger.warning(
            'Обнаружена существующая БД без alembic_version — автоматический stamp 0001 (переход с universal_migration)'
        )
        await _stamp_alembic_revision(_INITIAL_REVISION)
    elif await _has_orphaned_revision():
        logger.warning('Принудительный stamp head — старая ревизия несовместима с текущими миграциями')
        await _stamp_alembic_revision('head')

    cfg = _get_alembic_config()
    loop = asyncio.get_running_loop()
    # run_in_executor offloads to a thread where env.py can safely
    # call asyncio.run() to create its own event loop.
    await loop.run_in_executor(None, command.upgrade, cfg, 'head')
    logger.info('Alembic миграции применены')


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
