"""Cabinet API: channel subscription nudge for the main channel.

GET  /cabinet/channel-nudge  → ChannelNudgeResponse
POST /cabinet/channel-nudge/seen → {"ok": true}
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.schemas.channel import ChannelBasicInfo, ChannelNudgeResponse, ChannelPostInfo
from app.database.crud.user import update_user_last_seen_post
from app.database.models import User
from app.services.channel_subscription_service import channel_subscription_service


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/channel-nudge', tags=['Cabinet Channel Nudge'])


class MarkSeenRequest(BaseModel):
    post_id: int


@router.get('', response_model=ChannelNudgeResponse)
async def get_channel_nudge(
    current_user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ChannelNudgeResponse:
    """Return whether the user needs to subscribe and/or see a fresh post.

    Never raises 500: Telegram/panel errors degrade to needs_subscribe=True.
    """
    main_channel = await channel_subscription_service.get_main_channel()

    if not main_channel:
        return ChannelNudgeResponse(
            needs_subscribe=False,
            channel=None,
            latest_post=None,
            show_post=False,
        )

    # Build latest_post block
    post_id = main_channel.get('last_post_message_id')
    latest_post: ChannelPostInfo | None = None
    if post_id and main_channel.get('last_post_link'):
        latest_post = ChannelPostInfo(
            id=post_id,
            link=main_channel['last_post_link'],
            title=main_channel.get('last_post_title'),
        )

    channel_info = ChannelBasicInfo(
        title=main_channel.get('title'),
        link=main_channel.get('channel_link'),
    )

    # Determine needs_subscribe
    needs_subscribe: bool = True
    if current_user.telegram_id:
        try:
            subs = await channel_subscription_service.check_user_subscriptions(current_user.telegram_id)
            needs_subscribe = not subs.get(main_channel['channel_id'], False)
        except Exception as e:
            # Degrade gracefully: show nudge, no 500
            logger.warning('channel_nudge: membership check failed', error=e)
            needs_subscribe = True

    # show_post: true if there is a post the user hasn't seen yet
    show_post = (
        latest_post is not None
        and latest_post.id != current_user.last_seen_channel_post_id
    )

    return ChannelNudgeResponse(
        needs_subscribe=needs_subscribe,
        channel=channel_info,
        latest_post=latest_post,
        show_post=show_post,
    )


@router.post('/seen', response_model=dict)
async def mark_channel_nudge_seen(
    body: MarkSeenRequest,
    current_user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    """Mark a channel post as seen for the current user."""
    await update_user_last_seen_post(db, current_user.id, body.post_id)
    return {'ok': True}
