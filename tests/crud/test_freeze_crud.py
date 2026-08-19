# tests/crud/test_freeze_crud.py
#
# Тесты для фильтров заморозки в CRUD-слое:
#   - get_expired_subscriptions не возвращает замороженные подписки
#   - get_subscriptions_for_auto_unfreeze возвращает нужное
#
# Паттерн: SQLite in-memory с патчем JSONB, аналогично test_broadcast_blocked_report.py.
import sys as _sys
_sys.modules.pop('aiosqlite', None)
import aiosqlite as _aiosqlite_real  # noqa: F401
_sys.modules['aiosqlite'] = _aiosqlite_real

import secrets
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database.models import Subscription, Tariff, User
from app.database.crud.subscription import (
    get_expired_subscriptions,
    get_subscriptions_for_auto_unfreeze,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_jsonb_for_sqlite():
    """Replace JSONB columns with JSON on all tables so SQLite can handle them."""
    for table in (User.__table__, Subscription.__table__, Tariff.__table__):
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
    async with engine.begin() as conn:
        await conn.run_sync(Tariff.__table__.create)
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(Subscription.__table__.create)

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _add_user(s: AsyncSession, uid: int) -> User:
    u = User(
        id=uid,
        telegram_id=uid * 100,
        auth_type='telegram',
        referral_code=f'rc{uid}',
        balance_kopeks=0,
        status='active',
    )
    s.add(u)
    await s.flush()
    return u


async def _add_sub(
    s: AsyncSession,
    user_id: int,
    *,
    status: str = 'active',
    end_date: datetime,
    is_frozen: bool = False,
    frozen_auto_unfreeze_at: datetime | None = None,
) -> Subscription:
    sub = Subscription(
        user_id=user_id,
        status=status,
        end_date=end_date,
        is_frozen=is_frozen,
        frozen_auto_unfreeze_at=frozen_auto_unfreeze_at,
        remnawave_short_id=secrets.token_hex(8),
    )
    s.add(sub)
    await s.flush()
    return sub


# ---------------------------------------------------------------------------
# Tests: get_subscriptions_for_auto_unfreeze
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_unfreeze_returns_overdue_frozen(session: AsyncSession):
    """Замороженная подписка с frozen_auto_unfreeze_at в прошлом — возвращается."""
    now = datetime.now(UTC)
    await _add_user(session, uid=1)
    sub = await _add_sub(
        session,
        user_id=1,
        status='disabled',
        end_date=now + timedelta(days=10),
        is_frozen=True,
        frozen_auto_unfreeze_at=now - timedelta(hours=1),
    )
    await session.commit()

    result = await get_subscriptions_for_auto_unfreeze(session, now)
    assert any(r.id == sub.id for r in result), "Ожидали найти замороженную подписку с истёкшей авто-разморозкой"


@pytest.mark.asyncio
async def test_auto_unfreeze_skips_future_unfreeze(session: AsyncSession):
    """Замороженная подписка с frozen_auto_unfreeze_at в будущем — НЕ возвращается."""
    now = datetime.now(UTC)
    await _add_user(session, uid=2)
    sub = await _add_sub(
        session,
        user_id=2,
        status='disabled',
        end_date=now + timedelta(days=10),
        is_frozen=True,
        frozen_auto_unfreeze_at=now + timedelta(days=2),
    )
    await session.commit()

    result = await get_subscriptions_for_auto_unfreeze(session, now)
    assert not any(r.id == sub.id for r in result), "Подписка с будущим unfreeze_at не должна возвращаться"


@pytest.mark.asyncio
async def test_auto_unfreeze_skips_not_frozen(session: AsyncSession):
    """Незамороженная подписка — НЕ возвращается, даже если дата в прошлом."""
    now = datetime.now(UTC)
    await _add_user(session, uid=3)
    sub = await _add_sub(
        session,
        user_id=3,
        status='active',
        end_date=now + timedelta(days=10),
        is_frozen=False,
        frozen_auto_unfreeze_at=now - timedelta(hours=1),
    )
    await session.commit()

    result = await get_subscriptions_for_auto_unfreeze(session, now)
    assert not any(r.id == sub.id for r in result), "Незамороженная подписка не должна возвращаться"


# ---------------------------------------------------------------------------
# Tests: get_expired_subscriptions — фильтр is_frozen
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expired_subscriptions_skips_frozen(session: AsyncSession):
    """Замороженная истёкшая подписка не попадает в свип expired."""
    now = datetime.now(UTC)
    await _add_user(session, uid=4)
    sub_frozen = await _add_sub(
        session,
        user_id=4,
        status='active',
        end_date=now - timedelta(hours=2),
        is_frozen=True,
    )
    await session.commit()

    result = await get_expired_subscriptions(session)
    assert not any(r.id == sub_frozen.id for r in result), \
        "Замороженная подписка не должна попасть в get_expired_subscriptions"


@pytest.mark.asyncio
async def test_expired_subscriptions_returns_normal_expired(session: AsyncSession):
    """Обычная (незамороженная) истёкшая активная подписка возвращается."""
    now = datetime.now(UTC)
    await _add_user(session, uid=5)
    sub_normal = await _add_sub(
        session,
        user_id=5,
        status='active',
        end_date=now - timedelta(hours=2),
        is_frozen=False,
    )
    await session.commit()

    result = await get_expired_subscriptions(session)
    assert any(r.id == sub_normal.id for r in result), \
        "Обычная истёкшая подписка должна возвращаться"


# ---------------------------------------------------------------------------
# Tests: get_subscriptions_for_auto_unfreeze — заблокированные пользователи
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_unfreeze_skips_blocked_user(session: AsyncSession):
    """Заблокированный пользователь с наступившей авто-разморозкой НЕ возвращается."""
    now = datetime.now(UTC)
    # Создаём пользователя со статусом 'blocked'
    u = User(
        id=10,
        telegram_id=1000,
        auth_type='telegram',
        referral_code='rc10',
        balance_kopeks=0,
        status='blocked',
    )
    session.add(u)
    await session.flush()

    sub = await _add_sub(
        session,
        user_id=10,
        status='disabled',
        end_date=now + timedelta(days=10),
        is_frozen=True,
        frozen_auto_unfreeze_at=now - timedelta(hours=1),
    )
    await session.commit()

    result = await get_subscriptions_for_auto_unfreeze(session, now)
    assert not any(r.id == sub.id for r in result), \
        "Заблокированный пользователь не должен получать авто-разморозку"
