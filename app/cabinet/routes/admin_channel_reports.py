"""Admin API для отчётов по обязательным каналам."""

import io

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.required_channel import get_channel_by_id
from app.database.models import User
from app.services.channel_membership_report_service import (
    ReportAlreadyRunning,
    ReportNotFound,
    channel_membership_report_service,
)

from ..dependencies import get_cabinet_db, require_permission
from ..schemas.channel_report import (
    ChannelReportStartResponse,
    ChannelReportStatusResponse,
)


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/channel-subscriptions', tags=['Cabinet Admin Channel Reports'])


@router.post(
    '/{channel_db_id}/report',
    response_model=ChannelReportStartResponse,
    status_code=202,
)
async def start_channel_report(
    channel_db_id: int,
    admin: User = Depends(require_permission('channels:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ChannelReportStartResponse:
    """Запускает фоновой отчёт: кто из активных подписчиков НЕ в канале."""
    channel = await get_channel_by_id(db, channel_db_id)
    if channel is None:
        raise HTTPException(status_code=404, detail='Channel not found')

    try:
        report_id = await channel_membership_report_service.start_report(
            channel_db_id=channel_db_id,
            admin_telegram_id=admin.telegram_id,
        )
    except ReportAlreadyRunning as exc:
        logger.error('Channel report already running', channel_db_id=channel_db_id, error=str(exc))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Operation failed. Check logs.')
    except ValueError as exc:
        logger.error('Channel report start failed', channel_db_id=channel_db_id, error=str(exc))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Operation failed. Check logs.')

    return ChannelReportStartResponse(report_id=report_id)


@router.get(
    '/reports/{report_id}',
    response_model=ChannelReportStatusResponse,
)
async def get_report_status(
    report_id: str,
    _admin: User = Depends(require_permission('channels:read')),
) -> ChannelReportStatusResponse:
    try:
        data = channel_membership_report_service.get_status(report_id)
    except ReportNotFound:
        raise HTTPException(status_code=404, detail='Report not found')
    return ChannelReportStatusResponse(**data)


@router.get('/reports/{report_id}/csv')
async def download_report_csv(
    report_id: str,
    _admin: User = Depends(require_permission('channels:read')),
) -> StreamingResponse:
    try:
        csv_bytes, filename = channel_membership_report_service.get_csv(report_id)
    except ReportNotFound:
        raise HTTPException(status_code=404, detail='Report not found')
    except ValueError as exc:
        logger.error('Channel report CSV failed', report_id=report_id, error=str(exc))
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Operation failed. Check logs.')

    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@router.post('/reports/{report_id}/cancel', status_code=200)
async def cancel_report(
    report_id: str,
    _admin: User = Depends(require_permission('channels:edit')),
) -> dict:
    try:
        cancelled = await channel_membership_report_service.cancel(report_id)
    except ReportNotFound:
        raise HTTPException(status_code=404, detail='Report not found')
    return {'cancelled': cancelled}
