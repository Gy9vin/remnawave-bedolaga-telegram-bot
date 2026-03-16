"""
Админский хендлер для управления модулем Happ App Management.

Позволяет настраивать заголовки Happ, синхронизировать с Remnawave,
управлять провайдерами, импортировать/экспортировать конфигурацию.
"""

import asyncio
import json
import re
from datetime import datetime
from html import escape as html_escape
from typing import Any

import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.services.happ_management import config as cfg
from app.services.happ_management.remnawave_sync import (
    cleanup_remnawave_headers,
    schedule_sync,
    sync_to_remnawave,
)
from app.utils.decorators import admin_required, error_handler


logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# FSM States
# ---------------------------------------------------------------------------


class HappManagementStates(StatesGroup):
    waiting_str_value = State()
    waiting_provider_id = State()
    waiting_import_file = State()
    waiting_counter_value = State()
    waiting_prov_str_value = State()
    waiting_autoreg_count = State()
    waiting_autoreg_domain = State()
    waiting_captcha_key = State()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENTITIES = {
    'announce': {
        'label': '\U0001f4e2 \u041e\u0431\u044a\u044f\u0432\u043b\u0435\u043d\u0438\u0435',
        'keys': ['ANNOUNCE_TEXT', 'ANNOUNCE_SCHEDULE_START', 'ANNOUNCE_SCHEDULE_END', 'ANNOUNCE_ONCE'],
        'section': 'client',
    },
    'info_banner': {
        'label': '\U0001fa67 \u0418\u043d\u0444\u043e-\u0431\u0430\u043d\u043d\u0435\u0440',
        'keys': ['SUB_INFO_TEXT', 'SUB_INFO_COLOR', 'SUB_INFO_BUTTON_TEXT', 'SUB_INFO_BUTTON_LINK'],
        'section': 'client',
    },
    'server_desc': {
        'label': '\U0001f4dd \u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435 \u0441\u0435\u0440\u0432\u0435\u0440\u0430',
        'keys': ['SERVER_DESCRIPTION'],
        'section': 'client',
    },
    'color_theme': {
        'label': '\U0001f3a8 \u0422\u0435\u043c\u0430 \u043e\u0444\u043e\u0440\u043c\u043b\u0435\u043d\u0438\u044f (iOS)',
        'keys': ['COLOR_PROFILE'],
        'section': 'client',
    },
    'sub_expire': {
        'label': '\u23f3 \u0418\u0441\u0442\u0435\u0447\u0435\u043d\u0438\u0435 \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0438',
        'keys': ['SUB_EXPIRE_ENABLED', 'SUB_EXPIRE_BUTTON_LINK', 'NOTIFICATION_SUBS_EXPIRE'],
        'section': 'client',
    },
    'update_beh': {
        'label': '\U0001f504 \u041e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435',
        'keys': ['AUTO_UPDATE_ENABLED', 'PROFILE_UPDATE_INTERVAL', 'AUTO_UPDATE_ON_OPEN'],
        'section': 'behavior',
    },
    'autoconnect': {
        'label': '\u26a1 \u0410\u0432\u0442\u043e\u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435',
        'keys': ['AUTOCONNECT_ENABLED', 'AUTOCONNECT_TYPE'],
        'section': 'behavior',
    },
    'ping': {
        'label': '\U0001f4f6 \u041f\u0438\u043d\u0433',
        'keys': ['PING_ONOPEN_ENABLED', 'PING_TYPE', 'PING_CHECK_URL', 'PING_RESULT'],
        'section': 'behavior',
    },
    'bypass': {
        'label': '\U0001f6e1 \u041e\u0431\u0445\u043e\u0434 \u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u043e\u043a',
        'keys': [
            'FRAGMENTATION_ENABLED',
            'FRAGMENTATION_PACKETS',
            'FRAGMENTATION_LENGTH',
            'FRAGMENTATION_INTERVAL',
            'FRAGMENTATION_MAXSPLIT',
            'NOISES_ENABLED',
            'NOISES_TYPE',
            'NOISES_PACKET',
            'NOISES_DELAY',
            'NOISES_APPLYTO',
            'CHANGE_USER_AGENT',
        ],
        'section': 'behavior',
    },
    'mux': {
        'label': '\U0001f310 \u041c\u0443\u043b\u044c\u0442\u0438\u043f\u043b\u0435\u043a\u0441\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435',
        'keys': ['MUX_ENABLED', 'MUX_TCP_CONNECTIONS', 'MUX_XUDP_CONNECTIONS', 'MUX_QUIC'],
        'section': 'behavior',
    },
    'security': {
        'label': '\U0001f510 \u0411\u0435\u0437\u043e\u043f\u0430\u0441\u043d\u043e\u0441\u0442\u044c',
        'keys': ['HIDE_SERVER_SETTINGS', 'ALWAYS_HWID_ENABLED', 'DISABLE_COLLAPSE'],
        'section': 'main',
    },
}

SECTION_ENTITIES = {
    'client': ['announce', 'info_banner', 'server_desc', 'color_theme', 'sub_expire'],
    'behavior': ['update_beh', 'autoconnect', 'ping', 'bypass', 'mux'],
}

BASICS_KEYS = {'MODULE_ENABLED', 'HAPP_PROVIDER_ID', 'REMNAWAVE_SYNC_ENABLED'}

PROVIDERS_PER_PAGE = 4


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _status_icon(key: str) -> str:
    """Returns a status icon based on the current value of a bool setting."""
    val = cfg.get(key)
    schema = cfg.SETTINGS_SCHEMA.get(key, {})
    if schema.get('type') == 'bool':
        return '\U0001f7e2' if val else '\u26aa\ufe0f'
    if schema.get('type') == 'choice':
        return '\U0001f535' if val else '\u26aa\ufe0f'
    if schema.get('type') == 'str':
        return '\u2705' if val else '\u2796'
    return ''


def _format_value(key: str, value: Any) -> str:
    """Форматирует значение для отображения в меню."""
    schema = cfg.SETTINGS_SCHEMA.get(key, {})
    stype = schema.get('type', 'str')
    if stype == 'bool':
        return '\U0001f7e2 Вкл' if value else '\u26aa\ufe0f Выкл'
    if stype == 'choice':
        label = cfg.get_choice_label(key, str(value) if value else '')
        return label or str(value) or '\u2014'
    if not value:
        return '\u2014 (пусто)'
    text = str(value)
    if len(text) > 40:
        text = text[:37] + '...'
    return html_escape(text)


def _entity_summary(ent_key: str) -> str:
    """Returns a one-line summary for an entity group."""
    ent = ENTITIES.get(ent_key)
    if not ent:
        return ''
    keys = ent['keys']
    active = 0
    for k in keys:
        schema = cfg.SETTINGS_SCHEMA.get(k, {})
        val = cfg.get(k)
        if (schema.get('type') == 'bool' and val) or (schema.get('type') in ('str', 'choice') and val):
            active += 1
    total = len(keys)
    return f'{active}/{total}'


def _build_main_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Главное меню Happ Management."""
    enabled = cfg.get('MODULE_ENABLED')
    providers = cfg.get_providers()
    text = '<b>\U0001f4e1 Happ App Management</b>\n\n'
    text += f'{"\u2705 Модуль включён" if enabled else "\u274c Модуль выключен"}\n'
    if enabled:
        active = []
        if cfg.get('SUB_INFO_TEXT'):
            active.append('инфо-баннер')
        if cfg.get('SUB_EXPIRE_ENABLED'):
            active.append('истечение')
        if cfg.get('AUTOCONNECT_ENABLED'):
            active.append('автоподключение')
        if cfg.get('FRAGMENTATION_ENABLED'):
            active.append('фрагментация')
        if cfg.get('MUX_ENABLED'):
            active.append('mux')
        if providers:
            active.append(f'{len(providers)} Provider ID')
        if active:
            text += f'\u26a1 {", ".join(active)}\n'
    else:
        text += '\n<i>Заголовки не добавляются.</i>\n'
    builder = InlineKeyboardBuilder()
    builder.button(text='\u2705 Модуль вкл.' if enabled else '\u274c Модуль выкл.', callback_data='happ_module_toggle')
    builder.button(text='\U0001f464 Для клиента', callback_data='happ_section_client')
    builder.button(text='\u26a1 Поведение', callback_data='happ_section_behavior')
    builder.button(text='\U0001f510 Безопасность', callback_data='happ_entity_security')
    builder.button(text='\U0001f4e6 Импорт / Экспорт', callback_data='happ_backup')
    builder.button(text='\u2699\ufe0f Настройки модуля', callback_data='happ_settings')
    builder.button(text='\u2b05\ufe0f Назад', callback_data='admin_panel')
    builder.adjust(1)
    return text, builder.as_markup()


def _build_settings_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Меню настроек — секции + быстрые переключатели."""
    lines = [
        '<b>\u2699\ufe0f Настройки Happ</b>',
        '',
    ]

    # Quick toggles for basics
    for key in ('MODULE_ENABLED', 'REMNAWAVE_SYNC_ENABLED'):
        schema = cfg.SETTINGS_SCHEMA.get(key, {})
        icon = _status_icon(key)
        lines.append(f'{icon} {schema.get("label", key)}')

    pid = cfg.get('HAPP_PROVIDER_ID')
    lines.append(
        f'\U0001f511 Provider ID: <code>{html_escape(pid)}</code>' if pid else '\U0001f511 Provider ID: \u2014'
    )

    builder = InlineKeyboardBuilder()

    # Quick toggles
    for key in ('MODULE_ENABLED', 'REMNAWAVE_SYNC_ENABLED'):
        schema = cfg.SETTINGS_SCHEMA.get(key, {})
        val = cfg.get(key)
        icon = '\U0001f7e2' if val else '\u26aa\ufe0f'
        builder.row(
            InlineKeyboardButton(
                text=f'{icon} {schema.get("label", key)}',
                callback_data=f'happ_qtoggle_{key}',
            )
        )

    # Sections
    builder.row(
        InlineKeyboardButton(text='\U0001f464 Для клиента', callback_data='happ_section_client'),
        InlineKeyboardButton(text='\u26a1 Поведение', callback_data='happ_section_behavior'),
    )

    # Entity groups from 'main' section (security)
    builder.row(
        InlineKeyboardButton(
            text=f'\U0001f510 Безопасность ({_entity_summary("security")})',
            callback_data='happ_entity_security',
        )
    )

    # Remnawave + Providers
    builder.row(InlineKeyboardButton(text='\U0001f310 Remnawave', callback_data='happ_remnawave'))
    builder.row(InlineKeyboardButton(text='\U0001f511 Провайдеры', callback_data='happ_providers'))

    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data='happ_main'))

    return '\n'.join(lines), builder.as_markup()


def _build_section_menu(section_key: str) -> tuple[str, InlineKeyboardMarkup]:
    """Меню секции (client / behavior) — список entity-групп."""
    section_label = {'client': '\U0001f464 Для клиента', 'behavior': '\u26a1 Поведение'}.get(section_key, section_key)
    lines = [f'<b>{section_label}</b>', '']

    entity_keys = SECTION_ENTITIES.get(section_key, [])
    builder = InlineKeyboardBuilder()

    for ent_key in entity_keys:
        ent = ENTITIES.get(ent_key, {})
        summary = _entity_summary(ent_key)
        builder.row(
            InlineKeyboardButton(
                text=f'{ent.get("label", ent_key)} ({summary})',
                callback_data=f'happ_entity_{ent_key}',
            )
        )

    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data='happ_settings'))

    return '\n'.join(lines), builder.as_markup()


