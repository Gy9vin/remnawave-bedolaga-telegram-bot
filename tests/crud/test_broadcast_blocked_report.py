# tests/crud/test_broadcast_blocked_report.py
#
# The project conftest stubs `aiosqlite` with an empty module so that the
# app's regular imports don't fail in environments without it. This test
# actually needs the real driver, so we restore it before SQLAlchemy loads.
import sys as _sys
_sys.modules.pop('aiosqlite', None)
import aiosqlite as _aiosqlite_real  # noqa: F401  (ensures real module is loaded)
_sys.modules['aiosqlite'] = _aiosqlite_real

import secrets
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import JSON, Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database.models import BroadcastHistory, Subscription, Tariff, User
from app.database.crud.broadcast_reports import get_broadcast_blocked_active_subscribers


def _patch_jsonb_for_sqlite():
    """Replace JSONB columns with JSON on all tables so SQLite can handle them."""
    for table in (User.__table__, Subscription.__table__, BroadcastHistory.__table__):
        for col in list(table.columns):
            if isinstance(col.type, JSONB):
                col.type = JSON()


@pytest_asyncio.fixture
async def session():
    _patch_jsonb_for_sqlite()
    engine = create_async_engine(
        'sqlite+aiosqlite://',
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    # Create tables in FK-safe order: Tariff, User, Subscription, BroadcastHistory
    async with engine.begin() as conn:
        await conn.run_sync(Tariff.__table__.create)
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(Subscription.__table__.create)
        await conn.run_sync(BroadcastHistory.__table__.create)

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _add_user(s: AsyncSession, telegram_id: int, username: str | None = None) -> User:
    u = User(
        telegram_id=telegram_id,
        username=username,
        auth_type='telegram',
        referral_code=f'rc{telegram_id}',
        balance_kopeks=0,
    )
    s.add(u)
    await s.flush()  # populate u.id
    return u


async def _add_subscription(
    s: AsyncSession,
    user_id: int,
    tariff_id: int | None,
    status: str,
    end_date: datetime,
) -> Subscription:
    sub = Subscription(
        user_id=user_id,
        tariff_id=tariff_id,
        status=status,
        end_date=end_date,
        # remnawave_short_id is NOT NULL with server_default=''; supply explicitly for SQLite
        remnawave_short_id=secrets.token_hex(8),
    )
    s.add(sub)
    await s.flush()
    return sub


async def _add_broadcast(
    s: AsyncSession,
    blocked_user_ids: list[int] | None,
) -> BroadcastHistory:
    bh = BroadcastHistory(
        target_type='all',
        message_text='test',
        blocked_user_ids=blocked_user_ids,
        admin_name='admin',
        category='system',
        channel='telegram',
    )
    s.add(bh)
    await s.flush()
    return bh


@pytest.mark.asyncio
async def test_blocked_active_subscribers_basic(session: AsyncSession):
    """User A (active sub, in blocked list) is returned; B (expired) and C (not blocked) are not."""
    now = datetime.now(UTC)

    # Tariff
    tariff = Tariff(
        name='Pro',
        period_prices={},
        traffic_limit_gb=100,
        device_limit=1,
    )
    session.add(tariff)
    await session.flush()

    # User A: active subscription ending in ~30 days, IN blocked list
    user_a = await _add_user(session, telegram_id=100, username='alice')
    await _add_subscription(
        session, user_a.id, tariff.id, 'active', now + timedelta(days=30)
    )

    # User B: EXPIRED subscription, IN blocked list
    user_b = await _add_user(session, telegram_id=200, username='bob')
    await _add_subscription(
        session, user_b.id, tariff.id, 'expired', now - timedelta(days=5)
    )

    # User C: active subscription BUT not in blocked list
    user_c = await _add_user(session, telegram_id=300, username='carol')
    await _add_subscription(
        session, user_c.id, tariff.id, 'active', now + timedelta(days=60)
    )

    # BroadcastHistory with blocked_user_ids=[100, 200]
    bh = await _add_broadcast(session, blocked_user_ids=[100, 200])
    await session.commit()

    results = await get_broadcast_blocked_active_subscribers(session, bh.id)

    # Only user A should appear
    assert len(results) == 1
    row = results[0]
    assert row['telegram_id'] == 100
    assert row['username'] == 'alice'
    assert row['tariff_name'] == 'Pro'
    assert 28 <= row['days_left'] <= 31, f"Unexpected days_left={row['days_left']}"
    assert row['end_date'] is not None


@pytest.mark.asyncio
async def test_blocked_user_ids_none_returns_empty(session: AsyncSession):
    """Broadcast with blocked_user_ids=None must return []."""
    bh = await _add_broadcast(session, blocked_user_ids=None)
    await session.commit()

    results = await get_broadcast_blocked_active_subscribers(session, bh.id)
    assert results == []


@pytest.mark.asyncio
async def test_missing_broadcast_returns_empty(session: AsyncSession):
    """Non-existent broadcast_id must return []."""
    results = await get_broadcast_blocked_active_subscribers(session, broadcast_id=99999)
    assert results == []


@pytest.mark.asyncio
async def test_sorting_by_days_left(session: AsyncSession):
    """When multiple users are blocked with active subs, result is sorted soonest-first."""
    now = datetime.now(UTC)

    tariff = Tariff(name='Basic', period_prices={}, traffic_limit_gb=50, device_limit=1)
    session.add(tariff)
    await session.flush()

    user_x = await _add_user(session, telegram_id=111)
    await _add_subscription(session, user_x.id, tariff.id, 'active', now + timedelta(days=10))

    user_y = await _add_user(session, telegram_id=222)
    await _add_subscription(session, user_y.id, tariff.id, 'active', now + timedelta(days=5))

    bh = await _add_broadcast(session, blocked_user_ids=[111, 222])
    await session.commit()

    results = await get_broadcast_blocked_active_subscribers(session, bh.id)
    assert len(results) == 2
    # Y expires sooner (5 days) so should come first
    assert results[0]['telegram_id'] == 222
    assert results[1]['telegram_id'] == 111
