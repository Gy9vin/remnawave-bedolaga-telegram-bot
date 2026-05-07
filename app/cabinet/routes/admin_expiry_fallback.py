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


class CleanupOldExpiredResponse(BaseModel):
    success: bool
    deleted: int
    skipped_with_balance: int
    skipped_pending_purchase: int
    total_candidates: int
    months_threshold: int


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


@router.post('/cleanup-old-expired', response_model=CleanupOldExpiredResponse)
async def cleanup_old_expired(
    admin: User = Depends(require_permission('users:delete')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> CleanupOldExpiredResponse:
    """Удаляет юзеров со status=EXPIRED старше INACTIVE_USER_DELETE_MONTHS месяцев.

    Условия для удаления:
    - У юзера НЕТ ни одной активной подписки (любой)
    - balance_kopeks == 0 (если EXPIRED_CLEANUP_REQUIRE_ZERO_BALANCE=true)
    - Нет незавершённых guest_purchases

    Период задаётся через INACTIVE_USER_DELETE_MONTHS (default 3).
    """
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import exists as sa_exists

    from app.database.crud.user import delete_user
    from app.database.models import GuestPurchase, GuestPurchaseStatus, Subscription, SubscriptionStatus

    months = max(1, int(getattr(settings, 'INACTIVE_USER_DELETE_MONTHS', 3) or 3))
    require_zero_balance = bool(getattr(settings, 'EXPIRED_CLEANUP_REQUIRE_ZERO_BALANCE', True))
    threshold = datetime.now(UTC) - timedelta(days=months * 30)

    # Кандидаты: юзеры у которых есть EXPIRED подписки и нет активных
    result = await db.execute(
        select(User)
        .join(Subscription, Subscription.user_id == User.id)
        .where(
            and_(
                Subscription.status == SubscriptionStatus.EXPIRED.value,
                Subscription.end_date < threshold,
            )
        )
        .distinct()
    )
    candidates = list(result.scalars().all())

    deleted = 0
    skipped_balance = 0
    skipped_pending = 0

    for user in candidates:
        # Проверка: нет ни одной активной подписки
        active_check = await db.execute(
            select(func.count(Subscription.id)).where(
                and_(
                    Subscription.user_id == user.id,
                    Subscription.status.in_(
                        [SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]
                    ),
                )
            )
        )
        if (active_check.scalar() or 0) > 0:
            continue

        if require_zero_balance and (user.balance_kopeks or 0) > 0:
            skipped_balance += 1
            continue

        # Незавершённые guest_purchases
        has_pending = await db.scalar(
            select(
                sa_exists().where(
                    GuestPurchase.user_id == user.id,
                    GuestPurchase.status.notin_(
                        [
                            GuestPurchaseStatus.DELIVERED.value,
                            GuestPurchaseStatus.FAILED.value,
                            GuestPurchaseStatus.EXPIRED.value,
                        ]
                    ),
                )
            )
        )
        if has_pending:
            skipped_pending += 1
            continue

        try:
            success = await delete_user(db, user)
            if success:
                deleted += 1
        except Exception as exc:
            logger.error('Ошибка удаления EXPIRED юзера', user_id=user.id, error=str(exc))

    logger.info(
        'cleanup_old_expired завершён',
        admin_id=admin.id,
        deleted=deleted,
        skipped_balance=skipped_balance,
        skipped_pending=skipped_pending,
        candidates=len(candidates),
        months_threshold=months,
    )
    return CleanupOldExpiredResponse(
        success=True,
        deleted=deleted,
        skipped_with_balance=skipped_balance,
        skipped_pending_purchase=skipped_pending,
        total_candidates=len(candidates),
        months_threshold=months,
    )