def _build_entity_menu(entity_key: str) -> tuple[str, InlineKeyboardMarkup]:
    """Меню настроек entity-группы (например, bypass, mux, announce)."""
    ent = ENTITIES.get(entity_key)
    if not ent:
        return 'Entity not found', InlineKeyboardMarkup(inline_keyboard=[])

    label = ent['label']
    keys = ent['keys']
    section = ent.get('section', 'client')

    lines = [f'<b>{label}</b>', '']

    for key in keys:
        schema = cfg.SETTINGS_SCHEMA.get(key, {})
        if not schema:
            continue
        val = cfg.get(key)
        dep_met = cfg.is_dependency_met(key)
        dim = '' if dep_met else '\U0001f6ab '
        lines.append(f'{dim}<b>{schema.get("label", key)}:</b> {_format_value(key, val)}')
        hint = schema.get('hint', '')
        if hint:
            lines.append(f'  <i>{hint}</i>')
        lines.append('')

    builder = InlineKeyboardBuilder()

    for key in keys:
        schema = cfg.SETTINGS_SCHEMA.get(key, {})
        if not schema:
            continue
        stype = schema.get('type', 'str')
        slabel = schema.get('label', key)

        if stype == 'bool':
            val = cfg.get(key)
            icon = '\U0001f7e2' if val else '\u26aa\ufe0f'
            builder.row(InlineKeyboardButton(text=f'{icon} {slabel}', callback_data=f'happ_toggle_{key}'))
        elif stype == 'choice':
            val = cfg.get(key)
            choice_label = cfg.get_choice_label(key, str(val) if val else '')
            builder.row(
                InlineKeyboardButton(
                    text=f'\U0001f535 {slabel}: {choice_label}',
                    callback_data=f'happ_choice_{key}',
                )
            )
        else:  # str
            builder.row(InlineKeyboardButton(text=f'\u270f\ufe0f {slabel}', callback_data=f'happ_edit_{key}'))

    # Special: color_theme entity gets a reset button
    if entity_key == 'color_theme':
        builder.row(InlineKeyboardButton(text='\U0001f504 Сбросить тему', callback_data='happ_color_reset'))

    # Back button
    back_data = f'happ_section_{section}' if section in SECTION_ENTITIES else 'happ_settings'
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data=back_data))

    return '\n'.join(lines), builder.as_markup()


def _build_remnawave_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Меню синхронизации с Remnawave."""
    sync_on = cfg.get('REMNAWAVE_SYNC_ENABLED')
    reassign = cfg.get('REASSIGN_FROM_FOREIGN_SQUADS')
    source_squads = cfg.get_source_squads()
    text = '<b>\U0001f517 Remnawave</b>\n\n'
    text += f'Синхронизация: {"\u2705 включена" if sync_on else "\u274c выключена"}\n'
    if reassign:
        if source_squads:
            text += f'Забирать из сквадов: <b>{len(source_squads)} шт.</b>\n'
        else:
            text += 'Забирать из сквадов: <b>из всех</b>\n'
    else:
        text += 'Забирать из чужих сквадов: \u274c\n'
    text += '\n'
    if sync_on:
        text += 'Happ-заголовки автоматически отправляются в Remnawave.\n'
    else:
        text += 'Включите синхронизацию, чтобы заголовки отправлялись автоматически.\n'
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f'{"\u2705" if sync_on else "\u274c"} Синхронизация',
        callback_data='happ_qtoggle_REMNAWAVE_SYNC_ENABLED',
    )
    builder.button(
        text=f'{"\u2705" if reassign else "\u274c"} Забирать из чужих сквадов',
        callback_data='happ_qtoggle_REASSIGN_FROM_FOREIGN_SQUADS',
    )
    builder.button(text='\U0001f4cb Источники пользователей', callback_data='happ_source_squads_0')
    builder.button(text='\U0001f504 Синхронизировать сейчас', callback_data='happ_force_sync')
    builder.button(text='\U0001f9f9 Очистить заголовки', callback_data='happ_cleanup_remna')
    builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_settings')
    builder.adjust(1)
    return text, builder.as_markup()


def _build_backup_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Меню бэкапа настроек."""
    lines = [
        '<b>\U0001f4be Бэкап настроек</b>',
        '',
        'Экспорт — скачать все настройки и провайдеров в JSON-файл.',
        'Импорт — загрузить настройки из JSON-файла.',
    ]

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='\U0001f4e4 Экспорт', callback_data='happ_export'))
    builder.row(InlineKeyboardButton(text='\U0001f4e5 Импорт', callback_data='happ_import'))
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data='happ_main'))

    return '\n'.join(lines), builder.as_markup()


def _build_providers_menu(page=0) -> tuple[str, InlineKeyboardMarkup]:
    """Меню провайдеров с пагинацией."""
    providers = cfg.get_providers()
    total_prov = len(providers)
    total_pages = max(1, (total_prov + PROVIDERS_PER_PAGE - 1) // PROVIDERS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    text = '<b>\U0001f511 Provider ID</b>\n'
    if not providers:
        text += '\nОдин Provider ID поддерживает до 100 устройств.\n<i>Нет добавленных Provider ID.</i>\n\n'
    else:
        start = page * PROVIDERS_PER_PAGE
        end = min(start + PROVIDERS_PER_PAGE, total_prov)
        if total_pages > 1:
            text += f'<i>Стр. {page + 1}/{total_pages} \u00b7 всего: {total_prov}</i>\n'
        text += '\n'
        for idx, p in enumerate(providers[start:end], start + 1):
            pid = p.get('provider_id', '?')
            total = p.get('total_assigned', 0)
            managed = p.get('managed', True)
            overrides = p.get('overrides') or {}
            m_icon = '\U0001f527' if managed else '\U0001f512'
            if total >= 100:
                fill_tag = '\U0001f534'
            elif total >= 90:
                fill_tag = '\U0001f7e1'
            elif total >= 50:
                fill_tag = '\U0001f7e2'
            else:
                fill_tag = '\u26aa'
            line = f'{idx}. {m_icon} <code>{pid}</code>  {fill_tag} {total}/100 Happ'
            tags = []
            if not managed:
                tags.append('не упр.')
            if overrides:
                tags.append(f'{len(overrides)} переопр.')
            cs = p.get('custom_squad')
            if isinstance(cs, dict) and cs.get('uuid'):
                tags.append(f'\U0001f517 {cs.get("name", "?")[:15]}')
            if tags:
                line += f'  <i>{", ".join(tags)}</i>'
            text += line + '\n'
        text += '\n'
    builder = InlineKeyboardBuilder()
    rows = []
    builder.button(text='\u2795 Добавить', callback_data='happ_prov_add')
    rows.append(1)
    if providers:
        start = page * PROVIDERS_PER_PAGE
        end = min(start + PROVIDERS_PER_PAGE, total_prov)
        prov_btns = 0
        for p in providers[start:end]:
            pid = p.get('provider_id', '?')
            managed = p.get('managed', True)
            overrides = p.get('overrides') or {}
            ovr_label = f' ({len(overrides)})' if overrides else ''
            m_icon = '\U0001f527' if managed else '\U0001f512'
            builder.button(text=f'{m_icon} {pid}{ovr_label}', callback_data=f'happ_pcfg|{pid}')
            prov_btns += 1
        rows.extend([2] * ((prov_btns + 1) // 2))
        if total_pages > 1:
            nav_btns = 0
            if page > 0:
                builder.button(text=f'\u25c0\ufe0f {page}/{total_pages}', callback_data=f'happ_prov_pg_{page - 1}')
                nav_btns += 1
            builder.button(text=f'\u00b7 {page + 1}/{total_pages} \u00b7', callback_data='noop')
            nav_btns += 1
            if page < total_pages - 1:
                builder.button(text=f'{page + 2}/{total_pages} \u25b6\ufe0f', callback_data=f'happ_prov_pg_{page + 1}')
                nav_btns += 1
            rows.append(nav_btns)
        accounts = cfg.get_accounts()
        acc_label = f'\U0001f916 Авторег / Аккаунты ({len(accounts)})' if accounts else '\U0001f916 Авторегистрация'
        builder.button(text='\u2699\ufe0f Действия', callback_data='happ_prov_actions')
        builder.button(text=acc_label, callback_data='happ_prov_autoreg_menu')
        rows.append(2)
    else:
        builder.button(text='\U0001f916 Авторегистрация', callback_data='happ_autoreg')
        rows.append(1)
    builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_settings')
    rows.append(1)
    builder.adjust(*rows)
    return text, builder.as_markup()


def _build_provider_config_menu(provider_id: str) -> tuple[str, InlineKeyboardMarkup]:
    """Меню конфигурации конкретного провайдера (legacy)."""
    providers = cfg.get_providers()
    provider = None
    for p in providers:
        if p.get('provider_id') == provider_id:
            provider = p
            break

    if not provider:
        return f'Провайдер <code>{html_escape(provider_id)}</code> не найден.', InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data='happ_providers')]]
        )

    managed = provider.get('managed', True)
    count = provider.get('total_assigned', 0)
    squad_uuid = provider.get('squad_uuid', '\u2014')
    cs = provider.get('custom_squad')
    overrides = provider.get('overrides') or {}

    lines = [
        f'<b>\u2699\ufe0f Провайдер: <code>{html_escape(provider_id)}</code></b>',
        '',
        f'<b>Managed:</b> {"\U0001f7e2 Да" if managed else "\u26aa\ufe0f Нет"}',
        f'<b>Назначено:</b> {count}/100',
        f'<b>Squad UUID:</b> <code>{html_escape(str(squad_uuid or "\u2014"))}</code>',
    ]

    if isinstance(cs, dict) and cs.get('name'):
        lines.append(f'<b>Custom Squad:</b> {html_escape(cs["name"])}')

    if overrides:
        lines.append('')
        lines.append(f'<b>Переопределений:</b> {len(overrides)}')

    builder = InlineKeyboardBuilder()
    managed_text = '\u26aa\ufe0f Снять управление' if managed else '\U0001f7e2 Управлять'
    builder.row(
        InlineKeyboardButton(
            text=managed_text,
            callback_data=f'happ_ptoggle_managed_{provider_id}',
        )
    )
    builder.row(
        InlineKeyboardButton(
            text='\U0001f5d1 Удалить провайдера',
            callback_data=f'happ_pdel_{provider_id}',
        )
    )
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data='happ_providers'))

    return '\n'.join(lines), builder.as_markup()


def _build_choice_menu(key: str) -> tuple[str, InlineKeyboardMarkup]:
    """Меню выбора значения для choice-параметра."""
    schema = cfg.SETTINGS_SCHEMA.get(key, {})
    choices = schema.get('choices', [])
    current = cfg.get(key)

    lines = [
        f'<b>{schema.get("label", key)}</b>',
        '',
        f'<i>{schema.get("hint", "")}</i>',
        '',
        f'Текущее: <b>{cfg.get_choice_label(key, str(current) if current else "")}</b>',
    ]

    builder = InlineKeyboardBuilder()
    for ch in choices:
        label = cfg.get_choice_label(key, ch)
        icon = '\U0001f535' if str(current) == str(ch) else '\u26aa\ufe0f'
        builder.row(
            InlineKeyboardButton(
                text=f'{icon} {label}',
                callback_data=f'happ_set_choice_{key}_{ch}',
            )
        )

    # Back: figure out which entity this key belongs to
    back_data = _back_data_for_key(key)
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data=back_data))

    return '\n'.join(lines), builder.as_markup()


def _back_data_for_key(key: str) -> str:
    """Определяет callback_data кнопки 'Назад' для ключа настройки."""
    for ent_key, ent in ENTITIES.items():
        if key in ent['keys']:
            return f'happ_entity_{ent_key}'
    return 'happ_settings'


# ---------------------------------------------------------------------------
# New builder functions
# ---------------------------------------------------------------------------

DEL_PROVIDERS_PER_PAGE = 8


