"""Admin Happ Management routes — настройка Happ App через Cabinet."""

import json
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services.happ_management import config as cfg
from app.services.happ_management.remnawave_sync import (
    build_custom_headers,
    build_native_fields,
    cleanup_remnawave_headers,
    schedule_sync,
    sync_to_remnawave,
)

from ..dependencies import require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/happ', tags=['Admin Happ Management'])


# ─── Schemas ─────────────────────────────────────────────────────────────────


class HappSettingValue(BaseModel):
    value: Any


class HappProviderCreate(BaseModel):
    provider_id: str


class HappProviderUpdate(BaseModel):
    managed: bool | None = None
    total_assigned: int | None = None


class HappImportData(BaseModel):
    data: dict


class HappSourceSquadAdd(BaseModel):
    uuid: str
    name: str


# ─── Helper: map settings schema to JSON-serializable format ─────────────────


def _serialize_schema(key: str, schema: dict, value: Any, is_overridden: bool = False) -> dict:
    return {
        'key': key,
        'label': schema['label'],
        'hint': schema.get('hint', ''),
        'type': schema['type'],
        'category': schema['category'],
        'group': schema.get('group'),
        'depends_on': schema.get('depends_on'),
        'warning': schema.get('warning', False),
        'choices': schema.get('choices', []),
        'max_length': schema.get('max_length'),
        'validate': schema.get('validate'),
        'validate_hint': schema.get('validate_hint'),
        'validate_range': schema.get('validate_range'),
        'value': value,
        'is_overridden': is_overridden,
    }


# ─── Main status ─────────────────────────────────────────────────────────────


@router.get('/status')
async def get_status(_user=Depends(require_permission('admin'))):
    """Общий статус модуля — для главного экрана."""
    enabled = cfg.get('MODULE_ENABLED')
    remna_sync = cfg.get('REMNAWAVE_SYNC_ENABLED')
    providers = cfg.get_providers()

    active_features = []
    if cfg.get('SUB_INFO_TEXT'):
        active_features.append('инфо-баннер')
    if cfg.get('SUB_EXPIRE_ENABLED'):
        active_features.append('истечение')
    if cfg.get('AUTOCONNECT_ENABLED'):
        active_features.append('автоподключение')
    if cfg.get('FRAGMENTATION_ENABLED'):
        active_features.append('фрагментация')
    if cfg.get('MUX_ENABLED'):
        active_features.append('mux')

    return {
        'module_enabled': enabled,
        'remnawave_sync_enabled': remna_sync,
        'providers_count': len(providers),
        'active_features': active_features,
    }


# ─── Settings: get all / get by category ─────────────────────────────────────


@router.get('/settings')
async def get_all_settings(_user=Depends(require_permission('admin'))):
    """Все настройки по категориям (SECTIONS + CATEGORIES)."""
    result = {}
    for section_key, section in cfg.SECTIONS.items():
        result[section_key] = {
            'label': section['label'],
            'categories': {},
        }
        for cat_key in section['categories']:
            cat_label = cfg.CATEGORIES.get(cat_key, cat_key)
            cat_hint = cfg.CATEGORY_HINTS.get(cat_key, '')
            items = cfg.get_settings_by_category(cat_key)
            result[section_key]['categories'][cat_key] = {
                'label': cat_label,
                'hint': cat_hint,
                'settings': [_serialize_schema(k, schema, v) for k, schema, v in items],
            }
    return result


@router.get('/settings/{category}')
async def get_settings_by_category(category: str, _user=Depends(require_permission('admin'))):
    """Настройки одной категории."""
    if category not in cfg.CATEGORIES:
        raise HTTPException(status_code=404, detail='Категория не найдена')
    items = cfg.get_settings_by_category(category)
    return {
        'category': category,
        'label': cfg.CATEGORIES[category],
        'hint': cfg.CATEGORY_HINTS.get(category, ''),
        'settings': [_serialize_schema(k, schema, v) for k, schema, v in items],
    }


@router.get('/settings/key/{key}')
async def get_setting(key: str, _user=Depends(require_permission('admin'))):
    """Одна настройка по ключу."""
    schema = cfg.SETTINGS_SCHEMA.get(key)
    if not schema:
        raise HTTPException(status_code=404, detail='Настройка не найдена')
    value = cfg.get(key)
    return _serialize_schema(key, schema, value)


