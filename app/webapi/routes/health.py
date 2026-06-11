from __future__ import annotations

from fastapi import APIRouter, Security

from app.config import settings
from app.database import db_manager, get_pool_metrics
from app.services.version_service import version_service

from ..dependencies import require_api_token
from ..schemas.health import HealthCheckResponse, HealthFeatureFlags


router = APIRouter()


@router.get('/health', tags=['health'])
async def health_check() -> dict:
    # Public liveness probe — returns only {status: ok} to avoid version disclosure.
    # Detailed version/feature information is available at /health/detailed (token-gated).
    return {'status': 'ok'}


@router.get('/health/detailed', tags=['health'], response_model=HealthCheckResponse)
async def health_check_detailed(_: object = Security(require_api_token)) -> HealthCheckResponse:
    """Detailed health info including versions and feature flags. Requires API token."""
    return HealthCheckResponse(
        status='ok',
        api_version=settings.WEB_API_VERSION,
        bot_version=version_service.current_version,
        features=HealthFeatureFlags(
            monitoring=settings.MONITORING_INTERVAL > 0,
            maintenance=True,
            reporting=True,
            webhooks=bool(settings.WEBHOOK_URL),
        ),
    )


@router.get('/health/database', tags=['health'])
async def database_health(_: object = Security(require_api_token)) -> dict:
    """Детальная информация о состоянии базы данных."""

    return await db_manager.health_check()


@router.get('/metrics/pool', tags=['health'])
async def pool_metrics(_: object = Security(require_api_token)) -> dict:
    """Метрики пула подключений к базе данных."""

    return await get_pool_metrics()
