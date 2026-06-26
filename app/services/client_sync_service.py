"""Сервис синхронизации клиентских приложений из HWID-устройств RemnaWave.

Тянет все HWID-устройства из панели одним агрегированным запросом,
парсит клиентское приложение из поля platform/appVersion, маппит
устройство на bot-user_id через User.remnawave_uuid (фолбэк:
Subscription.remnawave_uuid), и делает upsert+prune в таблицу
user_clients. Используется для таргетирования рассылок по клиентскому
приложению (Happ, v2rayNG, Streisand …).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from app.database.models import Subscription, User, UserClient
from app.utils.client_detect import parse_client_app

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Модульное состояние последней синхронизации
# ---------------------------------------------------------------------------

_last_client_sync: datetime | None = None


def get_last_client_sync() -> datetime | None:
    """Возвращает UTC-время последнего успешного запуска sync_user_clients."""
    return _last_client_sync


# ---------------------------------------------------------------------------
# Вспомогательная функция парсинга datetime из строки панели
# ---------------------------------------------------------------------------

def _parse_panel_dt(value: str | None) -> datetime | None:
    """Парсит ISO-строку с суффиксом Z в datetime(UTC). Best-effort: None при ошибке."""
    if not value:
        return None
    try:
        # Панель отдаёт «2024-01-15T12:34:56.789Z» — заменяем Z на +00:00
        return datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Основная функция синхронизации
# ---------------------------------------------------------------------------

async def sync_user_clients(db: 'AsyncSession') -> dict:
    """Синхронизирует таблицу user_clients из HWID-устройств панели.

    Алгоритм:
    1. Получает все устройства одним вызовом get_all_hwid_devices() (метод
       сам листает страницы).
    2. Предзагружает карты panel_uuid→user_id без N+1-запросов.
    3. Для каждого устройства парсит app из поля platform (суррогат UA),
       резолвит user_id, собирает wanted = {(user_id, app): max_last_seen}.
    4. Upsert: создаёт или обновляет строки UserClient.
    5. Prune: удаляет строки UserClient для юзеров, которые были в синке,
       но для которых данное приложение в синке отсутствует (исчезло).
       Юзеры, которых синк вообще не видел, НЕ трогаются.

    Returns:
        {'devices': N, 'users': M, 'apps': K} или {'skipped': причина}.
    """
    global _last_client_sync

    # ── 1. Получить панельный клиент ────────────────────────────────────────
    from app.services.subscription_service import SubscriptionService
    from app.external.remnawave_api import RemnaWaveAPIError

    svc = SubscriptionService()
    svc._refresh_configuration()  # обновляем кэш конфига
    if not svc.is_configured:
        logger.warning(
            'client_sync: RemnaWave API не настроен, синхронизация пропущена',
            config_error=svc.configuration_error,
        )
        return {'skipped': 'not_configured'}

    # ── 2. Предзагрузка карт panel_uuid → user_id (без N+1) ─────────────────
    try:
        from sqlalchemy import select as sa_select

        # Карта по User.remnawave_uuid (приоритет)
        user_rows = await db.execute(
            sa_select(User.id, User.remnawave_uuid).where(User.remnawave_uuid.isnot(None))
        )
        user_uuid_map: dict[str, int] = {row.remnawave_uuid: row.id for row in user_rows}

        # Карта по Subscription.remnawave_uuid (фолбэк)
        sub_rows = await db.execute(
            sa_select(Subscription.user_id, Subscription.remnawave_uuid).where(
                Subscription.remnawave_uuid.isnot(None)
            )
        )
        sub_uuid_map: dict[str, int] = {row.remnawave_uuid: row.user_id for row in sub_rows}

    except Exception as e:
        logger.error('client_sync: ошибка загрузки карт uuid→user_id', error=e)
        return {'skipped': 'db_error'}

    # ── 3. Получить все HWID-устройства из панели ───────────────────────────
    all_devices: list[dict] = []
    try:
        async with svc.get_api_client() as api:
            result = await api.get_all_hwid_devices()
            all_devices = result.get('devices', [])
    except RemnaWaveAPIError as e:
        logger.error('client_sync: ошибка запроса HWID-устройств', error=e)
        return {'skipped': 'api_error'}
    except Exception as e:
        logger.error('client_sync: неожиданная ошибка при получении устройств', error=e)
        return {'skipped': 'api_error'}

    total_devices = len(all_devices)
    logger.info('client_sync: получено устройств из панели', count=total_devices)

    # ── 4. Собрать wanted: {(user_id, app_name): max last_seen_at} ──────────
    # Поле userAgent у HWID-записей отсутствует; platform — это «имя приложения»
    # (Happ, v2rayNG, Streisand …). Передаём его в parse_client_app как суррогат UA,
    # что даёт тот же prefix-семантики (до '/', '(' или пробела).
    wanted: dict[tuple[int, str], datetime | None] = {}
    users_in_sync: set[int] = set()

    for device in all_devices:
        puuid = device.get('userUuid')
        if not puuid:
            continue

        # Резолв user_id: сначала User-карта, потом Subscription-карта
        user_id = user_uuid_map.get(puuid) or sub_uuid_map.get(puuid)
        if user_id is None:
            continue

        # Парсим «клиентское приложение» из поля platform (суррогат userAgent)
        platform_str = device.get('platform') or device.get('appVersion') or device.get('deviceModel')
        app = parse_client_app(platform_str)

        # Берём best-effort дату последней активности устройства
        raw_dt = device.get('updatedAt') or device.get('lastSeen') or device.get('createdAt')
        seen_at = _parse_panel_dt(raw_dt)

        key = (user_id, app)
        existing = wanted.get(key)
        if existing is None or (seen_at is not None and (existing is None or seen_at > existing)):
            wanted[key] = seen_at
        users_in_sync.add(user_id)

    logger.info(
        'client_sync: собрано пар (user, app)',
        pairs=len(wanted),
        users=len(users_in_sync),
    )

    if not wanted:
        # Панель не вернула ни одного устройства с известным юзером
        _last_client_sync = datetime.now(UTC)
        return {'devices': total_devices, 'users': 0, 'apps': 0}

    # ── 5. Загрузить существующие UserClient-строки для затронутых юзеров ───
    from sqlalchemy import select as sa_select

    try:
        existing_rows_result = await db.execute(
            sa_select(UserClient).where(UserClient.user_id.in_(users_in_sync))
        )
        existing_rows: list[UserClient] = list(existing_rows_result.scalars().all())
    except Exception as e:
        logger.error('client_sync: ошибка загрузки существующих UserClient', error=e)
        return {'skipped': 'db_error'}

    # Индекс существующих строк
    existing_index: dict[tuple[int, str], UserClient] = {
        (row.user_id, row.app_name): row for row in existing_rows
    }

    now_utc = datetime.now(UTC)

    # ── 6. Upsert ────────────────────────────────────────────────────────────
    for (user_id, app_name), last_seen in wanted.items():
        existing_row = existing_index.get((user_id, app_name))
        if existing_row is None:
            # Создаём новую строку
            new_row = UserClient(
                user_id=user_id,
                app_name=app_name,
                last_seen_at=last_seen,
                updated_at=now_utc,
            )
            db.add(new_row)
        else:
            # Обновляем только если данные новее или last_seen_at было None
            existing_seen = existing_row.last_seen_at
            if last_seen is not None and (existing_seen is None or last_seen > existing_seen):
                existing_row.last_seen_at = last_seen
            existing_row.updated_at = now_utc

    # ── 7. Prune: удаляем строки для синкнутых юзеров, которых нет в wanted ─
    # НЕ трогаем юзеров, которых вообще не было в этом синке.
    wanted_keys = set(wanted.keys())
    for row in existing_rows:
        if (row.user_id, row.app_name) not in wanted_keys:
            await db.delete(row)

    # ── 8. Коммит ────────────────────────────────────────────────────────────
    try:
        await db.commit()
    except Exception as e:
        logger.error('client_sync: ошибка коммита', error=e)
        await db.rollback()
        return {'skipped': 'commit_error'}

    # ── 9. Обновляем timestamp и возвращаем статистику ───────────────────────
    _last_client_sync = datetime.now(UTC)

    unique_apps = len({app for (_, app) in wanted.keys()})
    logger.info(
        'client_sync: завершено',
        devices=total_devices,
        users=len(users_in_sync),
        apps=unique_apps,
    )
    return {'devices': total_devices, 'users': len(users_in_sync), 'apps': unique_apps}
