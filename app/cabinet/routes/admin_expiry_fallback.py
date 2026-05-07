"""Админ-API для управления fallback-сквадом (при истечении/трафике).

GET  /cabinet/admin/expiry-fallback/stats — счётчики «сейчас в fallback»
POST /cabinet/admin/expiry-fallback/restore-all — массовый возврат всех
POST /cabinet/admin/expiry-fallback/reconcile — принудительный запуск reconcile
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.models import Subscription, User

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)
router = APIRouter(prefix='/admin/expiry-fallback', tags=['Admin Expiry Fallback'])


class FallbackStatsResponse(BaseModel):
    enabled: bool
    fallback_squad_uuid: str | None
    grace_days: int
    total_days: int
    expired_in_fallback: int
    traffic_in_fallback: int
    total_in_fallback: int


class FallbackRestoreAllResponse(BaseModel):
    success: bool
    restored: int
    failed: int
    total: int


class FallbackReconcileResponse(BaseModel):
    success: bool
    stats: dict


@router.get('/stats', response_model=FallbackStatsResponse)
async def get_fallback_stats(
    admin: User = Depends(require_permission('users:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> FallbackStatsResponse:
    """Текущее состояние fallback-системы и счётчики."""
    expired_count = await db.execute(
        select(func.count(Subscription.id)).where(Subscription.expiry_fallback_active.is_(True))
    )
    traffic_count = await db.execute(
        select(func.count(Subscription.id)).where(Subscription.traffic_fallback_active.is_(True))
    )
    total_count = await db.execute(
        select(func.count(Subscription.id)).where(
            or_(
                Subscription.expiry_fallback_active.is_(True),
                Subscription.traffic_fallback_active.is_(True),
            )
        )
    )

    return FallbackStatsResponse(
        enabled=bool(getattr(settings, 'EXPIRY_FALLBACK_ENABLED', False)),
        fallback_squad_uuid=getattr(settings, 'EXPIRY_FALLBACK_SQUAD_UUID', None),
        grace_days=int(getattr(settings, 'EXPIRY_FALLBACK_GRACE_DAYS', 3) or 3),
        total_days=int(getattr(settings, 'EXPIRY_FALLBACK_DAYS', 90) or 90),
        expired_in_fallback=expired_count.scalar() or 0,
        traffic_in_fallback=traffic_count.scalar() or 0,
        total_in_fallback=total_count.scalar() or 0,
    )


@router.post('/restore-all', response_model=FallbackRestoreAllResponse)
async def restore_all_from_fallback(
    admin: User = Depends(require_permission('users:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> FallbackRestoreAllResponse:
    """Массовый возврат всех юзеров из fallback в исходные сквады.

    Используется как «аварийный выключатель» — если что-то пошло не так,
    вернуть всех одной кнопкой.
    """
    from app.services.expiry_fallback_service import restore_from_fallback

    result = await db.execute(
        select(Subscription)
        .options(selectinload(Subscription.user))
        .where(
            or_(
                Subscription.expiry_fallback_active.is_(True),
                Subscription.traffic_fallback_active.is_(True),
            )
        )
    )
    subs = list(result.scalars().all())

    restored = 0
    failed = 0
    for sub in subs:
        try:
            ok = await restore_from_fallback(db, sub)
            if ok:
                restored += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            logger.error(
                'restore_all_from_fallback: ошибка для подписки',
                subscription_id=sub.id,
                error=str(exc),
            )

    logger.info(
        'Массовый возврат из fallback завершён',
        admin_id=admin.id,
        total=len(subs),
        restored=restored,
        failed=failed,
    )
    return FallbackRestoreAllResponse(
        success=True,
        restored=restored,
        failed=failed,
        total=len(subs),
    )


@router.post('/reconcile', response_model=FallbackReconcileResponse)
async def trigger_reconcile(
    admin: User = Depends(require_permission('users:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> FallbackReconcileResponse:
    """Принудительно запустить periodic reconcile прямо сейчас (не ждать 15 мин)."""
    from app.services.expiry_fallback_service import reconcile_fallback_subscriptions

    stats = await reconcile_fallback_subscriptions(db)
    logger.info('Принудительный reconcile fallback', admin_id=admin.id, stats=stats)
    return FallbackReconcileResponse(success=True, stats=stats)
