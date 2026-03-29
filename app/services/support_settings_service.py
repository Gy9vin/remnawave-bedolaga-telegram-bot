import json
from pathlib import Path

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class SupportSettingsService:
    """Runtime editable support settings with JSON persistence."""

    _storage_path: Path = Path('data/support_settings.json')
    _data: dict = {}
    _loaded: bool = False

    @classmethod
    def _ensure_dir(cls) -> None:
        try:
            cls._storage_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error('Failed to ensure settings dir', error=e)

    @classmethod
    def _load(cls) -> None:
        if cls._loaded:
            return
        cls._ensure_dir()
        try:
            if cls._storage_path.exists():
                cls._data = json.loads(cls._storage_path.read_text(encoding='utf-8'))
            else:
                cls._data = {}
        except Exception as e:
            logger.error('Failed to load support settings', error=e)
            cls._data = {}
        cls._loaded = True

    @classmethod
    def _save(cls) -> bool:
        cls._ensure_dir()
        try:
            cls._storage_path.write_text(json.dumps(cls._data, ensure_ascii=False, indent=2), encoding='utf-8')
            return True
        except Exception as e:
            logger.error('Failed to save support settings', error=e)
            return False

    # Mode
    @classmethod
    def get_system_mode(cls) -> str:
        cls._load()
        mode = (cls._data.get('system_mode') or settings.get_support_system_mode()).strip().lower()
        return mode if mode in {'tickets', 'contact', 'both'} else 'both'

    @classmethod
    def set_system_mode(cls, mode: str) -> bool:
        mode_clean = (mode or '').strip().lower()
        if mode_clean not in {'tickets', 'contact', 'both'}:
            return False
        cls._load()
        cls._data['system_mode'] = mode_clean
        settings.SUPPORT_SYSTEM_MODE = mode_clean
        return cls._save()

    # Main menu visibility
    @classmethod
    def is_support_menu_enabled(cls) -> bool:
        cls._load()
        if 'menu_enabled' in cls._data:
            return bool(cls._data['menu_enabled'])
        return bool(settings.SUPPORT_MENU_ENABLED)

    @classmethod
    def set_support_menu_enabled(cls, enabled: bool) -> bool:
        cls._load()
        cls._data['menu_enabled'] = bool(enabled)
        return cls._save()

    # Contact vs tickets helpers
    @classmethod
    def is_tickets_enabled(cls) -> bool:
        return cls.get_system_mode() in {'tickets', 'both'}

    @classmethod
    def is_contact_enabled(cls) -> bool:
        return cls.get_system_mode() in {'contact', 'both'}

    # Descriptions (per language)
    @classmethod
    def get_support_info_text(cls, language: str) -> str:
        cls._load()
        lang = (language or settings.DEFAULT_LANGUAGE).split('-')[0].lower()
        overrides = cls._data.get('support_info_texts') or {}
        text = overrides.get(lang)
        if text and isinstance(text, str) and text.strip():
            return text
        # Fallback to dynamic localization default
        from app.localization.texts import get_texts

        return get_texts(lang).SUPPORT_INFO

    @classmethod
    def set_support_info_text(cls, language: str, text: str) -> bool:
        cls._load()
        lang = (language or settings.DEFAULT_LANGUAGE).split('-')[0].lower()
        texts_map = cls._data.get('support_info_texts') or {}
        texts_map[lang] = text or ''
        cls._data['support_info_texts'] = texts_map
        return cls._save()

    # Notifications & SLA
    @classmethod
    def get_admin_ticket_notifications_enabled(cls) -> bool:
        cls._load()
        if 'admin_ticket_notifications_enabled' in cls._data:
            return bool(cls._data['admin_ticket_notifications_enabled'])
        # fallback to global admin notifications setting
        return bool(settings.is_admin_notifications_enabled())

    @classmethod
    def set_admin_ticket_notifications_enabled(cls, enabled: bool) -> bool:
        cls._load()
        cls._data['admin_ticket_notifications_enabled'] = bool(enabled)
        return cls._save()

    @classmethod
    def get_user_ticket_notifications_enabled(cls) -> bool:
        cls._load()
        if 'user_ticket_notifications_enabled' in cls._data:
            return bool(cls._data['user_ticket_notifications_enabled'])
        # fallback to global enable notifications
        return bool(getattr(settings, 'ENABLE_NOTIFICATIONS', True))

    @classmethod
    def set_user_ticket_notifications_enabled(cls, enabled: bool) -> bool:
        cls._load()
        cls._data['user_ticket_notifications_enabled'] = bool(enabled)
        return cls._save()

    @classmethod
    def get_sla_enabled(cls) -> bool:
        cls._load()
        if 'ticket_sla_enabled' in cls._data:
            return bool(cls._data['ticket_sla_enabled'])
        return bool(getattr(settings, 'SUPPORT_TICKET_SLA_ENABLED', True))

    @classmethod
    def set_sla_enabled(cls, enabled: bool) -> bool:
        cls._load()
        cls._data['ticket_sla_enabled'] = bool(enabled)
        return cls._save()

    @classmethod
    def get_sla_minutes(cls) -> int:
        cls._load()
        minutes = cls._data.get('ticket_sla_minutes')
        if isinstance(minutes, int) and minutes > 0:
            return minutes
        return int(getattr(settings, 'SUPPORT_TICKET_SLA_MINUTES', 5))

    @classmethod
    def set_sla_minutes(cls, minutes: int) -> bool:
        try:
            minutes_int = int(minutes)
        except Exception:
            return False
        if minutes_int <= 0:
            return False
        cls._load()
        cls._data['ticket_sla_minutes'] = minutes_int
        return cls._save()

    # Moderators management
    @classmethod
    def get_moderators(cls) -> list[int]:
        cls._load()
        raw = cls._data.get('moderators') or []
        moderators: list[int] = []
        for item in raw:
            try:
                moderators.append(int(item))
            except Exception:
                continue
        return moderators

    @classmethod
    def is_moderator(cls, telegram_id: int) -> bool:
        try:
            tid = int(telegram_id)
        except Exception:
            return False
        return tid in cls.get_moderators()

    @classmethod
    def add_moderator(cls, telegram_id: int) -> bool:
        try:
            tid = int(telegram_id)
        except Exception:
            return False
        cls._load()
        moderators = set(cls.get_moderators())
        moderators.add(tid)
        cls._data['moderators'] = sorted(moderators)
        return cls._save()

    @classmethod
    def remove_moderator(cls, telegram_id: int) -> bool:
        try:
            tid = int(telegram_id)
        except Exception:
            return False
        cls._load()
        moderators = set(cls.get_moderators())
        if tid in moderators:
            moderators.remove(tid)
            cls._data['moderators'] = sorted(moderators)
            return cls._save()
        return True

    # Cabinet notifications (веб-кабинет)
    @classmethod
    def get_cabinet_user_notifications_enabled(cls) -> bool:
        """Уведомления юзерам в кабинет о ответе админа на тикет."""
        cls._load()
        if 'cabinet_user_notifications_enabled' in cls._data:
            return bool(cls._data['cabinet_user_notifications_enabled'])
        return True  # По умолчанию включено

    @classmethod
    def set_cabinet_user_notifications_enabled(cls, enabled: bool) -> bool:
        cls._load()
        cls._data['cabinet_user_notifications_enabled'] = bool(enabled)
        return cls._save()

    @classmethod
    def get_cabinet_admin_notifications_enabled(cls) -> bool:
        """Уведомления админам в кабинет о новых тикетах."""
        cls._load()
        if 'cabinet_admin_notifications_enabled' in cls._data:
            return bool(cls._data['cabinet_admin_notifications_enabled'])
        return True  # По умолчанию включено

    @classmethod
    def set_cabinet_admin_notifications_enabled(cls, enabled: bool) -> bool:
        cls._load()
        cls._data['cabinet_admin_notifications_enabled'] = bool(enabled)
        return cls._save()

    # Ticket AI mode
    @classmethod
    def get_ticket_ai_mode(cls) -> str:
        """Получить режим тикетов: 'off' | 'normal' | 'ai'."""
        cls._load()
        mode = (cls._data.get('ticket_ai_mode') or 'normal').strip().lower()
        return mode if mode in {'off', 'normal', 'ai'} else 'normal'

    @classmethod
    def set_ticket_ai_mode(cls, mode: str) -> bool:
        """Установить режим тикетов: 'off' | 'normal' | 'ai'."""
        mode_clean = (mode or '').strip().lower()
        if mode_clean not in {'off', 'normal', 'ai'}:
            return False
        cls._load()
        cls._data['ticket_ai_mode'] = mode_clean
        return cls._save()

    # AI Bot settings
    VALID_AI_STYLES = frozenset({'friendly', 'formal', 'brief', 'empathetic'})

    @classmethod
    def get_ai_names(cls) -> list[str]:
        """Получить список имён AI-агента."""
        cls._load()
        names = cls._data.get('ai_bot_names')
        if isinstance(names, list) and names:
            result = [n.strip() for n in names if isinstance(n, str) and n.strip()]
            if result:
                return result
        # Совместимость со старым полем
        old_name = cls._data.get('ai_bot_name')
        if old_name and isinstance(old_name, str) and old_name.strip():
            return [old_name.strip()]
        return [getattr(settings, 'SUPPORT_AI_BOT_NAME', 'Алиса')]

    @classmethod
    def get_ai_name(cls) -> str:
        """Получить первое имя (для обратной совместимости)."""
        return cls.get_ai_names()[0]

    @classmethod
    def get_random_ai_name(cls) -> str:
        """Выбрать случайное имя из списка."""
        import random

        names = cls.get_ai_names()
        return random.choice(names)

    @classmethod
    def set_ai_names(cls, names: list[str]) -> bool:
        """Установить список имён AI-агента."""
        cleaned = [n.strip() for n in names if isinstance(n, str) and n.strip() and len(n.strip()) <= 32]
        if not cleaned:
            return False
        cls._load()
        cls._data['ai_bot_names'] = cleaned
        cls._data.pop('ai_bot_name', None)  # Убрать старое поле
        return cls._save()

    @classmethod
    def set_ai_name(cls, name: str) -> bool:
        """Установить одно имя (обратная совместимость)."""
        return cls.set_ai_names([name])

    # AI Test mode
    @classmethod
    def get_ai_test_telegram_id(cls) -> int | None:
        """Получить telegram_id тестового пользователя (None = тест-режим выключен)."""
        cls._load()
        tid = cls._data.get('ai_test_telegram_id')
        if tid is None:
            return None
        try:
            return int(tid)
        except (TypeError, ValueError):
            return None

    @classmethod
    def set_ai_test_telegram_id(cls, telegram_id: int) -> bool:
        """Включить тест-режим для конкретного пользователя."""
        try:
            tid = int(telegram_id)
        except (TypeError, ValueError):
            return False
        cls._load()
        cls._data['ai_test_telegram_id'] = tid
        return cls._save()

    @classmethod
    def clear_ai_test_telegram_id(cls) -> bool:
        """Выключить тест-режим."""
        cls._load()
        cls._data.pop('ai_test_telegram_id', None)
        return cls._save()

    @classmethod
    def get_ai_style(cls) -> str:
        """Получить стиль ответов: 'friendly' | 'formal' | 'brief' | 'empathetic'."""
        cls._load()
        style = (cls._data.get('ai_response_style') or 'friendly').strip().lower()
        return style if style in cls.VALID_AI_STYLES else 'friendly'

    @classmethod
    def set_ai_style(cls, style: str) -> bool:
        """Установить стиль ответов AI."""
        style_clean = (style or '').strip().lower()
        if style_clean not in cls.VALID_AI_STYLES:
            return False
        cls._load()
        cls._data['ai_response_style'] = style_clean
        return cls._save()
