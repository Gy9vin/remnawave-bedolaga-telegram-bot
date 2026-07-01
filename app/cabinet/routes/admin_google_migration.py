"""Admin endpoints to trigger the Google-sunset set-password invite campaign."""

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.user import get_google_at_risk_users, get_google_migration_stats
from app.database.models import User
from app.services.google_migration_service import google_migration_service

from ..dependencies import get_cabinet_db, require_permission

logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/google-migration', tags=['admin-google-migration'])


@router.get('/status')
async def get_migration_status(
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    stats = await get_google_migration_stats(db)
    return {'stats': stats, 'run': google_migration_service.get_status()}


@router.get('/at-risk')
async def get_at_risk_users(
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    users = await get_google_at_risk_users(db)
    return {'count': len(users), 'users': users}


@router.post('/send')
async def send_invites(
    admin: User = Depends(require_permission('broadcasts:send')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    started = await google_migration_service.start()
    logger.info('Google migration invites triggered', admin_id=getattr(admin, 'id', None), started=started)
    return {'started': started}
