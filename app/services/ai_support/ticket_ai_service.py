"""AI сервис для автоматических ответов в тикетах."""

from datetime import UTC, datetime
from pathlib import Path

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)

_KB_PATH = Path(__file__).parent / 'knowledge_base.md'
_RULES_PATH = Path(__file__).parent / 'ai_rules.md'


def _load_text(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except Exception:
        return ''


def _get_ai_mode() -> str:
    """Получить текущий режим AI из SupportSettingsService."""
    try:
        from app.services.support_settings_service import SupportSettingsService

        return SupportSettingsService.get_ticket_ai_mode()
    except Exception:
        return 'off'


def _get_bot():
    """Получить экземпляр бота из maintenance_service."""
    try:
        from app.services.maintenance_service import maintenance_service

        return maintenance_service.bot
    except Exception:
        return None


class TicketAIService:
    """Сервис AI ответов на тикеты через GigaChat."""

    async def build_user_context(self, db, user_id: int) -> str:
        """Собрать текстовый контекст о пользователе."""
        try:
            from app.database.crud.user import get_user_by_id

            user = await get_user_by_id(db, user_id)
            if not user:
                return 'Пользователь не найден.'

            lines = [f'Данные пользователя (ID: {user_id}):']
            name = getattr(user, 'first_name', '') or ''
            if name:
                lines.append(f'Имя: {name}')

            balance_kopeks = getattr(user, 'balance_kopeks', 0) or 0
            lines.append(f'Баланс: {balance_kopeks / 100:.2f} руб.')

            # Загрузить подписку
            try:
                from app.database.crud.subscription import get_subscription_by_user_id

                sub = await get_subscription_by_user_id(db, user.id)
                if sub:
                    status = getattr(sub, 'status', 'unknown')
                    lines.append(f'Статус подписки: {status}')
                    end_date = getattr(sub, 'end_date', None)
                    if end_date:
                        lines.append(f'Подписка до: {end_date.strftime("%d.%m.%Y %H:%M")}')
                    traffic_limit = getattr(sub, 'traffic_limit_bytes', None)
                    traffic_used = getattr(sub, 'used_traffic_bytes', None)
                    if traffic_limit:
                        limit_gb = traffic_limit / (1024**3)
                        used_gb = (traffic_used or 0) / (1024**3)
                        lines.append(f'Трафик: {used_gb:.1f}/{limit_gb:.1f} ГБ использовано')
                else:
                    lines.append('Активной подписки нет.')
            except Exception as e:
                logger.debug('Не удалось загрузить подписку', error=e)
                lines.append('Подписка: данные недоступны')

            return '\n'.join(lines)
        except Exception as e:
            logger.error('Ошибка сборки контекста пользователя', error=e)
            return 'Контекст пользователя недоступен.'

    async def get_history(self, db, ticket_id: int) -> list[dict]:
        """Получить последние 10 сообщений тикета как историю чата."""
        try:
            from app.database.crud.ticket import TicketMessageCRUD

            messages = await TicketMessageCRUD.get_ticket_messages(db, ticket_id, limit=10)
            result = []
            for msg in messages:
                is_ai = getattr(msg, 'is_ai_response', False)
                is_admin = getattr(msg, 'is_from_admin', False)
                role = 'assistant' if (is_admin or is_ai) else 'user'
                result.append({'role': role, 'content': msg.message_text or ''})
            return result
        except Exception as e:
            logger.error('Ошибка получения истории тикета', ticket_id=ticket_id, error=e)
            return []

    def _build_system_prompt(self, user_context: str) -> str:
        """Собрать system prompt из правил, базы знаний и контекста пользователя."""
        bot_name = getattr(settings, 'SUPPORT_AI_BOT_NAME', 'Алиса')
        rules = _load_text(_RULES_PATH).replace('{bot_name}', bot_name)
        knowledge = _load_text(_KB_PATH)
        return f'{rules}\n\n## База знаний\n{knowledge}\n\n## {user_context}'

    async def generate_reply(self, db, ticket_id: int, user_id: int) -> str | None:
        """Сгенерировать ответ ИИ на тикет."""
        if not getattr(settings, 'GIGACHAT_AUTH_KEY', None):
            return None
        try:
            from app.services.ai_support.gigachat_client import gigachat_client

            user_context = await self.build_user_context(db, user_id)
            history = await self.get_history(db, ticket_id)
            system_prompt = self._build_system_prompt(user_context)
            return await gigachat_client.chat(messages=history, system_prompt=system_prompt)
        except Exception as e:
            logger.error('Ошибка генерации AI ответа', ticket_id=ticket_id, error=e)
            return None

    async def _send_ai_reply(self, db, ticket, user_id: int, ai_reply: str, bot=None) -> None:
        """Сохранить AI ответ, отправить пользователю и зеркалировать в форум."""
        from app.database.models import TicketMessage, TicketStatus

        msg_obj = TicketMessage(
            ticket_id=ticket.id,
            user_id=user_id,
            message_text=ai_reply,
            is_from_admin=True,
            is_ai_response=True,
        )
        db.add(msg_obj)
        ticket.status = TicketStatus.ANSWERED.value
        ticket.updated_at = datetime.now(UTC)
        await db.commit()

        # Отправить пользователю
        if bot:
            from app.database.crud.user import get_user_by_id

            db_user = await get_user_by_id(db, user_id)
            tg_id = getattr(db_user, 'telegram_id', None) if db_user else None
            if tg_id:
                try:
                    await bot.send_message(
                        chat_id=tg_id,
                        text=f'💬 <b>Ответ поддержки:</b>\n\n{ai_reply}',
                        parse_mode='HTML',
                    )
                except Exception as e:
                    logger.error('Ошибка отправки AI ответа пользователю', error=e)

        # Зеркало в форум
        forum_topic_id = getattr(ticket, 'forum_topic_id', None)
        if bot and forum_topic_id:
            from app.services.ai_support.forum_service import ticket_forum_service

            await ticket_forum_service.send_message(
                bot=bot,
                forum_topic_id=forum_topic_id,
                text=ai_reply,
                role='ai',
            )

    async def handle_ticket_created(self, event_data: dict) -> None:
        """Обработать событие создания тикета (callback от event_emitter).

        event_data = {'type': 'ticket.created', 'payload': {...}, 'timestamp': ...}
        """
        try:
            ai_mode = _get_ai_mode()
            if ai_mode == 'off':
                return

            payload = event_data.get('payload', {})
            ticket_id = payload.get('ticket_id')
            user_id = payload.get('user_id')
            if not ticket_id or not user_id:
                return

            bot = _get_bot()
            from app.database.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                from app.database.crud.ticket import TicketCRUD, TicketMessageCRUD
                from app.database.crud.user import get_user_by_id
                from app.services.ai_support.forum_service import ticket_forum_service

                ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False, load_user=True)
                if not ticket:
                    return

                db_user = await get_user_by_id(db, user_id)
                if not db_user:
                    return

                # Загрузить подписку
                subscription = None
                try:
                    from app.database.crud.subscription import get_subscription_by_user_id

                    subscription = await get_subscription_by_user_id(db, user_id)
                except Exception:
                    pass

                # Создать форум-тему
                forum_topic_id = None
                if ticket_forum_service.forum_enabled and bot:
                    user_card = ticket_forum_service.format_user_card(db_user, subscription)
                    ai_enabled = ai_mode == 'ai'
                    forum_topic_id, control_msg_id = await ticket_forum_service.create_topic(
                        bot=bot,
                        ticket_id=ticket_id,
                        title=ticket.title,
                        user_card_text=user_card,
                        ai_enabled=ai_enabled,
                    )

                    # Зеркало первого сообщения в форум
                    first_msg = await TicketMessageCRUD.get_first_message(db, ticket_id)
                    if first_msg and forum_topic_id:
                        await ticket_forum_service.send_message(
                            bot=bot,
                            forum_topic_id=forum_topic_id,
                            text=first_msg.message_text or '',
                            role='user',
                        )

                    # Сохранить forum_topic_id и control_msg_id
                    if forum_topic_id:
                        ticket.forum_topic_id = forum_topic_id
                        ticket.forum_control_msg_id = control_msg_id
                        ticket.ai_enabled = ai_mode == 'ai'
                        await db.commit()

                # Если режим AI — сгенерировать ответ
                if ai_mode == 'ai' and bot:
                    ai_reply = await self.generate_reply(db, ticket_id, user_id)
                    if ai_reply:
                        await self._send_ai_reply(db, ticket, user_id, ai_reply, bot)

        except Exception as e:
            logger.error('Ошибка handle_ticket_created', error=e)

    async def handle_ticket_message(self, event_data: dict) -> None:
        """Обработать событие нового сообщения в тикете (callback от event_emitter).

        event_data = {'type': 'ticket.message_added', 'payload': {...}, 'timestamp': ...}
        """
        try:
            payload = event_data.get('payload', {})
            is_from_admin = payload.get('is_from_admin', False)
            if is_from_admin:
                return  # AI отвечает только на сообщения пользователей

            ai_mode = _get_ai_mode()
            if ai_mode != 'ai':
                return

            ticket_id = payload.get('ticket_id')
            user_id = payload.get('user_id')
            message_text = payload.get('message_text', '')
            if not ticket_id or not user_id:
                return

            bot = _get_bot()
            from app.database.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                from app.database.crud.ticket import TicketCRUD
                from app.services.ai_support.forum_service import ticket_forum_service

                ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False)
                if not ticket:
                    return

                # Проверить что AI включён для этого тикета
                ai_enabled = getattr(ticket, 'ai_enabled', True)
                if not ai_enabled:
                    return

                # Зеркало нового сообщения в форум
                forum_topic_id = getattr(ticket, 'forum_topic_id', None)
                if forum_topic_id and bot:
                    await ticket_forum_service.send_message(
                        bot=bot,
                        forum_topic_id=forum_topic_id,
                        text=message_text,
                        role='user',
                    )

                # Генерировать AI ответ
                if not getattr(settings, 'GIGACHAT_AUTH_KEY', None):
                    return

                ai_reply = await self.generate_reply(db, ticket_id, user_id)
                if not ai_reply:
                    return

                await self._send_ai_reply(db, ticket, user_id, ai_reply, bot)

        except Exception as e:
            logger.error('Ошибка handle_ticket_message', error=e)


ai_ticket_service = TicketAIService()
