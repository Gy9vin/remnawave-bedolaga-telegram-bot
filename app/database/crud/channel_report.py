"""Helpers for channel membership report."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import RequiredChannel, Subscription, SubscriptionStatus, User, UserChannelSubscription, UserStatus


async def get_active_telegram_subscribers_for_report(
    db: AsyncSession,
) -> list[dict]:
    """Активные подписчики с telegram_id (включая trial).

    Возвращает по одной записи на пользователя с максимальной датой окончания
    среди его активных подписок.
    """
    current_time = datetime.now(UTC)
    result = await db.execute(
        select(
            User.id,
            User.telegram_id,
            User.username,
            User.first_name,
            User.last_name,
            func.max(Subscription.end_date).label('max_end_date'),
        )
        .join(Subscription, User.id == Subscription.user_id)
        .where(
            User.telegram_id.isnot(None),
            User.status == UserStatus.ACTIVE.value,
            Subscription.status == SubscriptionStatus.ACTIVE.value,
            Subscription.end_date > current_time,
        )
        .group_by(
            User.id,
            User.telegram_id,
            User.username,
            User.first_name,
            User.last_name,
        )
        .order_by(User.id)
    )
    return [
        {
            'user_id': row.id,
            'telegram_id': row.telegram_id,
            'username': row.username,
            'first_name': row.first_name,
            'last_name': row.last_name,
            'subscription_end_date': row.max_end_date,
        }
        for row in result.all()
    ]


async def get_subscribers_not_in_channels(
    db: AsyncSession,
) -> list[dict]:
    """Пользователи с активной подпиской, не подписанные хотя бы на один активный обязательный канал.

    Использует кэш UserChannelSubscription. Данные актуальны на момент последней проверки.
    """
    current_time = datetime.now(UTC)

    active_channel_ids_subquery = (
        select(RequiredChannel.channel_id)
        .where(RequiredChannel.is_active.is_(True))
        .scalar_subquery()
    )

    result = await db.execute(
        select(
            User.id,
            User.telegram_id,
            User.username,
            User.first_name,
            User.last_name,
            func.max(Subscription.end_date).label('max_end_date'),
            func.min(UserChannelSubscription.checked_at).label('last_checked'),
        )
        .join(Subscription, User.id == Subscription.user_id)
        .join(UserChannelSubscription, UserChannelSubscription.telegram_id == User.telegram_id)
        .where(
            User.telegram_id.isnot(None),
            User.status == UserStatus.ACTIVE.value,
            Subscription.status == SubscriptionStatus.ACTIVE.value,
            Subscription.end_date > current_time,
            UserChannelSubscription.channel_id.in_(active_channel_ids_subquery),
            UserChannelSubscription.is_member.is_(False),
        )
        .group_by(
            User.id,
            User.telegram_id,
            User.username,
            User.first_name,
            User.last_name,
        )
        .order_by(User.id)
    )
    return [
        {
            'user_id': row.id,
            'telegram_id': row.telegram_id,
            'username': row.username,
            'first_name': row.first_name,
            'last_name': row.last_name,
            'subscription_end_date': row.max_end_date,
            'last_checked': row.last_checked,
        }
        for row in result.all()
    ]