@router.patch('/settings/key/{key}')
async def update_setting(key: str, body: HappSettingValue, _user=Depends(require_permission('admin'))):
    """Обновить значение настройки."""
    schema = cfg.SETTINGS_SCHEMA.get(key)
    if not schema:
        raise HTTPException(status_code=404, detail='Настройка не найдена')

    value = body.value

    # Validate
    if schema['type'] == 'bool':
        if not isinstance(value, bool):
            raise HTTPException(status_code=422, detail='Ожидается boolean')
    elif schema['type'] == 'choice':
        choices = schema.get('choices', [])
        if value not in choices:
            raise HTTPException(status_code=422, detail=f'Допустимые значения: {choices}')
    elif schema['type'] == 'str':
        if value is None:
            value = ''
        value = str(value).strip()
        if value == '-':
            value = ''
        if value:
            # JSON compact for COLOR_PROFILE
            if key == 'COLOR_PROFILE' and value != 'resetcolors':
                try:
                    parsed = json.loads(value)
                    value = json.dumps(parsed, ensure_ascii=False, separators=(',', ':'))
                except json.JSONDecodeError:
                    raise HTTPException(status_code=422, detail='COLOR_PROFILE: невалидный JSON')
            err = cfg.validate_value(key, value)
            if err:
                raise HTTPException(status_code=422, detail=err)

    had_announce = key == 'ANNOUNCE_TEXT' and bool(cfg.get(key))
    cfg.set_value(key, value)
    if had_announce and not value:
        cfg.mark_announce_clear()
    schedule_sync()

    return {'key': key, 'value': value, 'syncing': cfg.get('REMNAWAVE_SYNC_ENABLED')}


# ─── Providers ───────────────────────────────────────────────────────────────


@router.get('/providers')
async def get_providers(_user=Depends(require_permission('admin'))):
    """Список всех Provider ID."""
    return {'providers': cfg.get_providers()}


@router.post('/providers', status_code=201)
async def add_provider(body: HappProviderCreate, _user=Depends(require_permission('admin'))):
    """Добавить Provider ID."""
    import re

    if not re.fullmatch(r'[A-Za-z0-9]{8}', body.provider_id):
        raise HTTPException(status_code=422, detail='Provider ID: 8 алфавитно-цифровых символов')
    added = cfg.add_provider(body.provider_id)
    if not added:
        raise HTTPException(status_code=409, detail='Provider ID уже существует')
    schedule_sync()
    return {'provider_id': body.provider_id, 'added': True}


@router.patch('/providers/{provider_id}')
async def update_provider(provider_id: str, body: HappProviderUpdate, _user=Depends(require_permission('admin'))):
    """Обновить параметры провайдера."""
    providers = cfg.get_providers()
    if not any(p.get('provider_id') == provider_id for p in providers):
        raise HTTPException(status_code=404, detail='Provider ID не найден')

    if body.managed is not None:
        cfg.set_provider_managed(provider_id, body.managed)
        schedule_sync()
    if body.total_assigned is not None:
        cfg.set_provider_total_assigned(provider_id, body.total_assigned)

    return {'provider_id': provider_id, 'updated': True}


@router.delete('/providers/{provider_id}')
async def delete_provider(provider_id: str, _user=Depends(require_permission('admin'))):
    """Удалить Provider ID."""
    removed = cfg.remove_provider(provider_id)
    if not removed:
        raise HTTPException(status_code=404, detail='Provider ID не найден')
    schedule_sync()
    return {'provider_id': provider_id, 'removed': True}


# ─── Per-provider overrides ───────────────────────────────────────────────────


@router.get('/providers/{provider_id}/overrides')
async def get_provider_overrides(provider_id: str, _user=Depends(require_permission('admin'))):
    """Переопределения настроек для провайдера."""
    overrides = cfg.get_provider_overrides(provider_id)
    return {'provider_id': provider_id, 'overrides': overrides}


@router.patch('/providers/{provider_id}/overrides/{key}')
async def set_provider_override(
    provider_id: str, key: str, body: HappSettingValue, _user=Depends(require_permission('admin'))
):
    """Задать переопределение настройки для провайдера."""
    if key in cfg.NON_OVERRIDABLE_KEYS:
        raise HTTPException(status_code=422, detail='Эта настройка не может быть переопределена для провайдера')
    if key not in cfg.SETTINGS_SCHEMA:
        raise HTTPException(status_code=404, detail='Настройка не найдена')
    cfg.set_provider_override(provider_id, key, body.value)
    schedule_sync()
    return {'provider_id': provider_id, 'key': key, 'value': body.value}


@router.delete('/providers/{provider_id}/overrides/{key}')
async def remove_provider_override(provider_id: str, key: str, _user=Depends(require_permission('admin'))):
    """Удалить переопределение настройки для провайдера."""
    cfg.remove_provider_override(provider_id, key)
    schedule_sync()
    return {'provider_id': provider_id, 'key': key, 'removed': True}


# ─── Remnawave sync ───────────────────────────────────────────────────────────


