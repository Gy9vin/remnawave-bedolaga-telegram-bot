# tests/crud/test_google_migration_crud.py
# The project conftest stubs `aiosqlite` with an empty module so that the
# app's regular imports don't fail in environments without it.  This test
# actually needs the real driver, so we restore it before SQLAlchemy loads.
import sys as _sys
_sys.modules.pop('aiosqlite', None)
import aiosqlite as _aiosqlite_real  # noqa: F401  (ensures real module is loaded)
_sys.modules['aiosqlite'] = _aiosqlite_real

import pytest
import pytest_asyncio
from sqlalchemy import JSON, Column, Integer, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database.models import User
from app.database.crud.user import get_google_linked_users, get_google_migration_stats


def _patch_jsonb_for_sqlite():
    """Replace JSONB columns with JSON on the User table so SQLite can handle them."""
    table = User.__table__
    for col in list(table.columns):
        if isinstance(col.type, JSONB):
            col.type = JSON()


@pytest_asyncio.fixture
async def session():
    _patch_jsonb_for_sqlite()
    engine = create_async_engine('sqlite+aiosqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _add(s, **kw):
    defaults = dict(referral_code=f"r{kw.get('email','x')}", balance_kopeks=0, promo_group_id=None)
    defaults.update(kw)
    u = User(**defaults)
    s.add(u)
    await s.commit()
    return u


@pytest.mark.asyncio
async def test_get_google_linked_users_filters(session):
    await _add(session, email='a@gmail.com', google_id='111', auth_type='google')
    await _add(session, email='b@gmail.com', google_id='222', auth_type='telegram', telegram_id=5)
    await _add(session, email=None, google_id='333', auth_type='google')  # no email -> excluded
    await _add(session, email='c@ya.ru', google_id=None, auth_type='email')  # no google -> excluded

    users = await get_google_linked_users(session)
    emails = {u.email for u in users}
    assert emails == {'a@gmail.com', 'b@gmail.com'}


@pytest.mark.asyncio
async def test_stats_counts(session):
    await _add(session, email='a@gmail.com', google_id='111', auth_type='google')
    await _add(session, email='b@gmail.com', google_id='222', auth_type='telegram', telegram_id=5)
    await _add(session, email='c@gmail.com', google_id='333', auth_type='google', password_hash='x')

    stats = await get_google_migration_stats(session)
    assert stats == {'total': 3, 'google_only': 2, 'with_password': 1}
