import sys as _sys
_sys.modules.pop('aiosqlite', None)
import aiosqlite as _a  # noqa: F401
_sys.modules['aiosqlite'] = _a

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import JSON, select  # noqa: F401
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database.models import SubscriptionEvent, User
from app.database.crud.subscription_event import get_subscription_purchase_timeline


def _patch_jsonb():
    for model in (User, SubscriptionEvent):
        for col in list(model.__table__.columns):
            if isinstance(col.type, JSONB):
                col.type = JSON()


@pytest_asyncio.fixture
async def session():
    _patch_jsonb()
    engine = create_async_engine('sqlite+aiosqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(SubscriptionEvent.__table__.create)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _ev(s, uid, etype, at, **extra):
    s.add(SubscriptionEvent(user_id=uid, event_type=etype, occurred_at=at, extra=extra or None))
    await s.commit()


@pytest.mark.asyncio
async def test_timeline_downtime_and_carry(session):
    s = session
    s.add(User(id=1, referral_code='r1', balance_kopeks=0))
    await s.commit()
    base = datetime(2026, 3, 21, 2, 42, tzinfo=UTC)
    # 1) первая покупка 30 дней -> конец base+30
    await _ev(s, 1, 'purchase', base, period_days=30)
    # 2) покупка через ~33 дня (после конца) -> простой, отсчёт заново
    await _ev(s, 1, 'purchase', base + timedelta(days=33, hours=10), period_days=30)
    # 3) покупка ДО конца (renewal с авторитетными датами) -> остаток учтён
    p_end = (base + timedelta(days=33, hours=10) + timedelta(days=30))
    await _ev(s, 1, 'renewal', p_end - timedelta(days=2, hours=13), period_days=30,
              previous_end_date=p_end.isoformat(),
              new_end_date=(p_end + timedelta(days=30)).isoformat())

    rows = await get_subscription_purchase_timeline(s, 1)
    assert len(rows) == 3
    assert rows[0]['index'] == 1 and rows[0]['downtime_seconds'] is None and rows[0]['carried_seconds'] is None
    assert rows[0]['new_end'] == (base + timedelta(days=30)).isoformat()
    # событие 2 — простой (>0), отсчёт от даты покупки
    assert rows[1]['downtime_seconds'] and rows[1]['downtime_seconds'] > 0
    assert rows[1]['carried_seconds'] is None
    # событие 3 — остаток (carried>0), новая дата из авторитетного new_end_date
    assert rows[2]['carried_seconds'] and rows[2]['carried_seconds'] > 0
    assert rows[2]['new_end'] == (p_end + timedelta(days=30)).isoformat()


@pytest.mark.asyncio
async def test_timeline_empty(session):
    session.add(User(id=2, referral_code='r2', balance_kopeks=0))
    await session.commit()
    assert await get_subscription_purchase_timeline(session, 2) == []
