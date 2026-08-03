"""Удаление пользователя не должно обнулять NOT NULL FK у дочерних записей.

`await db.delete(user)` заставляет SQLAlchemy подгрузить каждую коллекцию-
потомка на стороне User и, если у связи нет delete-каскада, «отвязать» строки
через `UPDATE ... SET user_id = NULL`. Для колонок с `nullable=False` это
NotNullViolationError, и вся транзакция удаления откатывается:

    null value in column "user_id" of relation "user_clients"
    violates not-null constraint

Сервисы удаления (blocked_users_service, user_service) пытаются обойти это,
удаляя потомков вручную, но списки разъезжаются с моделями (user_clients,
user_roles, platega/lava_subscriptions, … не удалялись нигде). Инвариант ниже
чинит проблему в корне: каскад задаётся на модели, а не в каждом сервисе.
"""

from __future__ import annotations

import sys as _sys


_sys.modules.pop('aiosqlite', None)
import aiosqlite as _a  # noqa: E402, F401


_sys.modules['aiosqlite'] = _a

from datetime import UTC, datetime  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import JSON, event, inspect, select  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.orm import configure_mappers  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database.models import Base, User, UserClient  # noqa: E402


def _patch_jsonb():
    """sqlite не знает JSONB — подменяем на переносимый JSON во всех таблицах."""
    for table in Base.metadata.tables.values():
        for col in list(table.columns):
            if isinstance(col.type, JSONB):
                col.type = JSON()


@pytest_asyncio.fixture
async def session():
    _patch_jsonb()
    engine = create_async_engine('sqlite+aiosqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)

    # SQLite игнорирует FK (и ON DELETE CASCADE), пока их явно не включить —
    # без этого тест не воспроизвёл бы поведение postgres.
    @event.listens_for(engine.sync_engine, 'connect')
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute('PRAGMA foreign_keys=ON')

    # Удаление User тянет за собой загрузку ВСЕХ дочерних коллекций, поэтому
    # схема нужна целиком, а не только users + user_clients.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_delete_user_removes_user_clients(session):
    user = User(telegram_id=7416977186, username='jestoks')
    session.add(user)
    await session.commit()

    session.add(UserClient(user_id=user.id, app_name='Happ', last_seen_at=datetime.now(UTC)))
    await session.commit()

    await session.delete(user)
    await session.commit()

    assert (await session.execute(select(UserClient))).scalars().all() == []
    assert (await session.execute(select(User))).scalars().all() == []


def test_all_not_null_user_children_have_delete_cascade():
    """Ни одна NOT NULL связь User → потомок не должна обнулять FK при удалении."""
    configure_mappers()
    offenders = []

    for rel in inspect(User).relationships:
        if rel.direction.name != 'ONETOMANY' or 'delete' in rel.cascade:
            continue
        child_cols = [remote for _local, remote in rel.local_remote_pairs if remote.table is not User.__table__]
        if child_cols and not any(col.nullable for col in child_cols):
            offenders.append(f'User.{rel.key} → {rel.mapper.class_.__name__}')

    assert offenders == [], (
        'Связи без delete-каскада при NOT NULL FK — db.delete(user) упадёт на NotNullViolation: '
        + ', '.join(offenders)
    )