@router.post('/sync')
async def force_sync(_user=Depends(require_permission('admin'))):
    """Принудительная синхронизация с Remnawave."""
    headers = build_custom_headers()
    native = build_native_fields()
    try:
        ok, total = await sync_to_remnawave()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Ошибка синхронизации: {e}') from e

    return {
        'ok': ok,
        'total': total,
        'headers_count': len(headers),
        'native_fields': list(native.keys()),
        'headers_preview': {k: str(v)[:50] for k, v in list(headers.items())[:10]},
    }


@router.post('/cleanup')
async def cleanup_headers(_user=Depends(require_permission('admin'))):
    """Очистить все Happ-заголовки из Remnawave."""
    try:
        ok, total = await cleanup_remnawave_headers()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Ошибка очистки: {e}') from e
    return {'ok': ok, 'total': total}


@router.post('/assign-users')
async def assign_users(_user=Depends(require_permission('admin'))):
    """Назначить неназначенных пользователей в провайдеры (External Squads)."""
    try:
        from app.services.happ_management.squad_manager import run_periodic_assignment

        assigned = await run_periodic_assignment()
    except Exception as e:
        logger.error('Ошибка назначения пользователей', error=e)
        assigned = 0
    return {'assigned': assigned}


@router.get('/squads/status')
async def get_squads_status(_user=Depends(require_permission('admin'))):
    """Статус External Squads по всем провайдерам."""
    try:
        from app.services.happ_management.squad_manager import get_status_for_all_panels

        statuses = await get_status_for_all_panels()
    except Exception as e:
        logger.error('Ошибка получения статуса сквадов', error=e)
        statuses = []
    return {'statuses': statuses}


# ─── Source squads ────────────────────────────────────────────────────────────


@router.get('/source-squads')
async def get_source_squads(_user=Depends(require_permission('admin'))):
    """Список сквадов-источников для перетягивания пользователей."""
    return {'source_squads': cfg.get_source_squads()}


@router.post('/source-squads', status_code=201)
async def add_source_squad(body: HappSourceSquadAdd, _user=Depends(require_permission('admin'))):
    """Добавить сквад в список источников."""
    added = cfg.add_source_squad(body.uuid, body.name)
    if not added:
        raise HTTPException(status_code=409, detail='Сквад уже в списке источников')
    return {'uuid': body.uuid, 'added': True}


@router.delete('/source-squads/{squad_uuid}')
async def remove_source_squad(squad_uuid: str, _user=Depends(require_permission('admin'))):
    """Убрать сквад из списка источников."""
    removed = cfg.remove_source_squad(squad_uuid)
    if not removed:
        raise HTTPException(status_code=404, detail='Сквад не найден в источниках')
    return {'uuid': squad_uuid, 'removed': True}


@router.delete('/source-squads')
async def clear_source_squads(_user=Depends(require_permission('admin'))):
    """Очистить весь список источников."""
    count = cfg.clear_source_squads()
    return {'removed': count}


@router.get('/external-squads')
async def get_external_squads(_user=Depends(require_permission('admin'))):
    """Все External Squads из Remnawave (для выбора источников)."""
    try:
        from app.services.happ_management.squad_manager import get_all_external_squads

        squads = await get_all_external_squads(exclude_bound=False)
    except Exception as e:
        logger.error('Ошибка получения списка сквадов', error=e)
        squads = []
    return {'squads': squads}


# ─── Headers preview ─────────────────────────────────────────────────────────


@router.get('/headers-preview')
async def get_headers_preview(_user=Depends(require_permission('admin'))):
    """Предпросмотр заголовков, которые будут отправлены в Remnawave."""
    headers = build_custom_headers()
    native = build_native_fields()
    return {
        'custom_response_headers': headers,
        'native_fields': native,
        'module_enabled': cfg.get('MODULE_ENABLED'),
    }


# ─── Export / Import ─────────────────────────────────────────────────────────


@router.get('/export')
async def export_settings(_user=Depends(require_permission('admin'))):
    """Экспортировать все настройки и провайдеров."""
    return cfg.export_all()


@router.post('/import')
async def import_settings(body: HappImportData, _user=Depends(require_permission('admin'))):
    """Импортировать настройки из бэкапа."""
    try:
        s_count, p_count = cfg.import_all(body.data)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f'Ошибка импорта: {e}') from e
    schedule_sync()
    return {'settings_imported': s_count, 'providers_imported': p_count}


# ─── Schema info ──────────────────────────────────────────────────────────────


@router.get('/schema')
async def get_schema(_user=Depends(require_permission('admin'))):
    """Полная схема настроек для построения UI."""
    return {
        'schema': cfg.SETTINGS_SCHEMA,
        'categories': cfg.CATEGORIES,
        'category_hints': cfg.CATEGORY_HINTS,
        'sections': cfg.SECTIONS,
        'section_order': cfg.SECTION_ORDER,
        'category_order': cfg.CATEGORY_ORDER,
        'choice_labels': cfg.CHOICE_LABELS,
    }
