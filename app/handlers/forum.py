"""Обработчик сообщений из Telegram-форума для двунаправленной тикетной системы."""

import structlog
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.ticket import TicketCRUD, TicketMessageCRUD
from app.database.models import TicketStatus
from app.services.ai_support.forum_service import ticket_forum_service
from app.services.support_settings_service import SupportSettingsService


logger = structlog.get_logger(__name__)


class IsForumSupportMessage(Filter):
    """Фильтр: сообщение из форум-чата поддержки (topic message)."""

    async def __call__(self, message: Message) -> bool:
        if not settings.SUPPORT_FORUM_ENABLED:
            return False
        if not settings.SUPPORT_FORUM_CHAT_ID:
            return False
        if message.chat.id != settings.SUPPORT_FORUM_CHAT_ID:
            return False
        if not message.is_topic_message:
            return False
        return True


class IsForumCallback(Filter):
    """Фильтр: callback из форум-чата поддержки."""

    async def __call__(self, callback: CallbackQuery) -> bool:
        if not settings.SUPPORT_FORUM_ENABLED:
            return False
        if not settings.SUPPORT_FORUM_CHAT_ID:
            return False
        if not callback.message:
            return False
        if callback.message.chat.id != settings.SUPPORT_FORUM_CHAT_ID:
            return False
        return True


async def on_forum_message(message: Message, db: AsyncSession, bot: Bot) -> None:
    """Обработка сообщений оператора из форум-темы тикета."""
    try:
        if message.from_user is None or message.from_user.is_bot:
            return

        if not message.text:
            return

        forum_thread_id = message.message_thread_id
        if not forum_thread_id:
            return

        ticket = await TicketCRUD.get_ticket_by_forum_thread(db, forum_thread_id)
        if not ticket:
            return
        if ticket.status == TicketStatus.CLOSED.value:
            return

        from_user_id = message.from_user.id
        is_operator = from_user_id in settings.ADMIN_IDS or SupportSettingsService.is_moderator(from_user_id)
        if not is_operator:
            return

        # Загружаем тикет с данными пользователя
        ticket = await TicketCRUD.get_ticket_by_id(db, ticket.id, load_messages=False, load_user=True)
        if not ticket or not ticket.user:
            logger.warning('Тикет или пользователь не найден', ticket_id=ticket.id if ticket else None)
            return

        # Сохраняем сообщение оператора в БД
        await TicketMessageCRUD.add_message(
            db,
            ticket_id=ticket.id,
            user_id=ticket.user_id,
            message_text=message.text,
            is_from_admin=True,
        )

        # Выключаем AI при ответе оператора
        ticket.ai_enabled = False
        await db.commit()

        # Отправляем ответ пользователю в бот
        tg_id = getattr(ticket.user, 'telegram_id', None)
        if tg_id:
            try:
                await bot.send_message(
                    chat_id=tg_id,
                    text=f'\U0001f4ac <b>Ответ оператора:</b>\n\n{message.text}',
                    parse_mode='HTML',
                )
            except Exception as e:
                logger.error(
                    'Ошибка отправки ответа пользователю',
                    telegram_id=tg_id,
                    ticket_id=ticket.id,
                    error=e,
                )
        else:
            logger.warning('У пользователя нет telegram_id', user_id=ticket.user_id, ticket_id=ticket.id)

    except Exception as e:
        logger.error('Ошибка обработки сообщения из форума', error=e)


async def on_forum_callback(callback: CallbackQuery, db: AsyncSession, bot: Bot) -> None:
    """Обработка callback-кнопок управления тикетом в форуме."""
    try:
        data = callback.data
        if not data:
            return

        if data.startswith('forum_ai_toggle:'):
            ticket_id_str = data.removeprefix('forum_ai_toggle:')
            try:
                ticket_id = int(ticket_id_str)
            except ValueError:
                await callback.answer('Некорректный ID тикета', show_alert=True)
                return

            ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False)
            if not ticket:
                await callback.answer('Тикет не найден', show_alert=True)
                return

            new_ai_enabled = not ticket.ai_enabled
            ticket.ai_enabled = new_ai_enabled
            await db.commit()

            # Обновляем кнопки управления
            thread_id = ticket.forum_topic_id
            ctrl_msg_id = ticket.forum_control_msg_id
            if thread_id and ctrl_msg_id:
                await ticket_forum_service.update_ai_button(bot, thread_id, ctrl_msg_id, ticket_id, new_ai_enabled)

            status_text = 'включен' if new_ai_enabled else 'выключен'
            await callback.answer(f'ИИ {status_text}', show_alert=False)

        elif data.startswith('forum_close_ticket:'):
            ticket_id_str = data.removeprefix('forum_close_ticket:')
            try:
                ticket_id = int(ticket_id_str)
            except ValueError:
                await callback.answer('Некорректный ID тикета', show_alert=True)
                return

            ticket = await TicketCRUD.get_ticket_by_id(db, ticket_id, load_messages=False, load_user=True)
            if not ticket:
                await callback.answer('Тикет не найден', show_alert=True)
                return

            await TicketCRUD.close_ticket(db, ticket_id)

            # Закрываем форум-тему
            forum_topic_id = ticket.forum_topic_id
            if forum_topic_id:
                await ticket_forum_service.close_topic(bot, forum_topic_id)

            # Уведомляем пользователя о закрытии
            if ticket.user:
                tg_id = getattr(ticket.user, 'telegram_id', None)
                if tg_id:
                    try:
                        await bot.send_message(
                            chat_id=tg_id,
                            text='\U0001f512 <b>Тикет закрыт</b>\n\nВаш тикет был закрыт оператором.',
                            parse_mode='HTML',
                        )
                    except Exception as e:
                        logger.error(
                            'Ошибка уведомления о закрытии тикета',
                            telegram_id=tg_id,
                            ticket_id=ticket_id,
                            error=e,
                        )

            await callback.answer('Тикет закрыт', show_alert=False)

    except Exception as e:
        logger.error('Ошибка обработки callback из форума', error=e)
        try:
            await callback.answer('Произошла ошибка', show_alert=True)
        except Exception:
            pass


def register_handlers(dp: Dispatcher) -> None:
    """Зарегистрировать обработчики форума."""
    dp.message.register(on_forum_message, IsForumSupportMessage())
    dp.callback_query.register(on_forum_callback, IsForumCallback(), F.data.startswith('forum_'))
