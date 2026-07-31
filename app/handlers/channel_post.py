"""Handler for channel_post updates in the main required channel.

The bot must be a channel admin to receive these events.
If it is not, events simply won't arrive — the handler degrades gracefully.
"""
from __future__ import annotations

from datetime import UTC, datetime

import structlog
from aiogram import Router
from aiogram.types import Message

from app.database.crud.required_channel import update_channel_post
from app.database.database import AsyncSessionLocal
from app.services.channel_subscription_service import channel_subscription_service


logger = structlog.get_logger(__name__)

router = Router(name='channel_post')

_MAX_TITLE_LEN = 120


def _build_post_link(message: Message) -> str:
    """Build a t.me link to a channel message.

    Uses @username if available, otherwise falls back to numeric channel_id.
    """
    username = getattr(message.chat, 'username', None)
    if username:
        return f'https://t.me/{username}/{message.message_id}'
    # Numeric channel ID is always negative; strip the leading -100 for t.me links
    raw_id = str(message.chat.id).lstrip('-')
    if raw_id.startswith('100'):
        raw_id = raw_id[3:]
    return f'https://t.me/c/{raw_id}/{message.message_id}'


def _extract_title(message: Message) -> str:
    """Extract up to 120 chars of post text/caption, fallback to 'Новый пост'."""
    text = getattr(message, 'text', None) or getattr(message, 'caption', None) or ''
    text = text.strip()
    if not text:
        return 'Новый пост'
    return text[:_MAX_TITLE_LEN]


@router.channel_post()
async def on_channel_post(message: Message) -> None:
    """Receive a new channel post; update last_post_* on the main channel."""
    try:
        main_channel = await channel_subscription_service.get_main_channel()
        if not main_channel:
            return  # No main channel configured — nothing to track

        # Only process posts from the main channel
        if str(message.chat.id) != main_channel['channel_id']:
            return

        link = _build_post_link(message)
        title = _extract_title(message)
        msg_date = getattr(message, 'date', None)
        at = msg_date if msg_date and msg_date.tzinfo else datetime.now(UTC)

        async with AsyncSessionLocal() as db:
            await update_channel_post(
                db,
                channel_db_id=main_channel['id'],
                message_id=message.message_id,
                link=link,
                title=title,
                at=at,
            )

        # Invalidate cache so the new post appears in nudge card immediately
        await channel_subscription_service.invalidate_channels_cache()

        logger.info(
            'Updated main channel last post',
            channel_id=main_channel['channel_id'],
            message_id=message.message_id,
        )
    except Exception as e:
        logger.error('Error handling channel_post', error=e)
