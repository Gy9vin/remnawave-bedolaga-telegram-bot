from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import SubscriptionEvent


async def create_subscription_event(
    db: AsyncSession,
    *,
    user_id: int,
    event_type: str,
    subscription_id: int | None = None,
    transaction_id: int | None = None,
    amount_kopeks: int | None = None,
    currency: str | None = None,
    message: str | None = None,
    occurred_at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> SubscriptionEvent:
    event = SubscriptionEvent(
        user_id=user_id,
        event_type=event_type,
        subscription_id=subscription_id,
        transaction_id=transaction_id,
        amount_kopeks=amount_kopeks,
        currency=currency,
        message=message,
        occurred_at=occurred_at or datetime.now(UTC),
        extra=extra or None,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


async def list_subscription_events(
    db: AsyncSession,
    *,
    limit: int,
    offset: int,
    event_types: Iterable[str] | None = None,
    user_id: int | None = None,
) -> tuple[list[SubscriptionEvent], int]:
    base_query = select(SubscriptionEvent)
    filters = []

    if event_types:
        filters.append(SubscriptionEvent.event_type.in_(set(event_types)))
    if user_id:
        filters.append(SubscriptionEvent.user_id == user_id)

    if filters:
        base_query = base_query.where(and_(*filters))

    total_query = base_query.with_only_columns(func.count()).order_by(None)
    total = await db.scalar(total_query) or 0

    result = await db.execute(
        base_query.options(selectinload(SubscriptionEvent.user))
        .order_by(SubscriptionEvent.occurred_at.desc())
        .offset(offset)
        .limit(limit)
    )

    return result.scalars().all(), int(total)


_TIMELINE_EVENT_TYPES = ('purchase', 'renewal', 'activation')


def _parse_iso_aware(value):
    """Parse an ISO datetime string to an aware datetime (UTC if naive)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


async def get_subscription_purchase_timeline(db: AsyncSession, user_id: int) -> list[dict]:
    """Хронология покупок/продлений для пользователя из subscription_events.

    Для renewal берём авторитетные previous_end_date/new_end_date из extra;
    для purchase/activation дату окончания реконструируем: если покупка после
    прошлого конца — отсчёт заново от даты покупки (был простой), иначе — плюсуем
    к остатку. Возвращает строки, отсортированные по дате (ISO), с секундами
    простоя/остатка (или None).
    """
    result = await db.execute(
        select(SubscriptionEvent)
        .where(
            SubscriptionEvent.user_id == user_id,
            SubscriptionEvent.event_type.in_(_TIMELINE_EVENT_TYPES),
        )
        .order_by(SubscriptionEvent.occurred_at.asc(), SubscriptionEvent.id.asc())
    )
    events = result.scalars().all()

    rows: list[dict] = []
    running_end = None
    for idx, ev in enumerate(events, start=1):
        extra = ev.extra or {}
        period_days = extra.get('period_days')
        if period_days is None and ev.event_type == 'activation':
            period_days = extra.get('trial_duration_days')

        date = _aware(ev.occurred_at)
        prev_end = running_end

        new_end_iso = extra.get('new_end_date')
        if new_end_iso:
            new_end = _parse_iso_aware(new_end_iso)
            prev_end_eff = _parse_iso_aware(extra.get('previous_end_date')) or prev_end
        else:
            days = int(period_days or 0)
            base = date if (prev_end is None or date >= prev_end) else prev_end
            new_end = base + timedelta(days=days)
            prev_end_eff = prev_end

        downtime_seconds = None
        carried_seconds = None
        if prev_end_eff is not None and date is not None:
            if date > prev_end_eff:
                downtime_seconds = int((date - prev_end_eff).total_seconds())
            elif date < prev_end_eff:
                carried_seconds = int((prev_end_eff - date).total_seconds())

        rows.append({
            'index': idx,
            'event_type': ev.event_type,
            'date': date.isoformat() if date else None,
            'period_days': int(period_days) if period_days is not None else None,
            'amount_kopeks': ev.amount_kopeks,
            'prev_end': prev_end_eff.isoformat() if prev_end_eff else None,
            'new_end': new_end.isoformat() if new_end else None,
            'downtime_seconds': downtime_seconds,
            'carried_seconds': carried_seconds,
        })
        running_end = new_end or running_end

    return rows
