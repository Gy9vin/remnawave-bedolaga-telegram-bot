"""CRUD helpers for broadcast-level reports.

Currently provides: get_broadcast_blocked_active_subscribers — returns users
who blocked the bot during a given broadcast AND still have an active subscription.
"""
from datetime import UTC, datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import BroadcastHistory, Subscription, SubscriptionStatus, Tariff, User


async def get_broadcast_blocked_active_subscribers(
    db: AsyncSession, broadcast_id: int
) -> list[dict]:
    """Return users who blocked the bot in *broadcast_id* and still have an active subscription.

    Returns a list of dicts sorted by days_left ascending (soonest-expiring first):
        {
            'telegram_id': int,
            'username': str | None,
            'email': str | None,
            'tariff_name': str | None,
            'end_date': str,   # ISO-8601
            'days_left': int,
        }

    Returns [] if the broadcast record does not exist or blocked_user_ids is None/empty.
    """
    # 1. Load the broadcast row and read blocked telegram_ids
    broadcast_row = await db.get(BroadcastHistory, broadcast_id)
    if broadcast_row is None:
        return []

    blocked_ids: list[int] | None = broadcast_row.blocked_user_ids
    if not blocked_ids:
        return []

    now = datetime.now(UTC)

    # 2. Query: select individual columns to avoid ORM relationship lazy-loading
    stmt = (
        select(
            User.telegram_id,
            User.username,
            User.email,
            Subscription.end_date,
            Tariff.name.label('tariff_name'),
        )
        .join(Subscription, Subscription.user_id == User.id)
        .outerjoin(Tariff, Tariff.id == Subscription.tariff_id)
        .where(
            and_(
                User.telegram_id.in_(blocked_ids),
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.end_date > now,
            )
        )
    )

    result = await db.execute(stmt)
    rows = result.all()

    # 3. Build result dicts
    output: list[dict] = []
    for row in rows:
        end_dt = row.end_date
        # Ensure aware (AwareDateTime handles it on load, but guard for SQLite tests)
        if end_dt is not None and end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=UTC)
        days_left = (end_dt - now).days if end_dt is not None else 0

        output.append(
            {
                'telegram_id': row.telegram_id,
                'username': row.username,
                'email': row.email,
                'tariff_name': row.tariff_name,
                'end_date': end_dt.isoformat() if end_dt is not None else None,
                'days_left': days_left,
            }
        )

    # Sort by days_left ascending (soonest-expiring first)
    output.sort(key=lambda d: d['days_left'])
    return output