def _build_delete_providers_page(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    providers = cfg.get_providers()
    total = len(providers)
    total_pages = max(1, (total + DEL_PROVIDERS_PER_PAGE - 1) // DEL_PROVIDERS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    builder = InlineKeyboardBuilder()
    rows: list[int] = []
    suffix = f'  ({page + 1}/{total_pages})' if total_pages > 1 else ''
    text = f'<b>\U0001f5d1 Удалить Provider ID</b>{suffix}\n\nВыберите провайдера для удаления:'
    start = page * DEL_PROVIDERS_PER_PAGE
    end = min(start + DEL_PROVIDERS_PER_PAGE, total)
    for p in providers[start:end]:
        pid = p.get('provider_id', '?')
        builder.button(text=f'\u274c {pid}', callback_data=f'happ_prov_del|{pid}')
    rows.extend([1] * (end - start))
    if total_pages > 1:
        nav_btns = 0
        if page > 0:
            builder.button(text=f'\u25c0\ufe0f {page}/{total_pages}', callback_data=f'happ_del_pg_{page - 1}')
            nav_btns += 1
        builder.button(text=f'\u00b7 {page + 1}/{total_pages} \u00b7', callback_data='noop')
        nav_btns += 1
        if page < total_pages - 1:
            builder.button(text=f'{page + 2}/{total_pages} \u25b6\ufe0f', callback_data=f'happ_del_pg_{page + 1}')
            nav_btns += 1
        rows.append(nav_btns)
    builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_providers')
    rows.append(1)
    builder.adjust(*rows)
    return text, builder.as_markup()


COUNTER_PER_PAGE = 8


def _build_counter_page(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    providers = cfg.get_providers()
    total = len(providers)
    total_pages = max(1, (total + COUNTER_PER_PAGE - 1) // COUNTER_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    builder = InlineKeyboardBuilder()
    rows: list[int] = []
    text = '<b>\u270f\ufe0f Установить счётчик устройств</b>\n\nВыберите провайдера для корректировки:'
    start = page * COUNTER_PER_PAGE
    end = min(start + COUNTER_PER_PAGE, total)
    for p in providers[start:end]:
        pid = p.get('provider_id', '?')
        cnt = p.get('total_assigned', 0)
        builder.button(text=f'{pid} ({cnt}/100)', callback_data=f'happ_prov_counter|{pid}')
    rows.extend([1] * (end - start))
    if total_pages > 1:
        nav_btns = 0
        if page > 0:
            builder.button(text=f'\u25c0\ufe0f {page}/{total_pages}', callback_data=f'happ_cnt_pg_{page - 1}')
            nav_btns += 1
        builder.button(text=f'\u00b7 {page + 1}/{total_pages} \u00b7', callback_data='noop')
        nav_btns += 1
        if page < total_pages - 1:
            builder.button(text=f'{page + 2}/{total_pages} \u25b6\ufe0f', callback_data=f'happ_cnt_pg_{page + 1}')
            nav_btns += 1
        rows.append(nav_btns)
    builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_providers')
    rows.append(1)
    builder.adjust(*rows)
    return text, builder.as_markup()


def _provider_section_for_key(key: str) -> str:
    schema = cfg.SETTINGS_SCHEMA.get(key, {})
    cat = schema.get('category', '')
    if cat == 'security':
        return 'security'
    return cfg.CATEGORY_TO_SECTION.get(cat, 'client')


_PROVIDER_EXTRA_SECTIONS = {
    'security': {'label': '\U0001f510 Безопасность', 'categories': ['security']},
}


async def _build_provider_settings_menu(provider_id: str) -> tuple[str, InlineKeyboardMarkup]:
    providers = cfg.get_providers()
    provider = next((p for p in providers if p.get('provider_id') == provider_id), None)
    if not provider:
        builder = InlineKeyboardBuilder()
        builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_providers')
        return 'Провайдер не найден', builder.as_markup()
    managed = provider.get('managed', True)
    overrides = provider.get('overrides') or {}
    custom = cfg.get_custom_squad(provider_id)
    total_assigned = provider.get('total_assigned', 0)
    text = f'<b>\u2699\ufe0f Настройки провайдера <code>{html_escape(provider_id)}</code></b>\n\n'
    text += f'{"\U0001f527 Управляемый" if managed else "\U0001f512 Не управляется"}\n'
    if managed:
        text += 'Модуль синхронизирует заголовки этого сквада.\n'
    else:
        text += 'Модуль <b>не трогает</b> заголовки этого сквада.\n'
    if custom:
        text += f'\n\U0001f517 Привязан к скваду: <b>{html_escape(custom.get("name", "?"))}</b>\n'
        text += '<i>Сквад не удаляется вместе с Provider ID.</i>\n'
    else:
        text += f'\nСквад: авто (<code>Happ-{html_escape(provider_id)}</code>)\n'
    text += '\n<b>Счётчики:</b>\n'
    text += f'  \u2022 Happ устройства: <b>{total_assigned}/100</b>\n'
    if overrides:
        text += f'\n<b>Переопределений:</b> {len(overrides)}\n'
        for k in list(overrides.keys())[:5]:
            schema = cfg.SETTINGS_SCHEMA.get(k)
            if schema:
                text += f'  \u2022 {schema["label"]}\n'
    else:
        text += '\nПереопределений нет — используются глобальные настройки.\n'
    builder = InlineKeyboardBuilder()
    m_text = '\U0001f527 Управляемый: \u2705' if managed else '\U0001f512 Управляемый: \u274c'
    builder.button(text=m_text, callback_data=f'happ_ptm|{provider_id}')
    if custom:
        builder.button(
            text=f'\U0001f517 Сквад: {custom.get("name", "?")[:20]}  \u2716\ufe0f',
            callback_data=f'happ_unbind|{provider_id}',
        )
    else:
        builder.button(text='\U0001f517 Привязать к скваду', callback_data=f'happ_bind|{provider_id}|0')
    prov_sections = [
        ('client', cfg.SECTIONS.get('client', {})),
        ('behavior', cfg.SECTIONS.get('behavior', {})),
        ('security', {'label': '\U0001f510 Безопасность', 'categories': ['security']}),
    ]
    for section_key, section in prov_sections:
        categories = section.get('categories', [])
        if not categories:
            continue
        items = cfg.get_settings_by_categories_for_provider(categories, provider_id)
        if not items:
            continue
        overridden_count = sum(1 for _, _, _, is_ovr in items if is_ovr)
        slabel = section.get('label', section_key)
        if overridden_count:
            slabel += f'  ({overridden_count} переопр.)'
        builder.button(text=slabel, callback_data=f'happ_psec|{section_key}')
    if overrides:
        builder.button(text='\U0001f504 Сбросить все переопределения', callback_data=f'happ_prst|{provider_id}')
    builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_providers')
    builder.adjust(1)
    return text, builder.as_markup()


def _build_provider_section_menu(provider_id: str, section_key: str) -> tuple[str, InlineKeyboardMarkup]:
    section = cfg.SECTIONS.get(section_key) or _PROVIDER_EXTRA_SECTIONS.get(section_key)
    if not section:
        builder = InlineKeyboardBuilder()
        builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_pcfg_back')
        return 'Раздел не найден', builder.as_markup()
    items = cfg.get_settings_by_categories_for_provider(section.get('categories', []), provider_id)
    text = f'<b>{section.get("label", section_key)}</b>\nПровайдер: <code>{html_escape(provider_id)}</code>\n'
    builder = InlineKeyboardBuilder()
    for key, schema, value, is_overridden in items:
        if not cfg.is_dependency_met_for_provider(key, provider_id):
            continue
        ovr_mark = ' \u2b50' if is_overridden else ''
        stype = schema.get('type', 'str')
        slabel = schema.get('label', key)
        if stype == 'bool':
            icon = '\u2705' if value else '\u274c'
            builder.button(text=f'{icon} {slabel}{ovr_mark}', callback_data=f'happ_ptgl|{key}')
            text += f'\u2022 <b>{slabel}</b>{ovr_mark}\n'
        elif stype == 'choice':
            dv = cfg.get_choice_label(key, value)
            builder.button(text=f'\U0001f539 {slabel}{ovr_mark}', callback_data=f'happ_pcho|{key}')
            text += f'\u2022 <b>{slabel}</b>: {dv}{ovr_mark}\n'
        else:
            builder.button(text=f'\u270f\ufe0f {slabel}{ovr_mark}', callback_data=f'happ_pedt|{key}')
            if value:
                dv = str(value)[:50] + '\u2026' if len(str(value)) > 50 else str(value)
                text += f'\u2022 <b>{slabel}</b>: {html_escape(dv)}{ovr_mark}\n'
            else:
                text += f'\u2022 <b>{slabel}</b>: <i>не задано</i>{ovr_mark}\n'
    builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_pcfg_back')
    builder.adjust(1)
    return text, builder.as_markup()


BIND_SQUADS_PER_PAGE = 6


async def _build_bind_squad_screen(provider_id: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    try:
        from app.services.happ_management.squad_manager import get_all_external_squads

        all_squads = await get_all_external_squads(exclude_bound=False)
        all_squads.sort(key=lambda s: s.get('name', '').lower())
    except Exception:
        all_squads = []
    total = len(all_squads)
    total_pages = max(1, (total + BIND_SQUADS_PER_PAGE - 1) // BIND_SQUADS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    text = f'<b>\U0001f517 Привязка к скваду</b>\nProvider ID: <code>{html_escape(provider_id)}</code>\n\n'
    if not all_squads:
        text += '<i>На панели нет External Squads для привязки.</i>\n'
    else:
        text += 'Выберите сквад для привязки.\n'
        if total_pages > 1:
            text += f'<i>Стр. {page + 1}/{total_pages} \u00b7 всего: {total}</i>\n'
    builder = InlineKeyboardBuilder()
    start = page * BIND_SQUADS_PER_PAGE
    end = min(start + BIND_SQUADS_PER_PAGE, total)
    for sq in all_squads[start:end]:
        name = sq.get('name', '?')
        uuid = sq.get('uuid', '')
        members = sq.get('members_count', 0)
        label = f'{name[:25]}  ({members} чел.)' if members else name[:30]
        builder.button(text=label, callback_data=f'happ_bsel|{uuid}')
    if total_pages > 1:
        if page > 0:
            builder.button(
                text=f'\u25c0\ufe0f {page}/{total_pages}',
                callback_data=f'happ_bind|{provider_id}|{page - 1}',
            )
        builder.button(text=f'\u00b7 {page + 1}/{total_pages} \u00b7', callback_data='noop')
        if page < total_pages - 1:
            builder.button(
                text=f'{page + 2}/{total_pages} \u25b6\ufe0f',
                callback_data=f'happ_bind|{provider_id}|{page + 1}',
            )
    builder.button(text='\u2b05\ufe0f Назад', callback_data=f'happ_pcfg|{provider_id}')
    builder.adjust(1)
    return text, builder.as_markup()


SQUADS_PER_PAGE = 6


async def _build_source_squads_screen(page: int) -> tuple[str, InlineKeyboardMarkup]:
    try:
        from app.services.happ_management.squad_manager import get_all_external_squads

        all_squads = await get_all_external_squads()
    except Exception:
        all_squads = []
    source_uuids = cfg.get_source_squad_uuids()
    reassign = cfg.get('REASSIGN_FROM_FOREIGN_SQUADS')
    text = '<b>\U0001f4cb Источники пользователей</b>\n\n'
    if not reassign:
        text += '\u26a0\ufe0f Опция \u00abЗабирать из чужих сквадов\u00bb выключена.\n\n'
    builder = InlineKeyboardBuilder()
    btn_rows: list[int] = []
    if not all_squads:
        text += 'Внешних сквадов не найдено.\n<i>Наши managed-сквады (Happ-*) не показываются.</i>\n'
    else:
        total_squads = len(all_squads)
        total_pages = max(1, (total_squads + SQUADS_PER_PAGE - 1) // SQUADS_PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        start = page * SQUADS_PER_PAGE
        end = min(start + SQUADS_PER_PAGE, total_squads)
        if source_uuids:
            text += f'Выбрано: <b>{len(source_uuids)}</b> из {total_squads} сквадов\n'
        else:
            text += f'Всего сквадов: <b>{total_squads}</b> (ни один не выбран \u2192 из всех)\n'
        text += '<i>Наши managed-сквады (Happ-*) не показываются.</i>\n\nНажмите на сквад для включения/выключения:\n\n'
        for sq in all_squads[start:end]:
            is_src = sq['uuid'] in source_uuids
            icon = '\u2705' if is_src else '\u2b1c'
            text += f'{icon} <b>{html_escape(sq["name"])}</b> ({sq["members_count"]} чел.)\n'
            short_name = sq['name'][:20]
            builder.button(text=f'{icon} {short_name}', callback_data=f'happ_st|{sq["uuid"]}|{page}')
            btn_rows.append(1)
        if total_pages > 1:
            nav_btns = 0
            if page > 0:
                builder.button(
                    text=f'\u25c0\ufe0f {page}/{total_pages}',
                    callback_data=f'happ_source_squads_{page - 1}',
                )
                nav_btns += 1
            builder.button(text=f'\u00b7 {page + 1}/{total_pages} \u00b7', callback_data='noop')
            nav_btns += 1
            if page < total_pages - 1:
                builder.button(
                    text=f'{page + 2}/{total_pages} \u25b6\ufe0f',
                    callback_data=f'happ_source_squads_{page + 1}',
                )
                nav_btns += 1
            btn_rows.append(nav_btns)
        if source_uuids:
            builder.button(text='\U0001f5d1 Сбросить все источники', callback_data=f'happ_sc|{page}')
            btn_rows.append(1)
    builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_remnawave')
    btn_rows.append(1)
    builder.adjust(*btn_rows)
    return text, builder.as_markup()


ACCOUNTS_PER_PAGE = 3


def _build_accounts_page(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    accounts = cfg.get_accounts()
    total = len(accounts)
    total_pages = max(1, (total + ACCOUNTS_PER_PAGE - 1) // ACCOUNTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    builder = InlineKeyboardBuilder()
    if not accounts:
        text = '<b>\U0001f4cb Аккаунты happ-proxy.com</b>\n\nНет зарегистрированных аккаунтов.'
        builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_prov_autoreg_menu')
        builder.adjust(1)
        return text, builder.as_markup()
    start = page * ACCOUNTS_PER_PAGE
    end = min(start + ACCOUNTS_PER_PAGE, total)
    text = f'<b>\U0001f4cb Аккаунты happ-proxy.com</b>  ({page + 1}/{total_pages})\nВсего: {total}\n\n'
    for i, acc in enumerate(accounts[start:end], start + 1):
        pid = acc.get('provider_id', '?')
        email = acc.get('email', '?')
        password = acc.get('password', '')
        domain = acc.get('domain', '')
        reg_date = acc.get('registered_at', '')
        if reg_date:
            try:
                dt = datetime.fromisoformat(reg_date)
                reg_date = dt.strftime('%d.%m.%Y')
            except Exception:
                reg_date = reg_date[:10]
        text += f'{i}. <code>{pid}</code>\n   \U0001f4e7 {html_escape(email)}\n'
        if password:
            text += f'   \U0001f511 <tg-spoiler>{html_escape(password)}</tg-spoiler>\n'
        text += f'   \U0001f310 {html_escape(domain) if domain else "\u2014"}\n   \U0001f4c5 {reg_date}\n\n'
    rows = []
    del_count = 0
    for acc in accounts[start:end]:
        pid = acc.get('provider_id', '')
        if pid:
            builder.button(text=f'\U0001f5d1 {pid}', callback_data=f'happ_rm|{pid}|{page}')
            del_count += 1
    rows.extend([2] * ((del_count + 1) // 2))
    if total_pages > 1:
        nav_btns = 0
        if page > 0:
            builder.button(text=f'\u25c0\ufe0f {page}/{total_pages}', callback_data=f'happ_acc_pg_{page - 1}')
            nav_btns += 1
        builder.button(text=f'\u00b7 {page + 1}/{total_pages} \u00b7', callback_data='noop')
        nav_btns += 1
        if page < total_pages - 1:
            builder.button(text=f'{page + 2}/{total_pages} \u25b6\ufe0f', callback_data=f'happ_acc_pg_{page + 1}')
            nav_btns += 1
        rows.append(nav_btns)
    builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_prov_autoreg_menu')
    rows.append(1)
    builder.adjust(*rows)
    return text, builder.as_markup()


_CIRCLED = [
    '\u24ea',
    '\u2460',
    '\u2461',
    '\u2462',
    '\u2463',
    '\u2464',
    '\u2465',
    '\u2466',
    '\u2467',
    '\u2468',
    '\u2469',
]


def _bar(step: int, total: int, width: int = 14) -> str:
    filled = round(step / total * width) if total else 0
    return '\u25b0' * filled + '\u25b1' * (width - filled)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


@admin_required
@error_handler
async def show_happ_main(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.clear()
    text, markup = _build_main_menu()
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_happ_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.clear()
    text, markup = _build_settings_menu()
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_happ_section(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    section_key = callback.data.replace('happ_section_', '', 1)
    if section_key not in SECTION_ENTITIES:
        await callback.answer('Секция не найдена', show_alert=True)
        return
    text, markup = _build_section_menu(section_key)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_happ_entity(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    entity_key = callback.data.replace('happ_entity_', '', 1)
    if entity_key not in ENTITIES:
        await callback.answer('Группа не найдена', show_alert=True)
        return
    text, markup = _build_entity_menu(entity_key)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_happ_remnawave(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    text, markup = _build_remnawave_menu()
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_happ_backup(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    text, markup = _build_backup_menu()
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_happ_providers(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    text, markup = _build_providers_menu(page=0)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def providers_page(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    page_str = callback.data.replace('happ_prov_pg_', '', 1)
    try:
        page = int(page_str)
    except ValueError:
        page = 0
    text, markup = _build_providers_menu(page=page)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def toggle_module(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    current = cfg.get('MODULE_ENABLED')
    cfg.set_value('MODULE_ENABLED', not current)
    schedule_sync()
    text, markup = _build_main_menu()
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    status = 'включён' if not current else 'выключен'
    await callback.answer(f'Модуль {status}')


@admin_required
@error_handler
async def quick_toggle(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    key = callback.data.replace('happ_qtoggle_', '', 1)
    schema = cfg.SETTINGS_SCHEMA.get(key)
    if not schema or schema.get('type') != 'bool':
        await callback.answer('Параметр не найден', show_alert=True)
        return

    current = cfg.get(key)
    cfg.set_value(key, not current)
    schedule_sync()

    label = schema.get('label', key)
    status = 'включён' if not current else 'выключен'

    # Rebuild the menu the user was in
    if callback.data.endswith('REMNAWAVE_SYNC_ENABLED'):
        # Could be from settings or remnawave menu
        text, markup = _build_settings_menu()
    else:
        text, markup = _build_settings_menu()
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer(f'{label}: {status}')


@admin_required
@error_handler
async def toggle_bool(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    key = callback.data.replace('happ_toggle_', '', 1)
    schema = cfg.SETTINGS_SCHEMA.get(key)
    if not schema or schema.get('type') != 'bool':
        await callback.answer('Параметр не найден', show_alert=True)
        return

    current = cfg.get(key)
    cfg.set_value(key, not current)

    # If toggling ANNOUNCE_TEXT related bools and text was cleared
    if key == 'ANNOUNCE_ONCE' and not current and cfg.get('ANNOUNCE_TEXT'):
        pass  # Will be consumed on next sync

    schedule_sync()

    # Rebuild entity menu
    entity_key = None
    for ent_key, ent in ENTITIES.items():
        if key in ent['keys']:
            entity_key = ent_key
            break

    if entity_key:
        text, markup = _build_entity_menu(entity_key)
    else:
        text, markup = _build_settings_menu()

    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    label = schema.get('label', key)
    status = 'включён' if not current else 'выключен'
    await callback.answer(f'{label}: {status}')


@admin_required
@error_handler
async def show_choice(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    key = callback.data.replace('happ_choice_', '', 1)
    schema = cfg.SETTINGS_SCHEMA.get(key)
    if not schema or schema.get('type') != 'choice':
        await callback.answer('Параметр не найден', show_alert=True)
        return
    text, markup = _build_choice_menu(key)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def set_choice(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    # Format: happ_set_choice_{KEY}_{VALUE}
    raw = callback.data.replace('happ_set_choice_', '', 1)
    found_key = None
    found_value = None
    for schema_key in cfg.SETTINGS_SCHEMA:
        prefix = schema_key + '_'
        if raw == schema_key:
            found_key = schema_key
            found_value = ''
            break
        if raw.startswith(prefix):
            found_key = schema_key
            found_value = raw[len(prefix) :]
            break
    if not found_key:
        await callback.answer('Параметр не найден', show_alert=True)
        return

    cfg.set_value(found_key, found_value)
    schedule_sync()

    label = cfg.get_choice_label(found_key, found_value)
    text, markup = _build_choice_menu(found_key)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer(f'Установлено: {label}')


@admin_required
@error_handler
async def start_edit_str(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    key = callback.data.replace('happ_edit_', '', 1)
    schema = cfg.SETTINGS_SCHEMA.get(key)
    if not schema:
        await callback.answer('Параметр не найден', show_alert=True)
        return

    current = cfg.get(key)
    lines = [
        f'<b>\u270f\ufe0f {schema.get("label", key)}</b>',
        '',
        f'<i>{schema.get("hint", "")}</i>',
        '',
    ]
    if current:
        lines.append(f'Текущее: <code>{html_escape(str(current))}</code>')
    else:
        lines.append('Текущее: \u2014 (пусто)')
    lines.append('')
    lines.append('Отправьте новое значение или /cancel для отмены.')

    builder = InlineKeyboardBuilder()
    if current:
        builder.row(InlineKeyboardButton(text='\U0001f5d1 Очистить', callback_data=f'happ_clear_{key}'))
    back_data = _back_data_for_key(key)
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Назад', callback_data=back_data))

    await state.set_state(HappManagementStates.waiting_str_value)
    await state.update_data(happ_edit_key=key)

    await callback.message.edit_text('\n'.join(lines), reply_markup=builder.as_markup(), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def process_str_value(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    key = data.get('happ_edit_key')
    if not key:
        await state.clear()
        return

    if message.text and message.text.strip() == '/cancel':
        await state.clear()
        await message.answer('Отменено.')
        return

    value = (message.text or '').strip()
    schema = cfg.SETTINGS_SCHEMA.get(key, {})

    # Validate
    error = cfg.validate_value(key, value)
    if error:
        await message.answer(f'\u274c {error}\n\nПопробуйте снова или /cancel.', parse_mode='HTML')
        return

    cfg.set_value(key, value)
    schedule_sync()
    await state.clear()

    label = schema.get('label', key)
    if value:
        await message.answer(
            f'\u2705 <b>{label}</b> установлено:\n<code>{html_escape(value[:200])}</code>',
            parse_mode='HTML',
        )
    else:
        await message.answer(f'\u2705 <b>{label}</b> очищено.', parse_mode='HTML')


@admin_required
@error_handler
async def clear_str_value(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    key = callback.data.replace('happ_clear_', '', 1)
    schema = cfg.SETTINGS_SCHEMA.get(key, {})

    # If clearing announce text, mark for clear in remnawave
    if key == 'ANNOUNCE_TEXT' and cfg.get('ANNOUNCE_TEXT'):
        cfg.mark_announce_clear()

    cfg.set_value(key, schema.get('default', ''))
    schedule_sync()
    await state.clear()

    # Return to entity menu
    entity_key = None
    for ent_key, ent in ENTITIES.items():
        if key in ent['keys']:
            entity_key = ent_key
            break

    if entity_key:
        text, markup = _build_entity_menu(entity_key)
        await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    label = schema.get('label', key)
    await callback.answer(f'{label} очищено')


@admin_required
@error_handler
async def force_sync(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    if not cfg.get('REMNAWAVE_SYNC_ENABLED'):
        await callback.answer('Синхронизация выключена. Включите в настройках.', show_alert=True)
        return

    await callback.answer('Синхронизация запущена...')

    try:
        success, total = await sync_to_remnawave()
        result_text = f'\u2705 Синхронизация: {success}/{total}'
        if success < total:
            result_text = f'\u26a0\ufe0f Синхронизация: {success}/{total} (есть ошибки)'
    except Exception as e:
        logger.error(f'[HappHandler] force_sync error: {e}', exc_info=True)
        result_text = f'\u274c Ошибка синхронизации: {e}'

    text, markup = _build_remnawave_menu()
    lines = text.split('\n')
    lines.append('')
    lines.append(f'<b>Результат:</b> {result_text}')
    await callback.message.edit_text('\n'.join(lines), reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def cleanup_remna(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await callback.answer('Очистка заголовков...')

    try:
        success, total = await cleanup_remnawave_headers()
        result_text = f'\u2705 Очищено: {success}/{total}'
        if success < total:
            result_text = f'\u26a0\ufe0f Очищено: {success}/{total} (есть ошибки)'
    except Exception as e:
        logger.error(f'[HappHandler] cleanup error: {e}', exc_info=True)
        result_text = f'\u274c Ошибка: {e}'

    text, markup = _build_remnawave_menu()
    lines = text.split('\n')
    lines.append('')
    lines.append(f'<b>Результат:</b> {result_text}')
    await callback.message.edit_text('\n'.join(lines), reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def start_add_provider(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(HappManagementStates.waiting_provider_id)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Отмена', callback_data='happ_providers'))

    await callback.message.edit_text(
        '<b>\u2795 Добавление провайдера</b>\n\n'
        'Отправьте Provider ID (например: <code>nS5jOH5b</code>).\n'
        "Получите его на <a href='https://happ-proxy.com'>happ-proxy.com</a>.\n\n"
        'Или /cancel для отмены.',
        reply_markup=builder.as_markup(),
        parse_mode='HTML',
        disable_web_page_preview=True,
    )
    await callback.answer()


@admin_required
@error_handler
async def process_add_provider(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    if message.text and message.text.strip() == '/cancel':
        await state.clear()
        await message.answer('Отменено.')
        return

    provider_id = (message.text or '').strip()
    if not provider_id:
        await message.answer('\u274c Пустой Provider ID. Попробуйте снова или /cancel.')
        return

    if not re.match(r'^[a-zA-Z0-9_-]+$', provider_id):
        await message.answer(
            '\u274c Provider ID может содержать только латинские буквы, цифры, - и _.\nПопробуйте снова или /cancel.'
        )
        return

    added = cfg.add_provider(provider_id)
    await state.clear()

    if added:
        schedule_sync()
        await message.answer(
            f'\u2705 Провайдер <code>{html_escape(provider_id)}</code> добавлен.\n'
            'Сквад будет создан при следующей синхронизации.',
            parse_mode='HTML',
        )
    else:
        await message.answer(
            f'\u26a0\ufe0f Провайдер <code>{html_escape(provider_id)}</code> уже существует.',
            parse_mode='HTML',
        )


@admin_required
@error_handler
async def show_provider_config(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    provider_id = callback.data.replace('happ_pcfg_', '', 1)
    text, markup = _build_provider_config_menu(provider_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def toggle_provider_managed(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    provider_id = callback.data.replace('happ_ptoggle_managed_', '', 1)
    current = cfg.is_provider_managed(provider_id)
    cfg.set_provider_managed(provider_id, not current)
    schedule_sync()

    text, markup = _build_provider_config_menu(provider_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    status = 'включено' if not current else 'выключено'
    await callback.answer(f'Управление заголовками: {status}')


@admin_required
@error_handler
async def delete_provider(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    provider_id = callback.data.replace('happ_pdel_', '', 1)
    removed = cfg.remove_provider(provider_id)

    if removed:
        schedule_sync()
        await callback.answer(f'Провайдер {provider_id} удалён')
    else:
        await callback.answer('Провайдер не найден', show_alert=True)

    text, markup = _build_providers_menu(page=0)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def assign_users_now(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    providers = cfg.get_providers()
    if not providers:
        await callback.answer('Нет провайдеров', show_alert=True)
        return

    if not cfg.get('REMNAWAVE_SYNC_ENABLED'):
        await callback.answer('Синхронизация выключена', show_alert=True)
        return

    await callback.answer('Назначение пользователей запущено...')

    try:
        from app.services.happ_management.squad_manager import run_periodic_assignment

        assigned = await run_periodic_assignment()
        result = f'\u2705 Назначено: {assigned}'
    except Exception as e:
        logger.error(f'[HappHandler] assign error: {e}', exc_info=True)
        result = f'\u274c Ошибка: {e}'

    text, markup = _build_providers_menu(page=0)
    lines = text.split('\n')
    lines.append('')
    lines.append(f'<b>Результат:</b> {result}')
    await callback.message.edit_text('\n'.join(lines), reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def export_settings(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    try:
        data = cfg.export_all()
        content = json.dumps(data, ensure_ascii=False, indent=2)
        file = BufferedInputFile(
            content.encode('utf-8'),
            filename='happ_management_backup.json',
        )
        await callback.message.answer_document(file, caption='\U0001f4be Бэкап настроек Happ Management')
        await callback.answer()
    except Exception as e:
        logger.error(f'[HappHandler] export error: {e}', exc_info=True)
        await callback.answer(f'Ошибка экспорта: {e}', show_alert=True)


@admin_required
@error_handler
async def start_import(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(HappManagementStates.waiting_import_file)

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text='\u2b05\ufe0f Отмена', callback_data='happ_backup'))

    await callback.message.edit_text(
        '<b>\U0001f4e5 Импорт настроек</b>\n\nОтправьте JSON-файл с бэкапом настроек.\n\nИли /cancel для отмены.',
        reply_markup=builder.as_markup(),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_import_file(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    if message.text and message.text.strip() == '/cancel':
        await state.clear()
        await message.answer('Отменено.')
        return

    if not message.document:
        await message.answer('\u274c Отправьте JSON-файл или /cancel.')
        return

    try:
        file = await message.bot.download(message.document)
        content = file.read().decode('utf-8')
        data = json.loads(content)
    except json.JSONDecodeError:
        await message.answer('\u274c Невалидный JSON. Попробуйте другой файл или /cancel.')
        return
    except Exception as e:
        await message.answer(f'\u274c Ошибка чтения файла: {e}')
        return

    if not isinstance(data, dict):
        await message.answer('\u274c Файл должен содержать JSON-объект.')
        return

    try:
        s_count, p_count = cfg.import_all(data)
        schedule_sync()
        await state.clear()
        await message.answer(
            f'\u2705 Импорт завершён!\nНастроек: {s_count}\nПровайдеров: {p_count}',
            parse_mode='HTML',
        )
    except Exception as e:
        logger.error(f'[HappHandler] import error: {e}', exc_info=True)
        await message.answer(f'\u274c Ошибка импорта: {e}')


# ---------------------------------------------------------------------------
# New handlers
# ---------------------------------------------------------------------------


@admin_required
@error_handler
async def noop_handler(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await callback.answer()


@admin_required
@error_handler
async def reset_color_profile(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    cfg.set_value('COLOR_PROFILE', 'resetcolors')
    schedule_sync()
    await state.clear()
    await callback.answer('Тема будет сброшена при следующем обновлении подписки', show_alert=True)
    builder = InlineKeyboardBuilder()
    builder.button(text='\U0001f5d1 Очистить (перестать сбрасывать)', callback_data='happ_clear_COLOR_PROFILE')
    builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_entity_color_theme')
    builder.adjust(1)
    await callback.message.edit_text(
        '<b>\U0001f504 Сброс темы</b>\n\nЗначение <code>resetcolors</code> установлено.\n'
        'При следующем обновлении подписки тема на устройствах будет сброшена.',
        reply_markup=builder.as_markup(),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def show_provider_actions(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    remna_sync = cfg.get('REMNAWAVE_SYNC_ENABLED')
    builder = InlineKeyboardBuilder()
    if remna_sync:
        builder.button(text='\U0001f4ca Статус', callback_data='happ_prov_status')
    builder.button(text='\U0001f465 Назначить пользователей', callback_data='happ_prov_assign')
    if remna_sync:
        builder.button(text='\U0001f504 Синхр. сквады', callback_data='happ_prov_sync')
    builder.button(text='\U0001f9ee Счётчик устройств', callback_data='happ_prov_counter_list')
    builder.button(text='\U0001f5d1 Удалить провайдер', callback_data='happ_prov_del_list')
    builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_providers')
    builder.adjust(1)
    await callback.message.edit_text(
        '<b>\u2699\ufe0f Действия с провайдерами</b>\n\nВыберите действие:',
        reply_markup=builder.as_markup(),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def show_autoreg_menu(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    accounts = cfg.get_accounts()
    builder = InlineKeyboardBuilder()
    builder.button(text='\U0001f916 Авторегистрация', callback_data='happ_autoreg')
    if accounts:
        builder.button(text=f'\U0001f4cb Аккаунты ({len(accounts)})', callback_data='happ_accounts')
    builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_providers')
    builder.adjust(1)
    text = (
        f'<b>\U0001f916 Авторегистрация / Аккаунты</b>\n\n'
        f'Зарегистрировано аккаунтов: <b>{len(accounts)}</b>\n\n'
        'Авторегистрация создаёт аккаунты на happ-proxy.com,\n'
        'привязывает домен и добавляет Provider ID в модуль.'
    )
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def refresh_providers_status(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    page = data.get('happ_prov_page', 0)
    await callback.answer('\U0001f4ca Загружаю статус\u2026')
    text, markup = _build_providers_menu(page=page)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    except Exception:
        pass


@admin_required
@error_handler
async def sync_squads_now(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    page = data.get('happ_prov_page', 0)
    await callback.answer('\U0001f504 Синхронизация сквадов\u2026')
    try:
        ok, total = await sync_to_remnawave()
        if total == 0:
            result = 'Remnawave-панели не найдены.'
        elif ok == total:
            result = f'\u2705 Сквады синхронизированы: {ok}/{total} панелей.'
        else:
            result = f'\u26a0\ufe0f {ok}/{total} панелей. Проверьте логи.'
    except Exception as e:
        result = f'\u274c Ошибка: {e}'
    text, markup = _build_providers_menu(page=page)
    try:
        await callback.message.edit_text(
            f'<b>\U0001f504 Синхронизация</b>\n\n{result}\n\n' + text.split('\n\n', 1)[-1],
            reply_markup=markup,
            parse_mode='HTML',
        )
    except Exception:
        pass


@admin_required
@error_handler
async def show_delete_providers(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(None)
    providers = cfg.get_providers()
    if not providers:
        await callback.answer('Нет провайдеров для удаления', show_alert=True)
        return
    text, markup = _build_delete_providers_page(0)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def delete_providers_page(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    page = int(callback.data[len('happ_del_pg_') :])
    text, markup = _build_delete_providers_page(page)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    except Exception:
        pass
    await callback.answer()


@admin_required
@error_handler
async def confirm_delete_provider(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    provider_id = callback.data.split('|', 1)[1]
    providers = cfg.get_providers()
    provider = next((p for p in providers if p.get('provider_id') == provider_id), None)
    has_squad = bool(provider and provider.get('squad_uuid'))
    is_custom = cfg.is_custom_squad(provider_id)
    total = provider.get('total_assigned', 0) if provider else 0
    text = f'<b>\u26a0\ufe0f Удалить Provider ID <code>{html_escape(provider_id)}</code>?</b>\n\n'
    text += '<b>Будет удалено:</b>\n\u2022 Provider ID из модуля\n\u2022 Назначения пользователей\n'
    if has_squad and not is_custom:
        text += '\u2022 External Squad на панели Remnawave\n'
    if is_custom:
        cs = cfg.get_custom_squad(provider_id)
        cs_name = cs.get('name', '?') if cs else '?'
        text += f'\n\u2139\ufe0f Привязанный сквад <b>{html_escape(cs_name)}</b> НЕ будет удалён.\n'
    if total:
        text += f'\n\u26a0\ufe0f Счётчик устройств: <b>{total}/100</b>\n'
    text += '\n<b>Это действие нельзя отменить.</b>'
    builder = InlineKeyboardBuilder()
    builder.button(text=f'\U0001f5d1 Да, удалить {provider_id}', callback_data=f'happ_prov_del_ok|{provider_id}')
    builder.button(text='\u274c Отмена', callback_data='happ_prov_del_list')
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def delete_provider_confirmed(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    page = data.get('happ_prov_page', 0)
    provider_id = callback.data.split('|', 1)[1]
    providers = cfg.get_providers()
    provider = next((p for p in providers if p.get('provider_id') == provider_id), None)
    squad_uuid = provider.get('squad_uuid') if provider else None
    is_custom = cfg.is_custom_squad(provider_id)
    removed = cfg.remove_provider(provider_id)
    if not removed:
        await callback.answer('Не найден', show_alert=True)
        return
    schedule_sync()
    result_parts = [f'Provider ID {provider_id} удалён']
    if squad_uuid and not is_custom:
        try:
            from app.services.happ_management.squad_manager import delete_squad

            ok, msg = await delete_squad(squad_uuid)
            result_parts.append('сквад удалён' if ok else f'сквад: {msg}')
        except Exception as e:
            result_parts.append(f'ошибка сквада: {e}')
    elif is_custom:
        result_parts.append('привязанный сквад сохранён')
    await callback.answer(', '.join(result_parts), show_alert=True)
    providers = cfg.get_providers()
    total_pages = max(1, (len(providers) + PROVIDERS_PER_PAGE - 1) // PROVIDERS_PER_PAGE)
    page = min(page, total_pages - 1)
    text, markup = _build_providers_menu(page=page)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def show_counter_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(None)
    providers = cfg.get_providers()
    if not providers:
        await callback.answer('Нет провайдеров', show_alert=True)
        return
    text, markup = _build_counter_page(0)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def counter_page(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    page = int(callback.data[len('happ_cnt_pg_') :])
    text, markup = _build_counter_page(page)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    except Exception:
        pass
    await callback.answer()


@admin_required
@error_handler
async def start_edit_counter(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    pid = callback.data.split('|', 1)[1]
    current = cfg.get_provider_total_assigned(pid)
    await state.set_state(HappManagementStates.waiting_counter_value)
    await state.update_data(happ_counter_pid=pid)
    text = (
        f'<b>\u270f\ufe0f Счётчик для <code>{html_escape(pid)}</code></b>\n\n'
        f'Текущее значение: <b>{current}/100</b>\n\n'
        'Введите актуальное количество устройств (число от 0 до 100).\n'
        'Значение можно посмотреть на happ-proxy.com в разделе Provider ID.'
    )
    builder = InlineKeyboardBuilder()
    builder.button(text='\u2b05\ufe0f Отмена', callback_data='happ_prov_counter_list')
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def process_counter_value(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    if not message.text or not message.text.strip().isdigit():
        await message.answer('Введите число от 0 до 100.')
        return
    value = int(message.text.strip())
    if value < 0 or value > 100:
        await message.answer('Допустимый диапазон: 0\u2013100.')
        return
    data = await state.get_data()
    pid = data.get('happ_counter_pid')
    if not pid:
        await state.clear()
        await message.answer('\u26a0\ufe0f Провайдер не выбран.')
        return
    old_value = cfg.get_provider_total_assigned(pid)
    cfg.set_provider_total_assigned(pid, value)
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.button(text='\U0001f465 К списку провайдеров', callback_data='happ_providers')
    builder.adjust(1)
    await message.answer(
        f'\u2705 Счётчик <code>{html_escape(pid)}</code>: {old_value} \u2192 <b>{value}</b>/100',
        reply_markup=builder.as_markup(),
        parse_mode='HTML',
    )


@admin_required
@error_handler
async def show_provider_config_full(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    pid = callback.data.split('|', 1)[1]
    providers = cfg.get_providers()
    if not any(p.get('provider_id') == pid for p in providers):
        await callback.answer('Провайдер не найден', show_alert=True)
        return
    await state.set_state(None)
    await state.update_data(happ_prov_ctx=pid, happ_prov_page=0)
    text, markup = await _build_provider_settings_menu(pid)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def back_to_provider_config(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    data = await state.get_data()
    pid = data.get('happ_prov_ctx')
    if not pid:
        text, markup = _build_providers_menu(page=0)
        await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
        await callback.answer()
        return
    text, markup = await _build_provider_settings_menu(pid)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def show_bind_squad(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    parts = callback.data.split('|')
    provider_id = parts[1] if len(parts) > 1 else ''
    try:
        page = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        page = 0
    await state.update_data(happ_prov_ctx=provider_id)
    text, markup = await _build_bind_squad_screen(provider_id, page)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def select_bind_squad(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    squad_uuid = callback.data.split('|', 1)[1]
    data = await state.get_data()
    provider_id = data.get('happ_prov_ctx')
    if not provider_id:
        await callback.answer('Контекст потерян', show_alert=True)
        return
    try:
        from app.services.happ_management.squad_manager import get_all_external_squads

        all_squads = await get_all_external_squads(exclude_bound=False)
        squad = next((s for s in all_squads if s.get('uuid') == squad_uuid), None)
    except Exception:
        squad = None
    if not squad:
        await callback.answer('Сквад не найден', show_alert=True)
        return
    squad_name = squad.get('name', '?')
    ok = cfg.bind_custom_squad(provider_id, squad_uuid, squad_name)
    if ok:
        schedule_sync()
        await callback.answer(f'\u2705 Привязан к \u00ab{squad_name}\u00bb', show_alert=True)
    else:
        await callback.answer('\u26a0\ufe0f Этот сквад уже привязан к другому провайдеру!', show_alert=True)
        return
    text, markup = await _build_provider_settings_menu(provider_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def unbind_squad(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    provider_id = callback.data.split('|', 1)[1]
    custom = cfg.get_custom_squad(provider_id)
    if not custom:
        await callback.answer('Нет привязки', show_alert=True)
        return
    squad_name = custom.get('name', '?')
    ok = cfg.unbind_custom_squad(provider_id)
    if ok:
        schedule_sync()
        await callback.answer(f'Отвязан от \u00ab{squad_name}\u00bb. Будет создан Happ-* сквад.', show_alert=True)
    else:
        await callback.answer('Ошибка', show_alert=True)
        return
    text, markup = await _build_provider_settings_menu(provider_id)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def toggle_provider_managed_full(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    pid = callback.data.split('|', 1)[1]
    current = cfg.is_provider_managed(pid)
    cfg.set_provider_managed(pid, not current)
    schedule_sync()
    await state.update_data(happ_prov_ctx=pid)
    status = '\U0001f527 Управляемый' if not current else '\U0001f512 Не управляется'
    await callback.answer(status)
    text, markup = await _build_provider_settings_menu(pid)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def show_provider_section(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    section_key = callback.data.split('|', 1)[1]
    data = await state.get_data()
    pid = data.get('happ_prov_ctx')
    if not pid:
        await callback.answer('Контекст провайдера потерян', show_alert=True)
        return
    text, markup = _build_provider_section_menu(pid, section_key)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def toggle_provider_bool(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    key = callback.data.split('|', 1)[1]
    data = await state.get_data()
    pid = data.get('happ_prov_ctx')
    if not pid:
        await callback.answer('Контекст провайдера потерян', show_alert=True)
        return
    schema = cfg.SETTINGS_SCHEMA.get(key)
    if not schema or schema.get('type') != 'bool':
        await callback.answer('Неизвестный параметр', show_alert=True)
        return
    current = cfg.get_effective(key, pid)
    new_value = not current
    cfg.set_provider_override(pid, key, new_value)
    schedule_sync()
    status = '\u2705 Включено' if new_value else '\u274c Выключено'
    await callback.answer(f'{schema["label"]}: {status} (переопр.)')
    section_key = _provider_section_for_key(key)
    text, markup = _build_provider_section_menu(pid, section_key)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def show_provider_choice(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    key = callback.data.split('|', 1)[1]
    data = await state.get_data()
    pid = data.get('happ_prov_ctx')
    if not pid:
        await callback.answer('Контекст провайдера потерян', show_alert=True)
        return
    schema = cfg.SETTINGS_SCHEMA.get(key)
    if not schema or schema.get('type') != 'choice':
        await callback.answer('Неизвестный параметр', show_alert=True)
        return
    current = cfg.get_effective(key, pid)
    is_overridden = key in cfg.get_provider_overrides(pid)
    current_label = cfg.get_choice_label(key, current)
    text = f'<b>{schema["label"]}</b>\nПровайдер: <code>{html_escape(pid)}</code>\n\n'
    hint = schema.get('hint', '')
    if hint:
        text += f'<i>{hint}</i>\n\n'
    ovr = ' \u2b50 переопределено' if is_overridden else ' (глобальное)'
    text += f'Текущее значение: <b>{current_label}</b>{ovr}\n\nВыберите:'
    builder = InlineKeyboardBuilder()
    for option in schema.get('choices', []):
        icon = '\U0001f518' if option == current else '\u26aa'
        display = cfg.get_choice_label(key, option)
        builder.button(text=f'{icon} {display}', callback_data=f'happ_pscv|{key}|{option}')
    if is_overridden:
        builder.button(text='\U0001f504 Использовать глобальное', callback_data=f'happ_prmv|{key}')
    section_key = _provider_section_for_key(key)
    builder.button(text='\u2b05\ufe0f Назад', callback_data=f'happ_psec|{section_key}')
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def set_provider_choice(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    parts = callback.data.split('|', 2)
    if len(parts) < 3:
        await callback.answer('Ошибка', show_alert=True)
        return
    key, value = parts[1], parts[2]
    data = await state.get_data()
    pid = data.get('happ_prov_ctx')
    if not pid:
        await callback.answer('Контекст провайдера потерян', show_alert=True)
        return
    schema = cfg.SETTINGS_SCHEMA.get(key)
    if not schema:
        await callback.answer('Неизвестный параметр', show_alert=True)
        return
    cfg.set_provider_override(pid, key, value)
    schedule_sync()
    display = cfg.get_choice_label(key, value)
    await callback.answer(f'{schema["label"]}: {display} (переопр.)')
    section_key = _provider_section_for_key(key)
    text, markup = _build_provider_section_menu(pid, section_key)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def start_edit_provider_str(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    key = callback.data.split('|', 1)[1]
    data = await state.get_data()
    pid = data.get('happ_prov_ctx')
    if not pid:
        await callback.answer('Контекст провайдера потерян', show_alert=True)
        return
    schema = cfg.SETTINGS_SCHEMA.get(key)
    if not schema:
        await callback.answer('Неизвестный параметр', show_alert=True)
        return
    current = cfg.get_effective(key, pid)
    is_overridden = key in cfg.get_provider_overrides(pid)
    text = f'<b>{schema["label"]}</b>\nПровайдер: <code>{html_escape(pid)}</code>\n\n'
    hint = schema.get('hint', '')
    if hint:
        text += f'<i>{hint}</i>\n\n'
    ovr = ' \u2b50 переопределено' if is_overridden else ' (глобальное)'
    text += f'Текущее значение: <code>{html_escape(str(current)) if current else "\u2014"}</code>{ovr}\n'
    if is_overridden:
        global_val = cfg.get(key)
        text += f'Глобальное: <code>{html_escape(str(global_val)) if global_val else "\u2014"}</code>\n'
    text += '\nВведите новое значение или отправьте <code>-</code> для очистки:'
    await state.set_state(HappManagementStates.waiting_prov_str_value)
    await state.update_data(happ_prov_edit_key=key, happ_prov_ctx=pid)
    builder = InlineKeyboardBuilder()
    if is_overridden:
        builder.button(text='\U0001f504 Использовать глобальное', callback_data=f'happ_prmv|{key}')
    section_key = _provider_section_for_key(key)
    builder.button(text='\u2b05\ufe0f Отмена', callback_data=f'happ_psec|{section_key}')
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def process_provider_str_value(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    if not message.text:
        await message.answer('Отправьте текстовое сообщение.')
        return
    data = await state.get_data()
    key = data.get('happ_prov_edit_key')
    pid = data.get('happ_prov_ctx')
    if not key or not pid or key not in cfg.SETTINGS_SCHEMA:
        await state.clear()
        return
    schema = cfg.SETTINGS_SCHEMA[key]
    value = message.text.strip()
    if value == '-':
        value = ''
    if value and key == 'COLOR_PROFILE' and value != 'resetcolors':
        try:
            parsed = json.loads(value)
            value = json.dumps(parsed, ensure_ascii=False, separators=(',', ':'))
        except Exception:
            section_key = _provider_section_for_key(key)
            builder = InlineKeyboardBuilder()
            builder.button(text='\u2b05\ufe0f Назад', callback_data=f'happ_psec|{section_key}')
            builder.adjust(1)
            await message.answer('\u274c Невалидный JSON.', reply_markup=builder.as_markup(), parse_mode='HTML')
            return
    if value:
        error = cfg.validate_value(key, value)
        if error:
            section_key = _provider_section_for_key(key)
            builder = InlineKeyboardBuilder()
            builder.button(text='\U0001f504 Попробовать снова', callback_data=f'happ_pedt|{key}')
            builder.button(text='\u2b05\ufe0f Назад', callback_data=f'happ_psec|{section_key}')
            builder.adjust(1)
            await message.answer(f'\u274c {error}', reply_markup=builder.as_markup(), parse_mode='HTML')
            return
    if value:
        cfg.set_provider_override(pid, key, value)
    else:
        cfg.remove_provider_override(pid, key)
    schedule_sync()
    await state.set_state(None)
    display = f'<code>{html_escape(value[:80])}</code>' if value else 'очищено (глобальное)'
    text = f'\u2b50 <b>{schema["label"]}</b> \u2192 {display}\n(провайдер: <code>{html_escape(pid)}</code>)'
    section_key = _provider_section_for_key(key)
    builder = InlineKeyboardBuilder()
    builder.button(text='\U0001f504 Использовать глобальное', callback_data=f'happ_prmv|{key}')
    builder.button(text='\u21a9\ufe0f Назад к секции', callback_data=f'happ_psec|{section_key}')
    builder.button(text='\u2b05\ufe0f К провайдеру', callback_data='happ_pcfg_back')
    builder.adjust(1)
    await message.answer(text, reply_markup=builder.as_markup(), parse_mode='HTML')


@admin_required
@error_handler
async def remove_provider_override(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    key = callback.data.split('|', 1)[1]
    data = await state.get_data()
    pid = data.get('happ_prov_ctx')
    if not pid:
        await callback.answer('Контекст провайдера потерян', show_alert=True)
        return
    schema = cfg.SETTINGS_SCHEMA.get(key)
    cfg.remove_provider_override(pid, key)
    schedule_sync()
    label = schema['label'] if schema else key
    await callback.answer(f'{label}: глобальное значение')
    await state.set_state(None)
    section_key = _provider_section_for_key(key)
    text, markup = _build_provider_section_menu(pid, section_key)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def confirm_reset_overrides(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    pid = callback.data.split('|', 1)[1]
    overrides = cfg.get_provider_overrides(pid)
    if not overrides:
        await callback.answer('Нет переопределений', show_alert=True)
        return
    await state.update_data(happ_prov_ctx=pid)
    builder = InlineKeyboardBuilder()
    builder.button(text=f'\u2705 Да, сбросить ({len(overrides)} шт.)', callback_data=f'happ_prst_ok|{pid}')
    builder.button(text='\u274c Отмена', callback_data='happ_pcfg_back')
    builder.adjust(1)
    text = (
        f'<b>\u26a0\ufe0f Сбросить все переопределения?</b>\n\n'
        f'Провайдер: <code>{html_escape(pid)}</code>\n'
        f'Будет сброшено: <b>{len(overrides)}</b> настроек.\n\n'
        'Все переопределения будут удалены,\nпровайдер вернётся к глобальным настройкам.'
    )
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def reset_all_provider_overrides(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    pid = callback.data.split('|', 1)[1]
    overrides = cfg.get_provider_overrides(pid)
    if not overrides:
        await callback.answer('Нет переопределений', show_alert=True)
        return
    count = len(overrides)
    for k in list(overrides.keys()):
        cfg.remove_provider_override(pid, k)
    schedule_sync()
    await state.update_data(happ_prov_ctx=pid)
    await callback.answer(f'Сброшено {count} переопределений')
    text, markup = await _build_provider_settings_menu(pid)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def show_source_squads(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(None)
    try:
        page = int(callback.data[len('happ_source_squads_') :])
    except (ValueError, IndexError):
        page = 0
    await callback.answer('\u23f3 Загрузка сквадов\u2026')
    text, markup = await _build_source_squads_screen(page)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def toggle_source_squad(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    parts = callback.data.split('|')
    if len(parts) < 2:
        await callback.answer('Ошибка данных', show_alert=True)
        return
    squad_uuid = parts[1]
    try:
        page = int(parts[2]) if len(parts) > 2 else 0
    except ValueError:
        page = 0
    if cfg.is_source_squad(squad_uuid):
        cfg.remove_source_squad(squad_uuid)
        await callback.answer('Сквад убран из источников')
    else:
        try:
            from app.services.happ_management.squad_manager import get_all_external_squads

            all_squads = await get_all_external_squads()
            name = next((s['name'] for s in all_squads if s['uuid'] == squad_uuid), '?')
        except Exception:
            name = '?'
        cfg.add_source_squad(squad_uuid, name)
        await callback.answer('Сквад добавлен в источники')
    text, markup = await _build_source_squads_screen(page)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')


@admin_required
@error_handler
async def clear_source_squads_handler(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    count = cfg.clear_source_squads()
    await callback.answer(f'Удалено источников: {count}', show_alert=True)
    try:
        page = int(callback.data.split('|', 1)[1])
    except (ValueError, IndexError):
        page = 0
    text, markup = await _build_source_squads_screen(page)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')


# ---------------------------------------------------------------------------
# Autoreg helpers and handlers
# ---------------------------------------------------------------------------


async def _do_show_autoreg(callback: types.CallbackQuery, state: FSMContext):
    """Display autoreg menu - shared by multiple handlers."""
    try:
        from app.services.happ_management.autoreg import get_available_method

        has_nodriver = get_available_method() == 'nodriver'
    except ImportError:
        has_nodriver = False
    domain = cfg.get('SUBSCRIPTION_DOMAIN')
    captcha_key = cfg.get('CAPTCHA_API_KEY') or ''
    chosen = cfg.get('AUTOREG_METHOD') or 'auto'
    has_http = bool(captcha_key)
    text = '<b>\U0001f916 Авторегистрация</b>\n\n'
    method_labels = {
        'http': ('\u26a1 HTTP + rucaptcha', 'быстрый, без браузера'),
        'nodriver': ('\U0001f310 Браузер (nodriver)', 'бесплатно, медленнее'),
        'auto': ('\U0001f504 Авто', 'лучший доступный'),
    }
    ml, md = method_labels.get(chosen, method_labels['auto'])
    text += f'<b>Метод:</b> {ml}\n<i>{md}</i>\n'
    if chosen in ('http', 'auto') and has_http:
        text += '\u2705 API-ключ задан\n'
    elif chosen == 'http' and not has_http:
        text += '\u26a0\ufe0f Нужен API-ключ rucaptcha.com!\n'
    if chosen in ('nodriver', 'auto') and has_nodriver:
        text += '\u2705 nodriver доступен\n'
    text += '\n'
    if domain:
        text += f'\U0001f4cc Домен: <code>{html_escape(domain)}</code>\n'
    else:
        text += '\u26a0\ufe0f Домен не задан\n'
    text += '\nВыберите количество аккаунтов:'
    await state.clear()
    builder = InlineKeyboardBuilder()
    for n in [1, 3, 5, 10]:
        builder.button(text=f'{n} акк.', callback_data=f'happ_autoreg_run_{n}')
    builder.button(text='Другое', callback_data='happ_autoreg_custom')
    if chosen == 'http':
        builder.button(text='\U0001f310 \u2192 Браузер', callback_data='happ_meth_nodriver')
        builder.button(text='\U0001f504 \u2192 Авто', callback_data='happ_meth_auto')
    elif chosen == 'nodriver':
        builder.button(text='\u26a1 \u2192 HTTP', callback_data='happ_meth_http')
        builder.button(text='\U0001f504 \u2192 Авто', callback_data='happ_meth_auto')
    else:
        builder.button(text='\u26a1 \u2192 HTTP', callback_data='happ_meth_http')
        builder.button(text='\U0001f310 \u2192 Браузер', callback_data='happ_meth_nodriver')
    if not has_http:
        builder.button(text='\U0001f511 Задать API-ключ', callback_data='happ_autoreg_apikey')
    else:
        builder.button(text='\U0001f511 Изменить API-ключ', callback_data='happ_autoreg_apikey')
    if domain:
        builder.button(text='\u270f\ufe0f Домен', callback_data='happ_autoreg_domain')
        builder.button(text='\U0001f5d1 Сбросить домен', callback_data='happ_autoreg_domain_clr')
    else:
        builder.button(text='\U0001f4cc Задать домен', callback_data='happ_autoreg_domain')
    builder.button(text='\u2b05\ufe0f Назад', callback_data='happ_prov_autoreg_menu')
    rows = [4, 1, 2, 1]
    if domain:
        rows += [2, 1]
    else:
        rows += [1, 1]
    builder.adjust(*rows)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def start_autoreg(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await _do_show_autoreg(callback, state)


@admin_required
@error_handler
async def autoreg_custom_count(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(HappManagementStates.waiting_autoreg_count)
    builder = InlineKeyboardBuilder()
    builder.button(text='\u2b05\ufe0f Отмена', callback_data='happ_autoreg')
    builder.adjust(1)
    await callback.message.edit_text(
        'Введите количество аккаунтов (1\u201310):', reply_markup=builder.as_markup(), parse_mode='HTML'
    )
    await callback.answer()


@admin_required
@error_handler
async def autoreg_change_domain(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(HappManagementStates.waiting_autoreg_domain)
    cur = cfg.get('SUBSCRIPTION_DOMAIN') or ''
    text = '\U0001f4cc Введите новый домен подписки\n(например <code>mydomain.com</code>):'
    if cur:
        text += f'\n\nТекущий: <code>{html_escape(cur)}</code>'
    builder = InlineKeyboardBuilder()
    builder.button(text='\u2b05\ufe0f Отмена', callback_data='happ_autoreg')
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def autoreg_clear_domain(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    cfg.set_value('SUBSCRIPTION_DOMAIN', '')
    await _do_show_autoreg(callback, state)


@admin_required
@error_handler
async def autoreg_switch_method(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    method = callback.data[len('happ_meth_') :]
    if method not in ('http', 'nodriver', 'auto'):
        method = 'auto'
    cfg.set_value('AUTOREG_METHOD', method)
    await _do_show_autoreg(callback, state)


@admin_required
@error_handler
async def autoreg_set_apikey(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(HappManagementStates.waiting_captcha_key)
    cur = cfg.get('CAPTCHA_API_KEY') or ''
    text = (
        '\U0001f511 <b>API-ключ rucaptcha.com</b>\n\n'
        'Введите ваш API-ключ от rucaptcha.com.\n\n'
        'Стоимость: ~$2\u20133 за 1000 капч.\n'
        'Скорость: ~15-25 сек на аккаунт, до 10+ параллельно.'
    )
    if cur:
        text += f'\n\nТекущий: <tg-spoiler>{html_escape(cur)}</tg-spoiler>'
    builder = InlineKeyboardBuilder()
    if cur:
        builder.button(text='\U0001f5d1 Удалить ключ', callback_data='happ_autoreg_rmkey')
    builder.button(text='\u2b05\ufe0f Отмена', callback_data='happ_autoreg')
    builder.adjust(1)
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def autoreg_remove_apikey(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    cfg.set_value('CAPTCHA_API_KEY', '')
    if (cfg.get('AUTOREG_METHOD') or 'auto') == 'http':
        cfg.set_value('AUTOREG_METHOD', 'auto')
    await _do_show_autoreg(callback, state)


@admin_required
@error_handler
async def process_captcha_key(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    key = message.text.strip() if message.text else ''
    if not key or len(key) < 10:
        await message.answer('\u274c Некорректный ключ. Попробуйте ещё раз.')
        return
    cfg.set_value('CAPTCHA_API_KEY', key)
    await state.clear()
    await message.answer(f'\u2705 API-ключ сохранён\n\n<tg-spoiler>{html_escape(key)}</tg-spoiler>', parse_mode='HTML')
    builder = InlineKeyboardBuilder()
    builder.button(text='\u2b05\ufe0f К авторегистрации', callback_data='happ_autoreg')
    builder.adjust(1)
    await message.answer('Теперь можете использовать HTTP-метод.', reply_markup=builder.as_markup())


@admin_required
@error_handler
async def autoreg_quick_run(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    try:
        count = int(callback.data[len('happ_autoreg_run_') :])
    except (ValueError, IndexError):
        await callback.answer('Недопустимое количество')
        return
    if not (1 <= count <= 10):
        await callback.answer('Недопустимое количество')
        return
    domain = cfg.get('SUBSCRIPTION_DOMAIN')
    if not domain:
        await state.update_data(autoreg_count=count)
        await state.set_state(HappManagementStates.waiting_autoreg_domain)
        await callback.message.edit_text(
            '\U0001f4cc Введите домен подписки\n(например <code>mydomain.com</code>):', parse_mode='HTML'
        )
        await callback.answer()
        return
    await state.clear()
    await callback.answer()
    await _run_autoreg(callback.message, count, domain)


@admin_required
@error_handler
async def process_autoreg_count(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    text = message.text.strip() if message.text else ''
    if not text.isdigit() or not (1 <= int(text) <= 10):
        await message.answer('Введите число от 1 до 10.')
        return
    count = int(text)
    domain = cfg.get('SUBSCRIPTION_DOMAIN')
    if not domain:
        await state.update_data(autoreg_count=count)
        await state.set_state(HappManagementStates.waiting_autoreg_domain)
        await message.answer(
            '\U0001f4cc Введите домен подписки\n(например <code>mydomain.com</code>):', parse_mode='HTML'
        )
        return
    await state.clear()
    await _run_autoreg(message, count, domain)


@admin_required
@error_handler
async def process_autoreg_domain(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    domain = (message.text or '').strip().lower()
    if not domain or '.' not in domain:
        await message.answer('Введите корректный домен (например <code>mydomain.com</code>).', parse_mode='HTML')
        return
    if domain.startswith('http'):
        from urllib.parse import urlparse

        domain = urlparse(domain).hostname or domain
    cfg.set_value('SUBSCRIPTION_DOMAIN', domain)
    data = await state.get_data()
    count = data.get('autoreg_count')
    await state.clear()
    if count:
        await _run_autoreg(message, count, domain)
    else:
        await message.answer(f'\u2705 Домен сохранён: <code>{html_escape(domain)}</code>', parse_mode='HTML')


async def _run_autoreg(message: types.Message, count: int, domain: str):
    try:
        from app.services.happ_management.autoreg import auto_register
    except ImportError:
        await message.answer('\u274c Модуль авторегистрации не установлен.')
        return
    progress_msg = await message.answer(
        f'\U0001f916 <b>Авторегистрация</b>  \u00b7  {count} акк.\n\n'
        + '\n'.join(f'  {_CIRCLED[i] if i < len(_CIRCLED) else f"#{i}"} {"\u25b1" * 14}' for i in range(1, count + 1)),
        parse_mode='HTML',
    )
    _progress_lock = asyncio.Lock()
    _worker_steps: dict[int, tuple[int, int]] = {}
    _last_text = ''

    async def _on_progress(worker: int, total: int, step: int, total_steps: int):
        nonlocal _last_text
        async with _progress_lock:
            _worker_steps[worker] = (step, total_steps)
            lines = []
            for w in range(1, total + 1):
                s, ts = _worker_steps.get(w, (0, total_steps))
                icon = _CIRCLED[w] if w < len(_CIRCLED) else f'#{w}'
                if s == -1:
                    lines.append(f'  {icon} \u274c <s>{"\u25b1" * 14}</s>')
                elif s > ts:
                    lines.append(f'  {icon} {"\u25b0" * 14} \u2705')
                else:
                    lines.append(f'  {icon} {_bar(s, ts)}')
            new_text = f'\U0001f916 <b>Авторегистрация</b>  \u00b7  {total} акк.\n\n' + '\n'.join(lines)
            if new_text != _last_text:
                _last_text = new_text
                try:
                    await progress_msg.edit_text(new_text, parse_mode='HTML')
                except Exception:
                    pass

    captcha_key = cfg.get('CAPTCHA_API_KEY') or ''
    preferred_method = cfg.get('AUTOREG_METHOD') or 'auto'
    try:
        results = await auto_register(
            count, domain, _on_progress, captcha_api_key=captcha_key, preferred_method=preferred_method
        )
    except Exception as e:
        logger.error(f'[HappAutoreg] Ошибка: {e}', exc_info=True)
        await progress_msg.edit_text(f'\u274c <b>Ошибка:</b> {html_escape(str(e))}', parse_mode='HTML')
        return
    added_providers = 0
    for acc in results:
        cfg.add_account(acc)
        if acc.get('domain'):
            cfg.add_provider(acc['provider_id'])
            added_providers += 1
    if added_providers:
        schedule_sync()
    ok_lines = []
    no_domain_lines = []
    for acc in results:
        pid = f'<code>{acc["provider_id"]}</code>'
        if acc.get('domain'):
            ok_lines.append(f'  \u2705 {pid}')
        else:
            no_domain_lines.append(f'  \u26a0\ufe0f {pid} \u2014 без домена')
    failed = count - len(results)
    header = '\U0001f916 <b>Авторегистрация завершена</b>\n\n'
    result_bar = _bar(len(results), count, 18)
    if len(results) == count:
        header += f'  {result_bar} \u2705 {len(results)}/{count}\n\n'
    elif len(results) > 0:
        header += f'  {result_bar}  {len(results)}/{count}\n\n'
    else:
        header += f'  {"\u25b1" * 18} \u274c 0/{count}\n\n'
    body = ''
    if ok_lines:
        body += '\n'.join(ok_lines)
    if no_domain_lines:
        if body:
            body += '\n'
        body += '\n'.join(no_domain_lines)
    if failed:
        body += f'\n  \u274c {failed} не удалось' if body else '  \u274c Не удалось создать ни одного'
    if not body:
        body = '  \u274c Ни одного аккаунта не создано'
    body += f'\n\n\U0001f4cc Домен: <code>{html_escape(domain)}</code>'
    builder = InlineKeyboardBuilder()
    builder.button(text='\U0001f465 Провайдеры', callback_data='happ_providers')
    builder.adjust(1)
    try:
        await progress_msg.edit_text(header + body, parse_mode='HTML', reply_markup=builder.as_markup())
    except Exception:
        await message.answer(header + body, parse_mode='HTML', reply_markup=builder.as_markup())


@admin_required
@error_handler
async def show_accounts_list(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    text, markup = _build_accounts_page(0)
    await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def accounts_page(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    page = int(callback.data[len('happ_acc_pg_') :])
    text, markup = _build_accounts_page(page)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    except Exception:
        pass
    await callback.answer()


@admin_required
@error_handler
async def delete_account(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    parts = callback.data.split('|')
    pid = parts[1] if len(parts) > 1 else ''
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    if not pid:
        await callback.answer('Ошибка: ID не указан', show_alert=True)
        return
    removed = cfg.remove_account(pid)
    if removed:
        providers = cfg.get_providers()
        squad_uuid = None
        is_custom = False
        for p in providers:
            if p.get('provider_id') == pid:
                squad_uuid = p.get('squad_uuid')
                cs = p.get('custom_squad')
                is_custom = isinstance(cs, dict) and bool(cs.get('uuid'))
                break
        cfg.remove_provider(pid)
        if squad_uuid and not is_custom:
            try:
                from app.services.happ_management.squad_manager import delete_squad

                await delete_squad(squad_uuid)
            except Exception as e:
                logger.error(f'[HappManagement] Ошибка удаления сквада {squad_uuid}: {e}')
        schedule_sync()
        await callback.answer(f'Аккаунт {pid} удалён', show_alert=True)
    else:
        await callback.answer(f'Аккаунт {pid} не найден', show_alert=True)
    text, markup = _build_accounts_page(page)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode='HTML')
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_handlers(dp: Dispatcher):
    # noop
    dp.callback_query.register(noop_handler, F.data == 'noop')

    # Main menu
    dp.callback_query.register(show_happ_main, F.data == 'happ_main')

    # Settings navigation
    dp.callback_query.register(show_happ_settings, F.data == 'happ_settings')
    dp.callback_query.register(show_happ_section, F.data.startswith('happ_section_'))
    dp.callback_query.register(show_happ_entity, F.data.startswith('happ_entity_'))

    # Remnawave
    dp.callback_query.register(show_happ_remnawave, F.data == 'happ_remnawave')
    dp.callback_query.register(show_source_squads, F.data.startswith('happ_source_squads_'))
    dp.callback_query.register(toggle_source_squad, F.data.startswith('happ_st|'))
    dp.callback_query.register(clear_source_squads_handler, F.data.startswith('happ_sc|'))

    # Backup
    dp.callback_query.register(show_happ_backup, F.data == 'happ_backup')

    # Module toggles
    dp.callback_query.register(toggle_module, F.data == 'happ_module_toggle')
    dp.callback_query.register(quick_toggle, F.data.startswith('happ_qtoggle_'))
    dp.callback_query.register(toggle_bool, F.data.startswith('happ_toggle_'))

    # Choices
    dp.callback_query.register(show_choice, F.data.startswith('happ_choice_'))
    dp.callback_query.register(set_choice, F.data.startswith('happ_set_choice_'))

    # String editing
    dp.callback_query.register(start_edit_str, F.data.startswith('happ_edit_'))
    dp.message.register(process_str_value, StateFilter(HappManagementStates.waiting_str_value))
    dp.callback_query.register(clear_str_value, F.data.startswith('happ_clear_'))
    dp.callback_query.register(reset_color_profile, F.data == 'happ_color_reset')

    # Remnawave actions
    dp.callback_query.register(force_sync, F.data == 'happ_force_sync')
    dp.callback_query.register(cleanup_remna, F.data == 'happ_cleanup_remna')

    # Provider list
    dp.callback_query.register(show_happ_providers, F.data == 'happ_providers')
    dp.callback_query.register(providers_page, F.data.startswith('happ_prov_pg_'))
    dp.callback_query.register(show_provider_actions, F.data == 'happ_prov_actions')
    dp.callback_query.register(show_autoreg_menu, F.data == 'happ_prov_autoreg_menu')
    dp.callback_query.register(refresh_providers_status, F.data == 'happ_prov_status')
    dp.callback_query.register(sync_squads_now, F.data == 'happ_prov_sync')
    dp.callback_query.register(assign_users_now, F.data == 'happ_prov_assign')

    # Add provider
    dp.callback_query.register(start_add_provider, F.data == 'happ_prov_add')
    dp.message.register(process_add_provider, StateFilter(HappManagementStates.waiting_provider_id))

    # Delete provider with confirmation
    dp.callback_query.register(show_delete_providers, F.data == 'happ_prov_del_list')
    dp.callback_query.register(delete_providers_page, F.data.startswith('happ_del_pg_'))
    dp.callback_query.register(confirm_delete_provider, F.data.startswith('happ_prov_del|'))
    dp.callback_query.register(delete_provider_confirmed, F.data.startswith('happ_prov_del_ok|'))

    # Counter management
    dp.callback_query.register(show_counter_list, F.data == 'happ_prov_counter_list')
    dp.callback_query.register(counter_page, F.data.startswith('happ_cnt_pg_'))
    dp.callback_query.register(start_edit_counter, F.data.startswith('happ_prov_counter|'))
    dp.message.register(process_counter_value, StateFilter(HappManagementStates.waiting_counter_value))

    # Per-provider settings
    dp.callback_query.register(show_provider_config_full, F.data.startswith('happ_pcfg|'))
    dp.callback_query.register(back_to_provider_config, F.data == 'happ_pcfg_back')
    dp.callback_query.register(show_bind_squad, F.data.startswith('happ_bind|'))
    dp.callback_query.register(select_bind_squad, F.data.startswith('happ_bsel|'))
    dp.callback_query.register(unbind_squad, F.data.startswith('happ_unbind|'))
    dp.callback_query.register(toggle_provider_managed_full, F.data.startswith('happ_ptm|'))
    dp.callback_query.register(show_provider_section, F.data.startswith('happ_psec|'))
    dp.callback_query.register(toggle_provider_bool, F.data.startswith('happ_ptgl|'))
    dp.callback_query.register(show_provider_choice, F.data.startswith('happ_pcho|'))
    dp.callback_query.register(set_provider_choice, F.data.startswith('happ_pscv|'))
    dp.callback_query.register(start_edit_provider_str, F.data.startswith('happ_pedt|'))
    dp.message.register(process_provider_str_value, StateFilter(HappManagementStates.waiting_prov_str_value))
    dp.callback_query.register(remove_provider_override, F.data.startswith('happ_prmv|'))
    dp.callback_query.register(confirm_reset_overrides, F.data.startswith('happ_prst|'))
    dp.callback_query.register(reset_all_provider_overrides, F.data.startswith('happ_prst_ok|'))

    # Legacy provider config (backward compat)
    dp.callback_query.register(show_provider_config, F.data.startswith('happ_pcfg_'))
    dp.callback_query.register(toggle_provider_managed, F.data.startswith('happ_ptoggle_managed_'))
    dp.callback_query.register(delete_provider, F.data.startswith('happ_pdel_'))

    # Autoreg UI
    dp.callback_query.register(start_autoreg, F.data == 'happ_autoreg')
    dp.callback_query.register(autoreg_custom_count, F.data == 'happ_autoreg_custom')
    dp.callback_query.register(autoreg_change_domain, F.data == 'happ_autoreg_domain')
    dp.callback_query.register(autoreg_clear_domain, F.data == 'happ_autoreg_domain_clr')
    dp.callback_query.register(autoreg_switch_method, F.data.startswith('happ_meth_'))
    dp.callback_query.register(autoreg_set_apikey, F.data == 'happ_autoreg_apikey')
    dp.callback_query.register(autoreg_remove_apikey, F.data == 'happ_autoreg_rmkey')
    dp.message.register(process_captcha_key, StateFilter(HappManagementStates.waiting_captcha_key))
    dp.callback_query.register(autoreg_quick_run, F.data.startswith('happ_autoreg_run_'))
    dp.message.register(process_autoreg_count, StateFilter(HappManagementStates.waiting_autoreg_count))
    dp.message.register(process_autoreg_domain, StateFilter(HappManagementStates.waiting_autoreg_domain))

    # Accounts
    dp.callback_query.register(show_accounts_list, F.data == 'happ_accounts')
    dp.callback_query.register(accounts_page, F.data.startswith('happ_acc_pg_'))
    dp.callback_query.register(delete_account, F.data.startswith('happ_rm|'))

    # Backup actions
    dp.callback_query.register(export_settings, F.data == 'happ_export')
    dp.callback_query.register(start_import, F.data == 'happ_import')
    dp.message.register(process_import_file, StateFilter(HappManagementStates.waiting_import_file))
