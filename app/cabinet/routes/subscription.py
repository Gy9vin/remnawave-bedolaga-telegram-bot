"""Subscription management routes for cabinet.

This module is a thin aggregator that includes all subscription sub-routers
from subscription_modules/. Each domain (status, purchase, traffic, devices, etc.)
lives in its own module for maintainability.

The router exported here preserves the original prefix='/subscription' and tags,
so all existing API paths remain unchanged.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ..dependencies import get_cabinet_db, get_current_cabinet_user
from ..schemas.subscription import SubscriptionStatusResponse
from .subscription_modules import (
    autopay_router,
    daily_router,
    devices_router,
    purchase_router,
    renewal_router,
    revoke_router,
    servers_router,
    status_router,
    tariff_switch_router,
    traffic_router,
)
from .subscription_modules.status import get_subscription as _get_subscription_handler
from app.database.crud.subscription_event import get_subscription_purchase_timeline


router = APIRouter(prefix='/subscription', tags=['Cabinet Subscription'])


# Root endpoint: GET /subscription (empty path — must be on this router directly)
@router.get('', response_model=SubscriptionStatusResponse)
async def get_subscription(
    user=Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
    subscription_id: int | None = Query(None, description='Subscription ID for multi-tariff'),
):
    return await _get_subscription_handler(user=user, db=db, subscription_id=subscription_id)


@router.get('/timeline')
async def get_subscription_timeline(
    user=Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    """История покупок/продлений текущего пользователя."""
    events = await get_subscription_purchase_timeline(db, user.id)
    return {'events': events, 'since': events[0]['date'] if events else None}


# Include all sub-routers
router.include_router(status_router)
router.include_router(renewal_router)
router.include_router(purchase_router)
router.include_router(traffic_router)
router.include_router(devices_router)
router.include_router(servers_router)
router.include_router(autopay_router)
router.include_router(daily_router)
router.include_router(tariff_switch_router)
router.include_router(revoke_router)
