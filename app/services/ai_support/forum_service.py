"""Сервис управления темами форума для тикетной системы."""

import structlog
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings


logger = structlog.get_logger(__name__)


class TicketForumService:
    """Управление темами Telegram-форума для зеркалирования тикетов."""

    @property
    def forum_chat_id(self) -> int | None:
        return getattr(settings, 'SUPPORT_FORUM_CHAT_ID', None)

    @property
    def forum_enabled(self) -> bool:
        val = getattr(settings, 'SUPPORT_FORUM_ENABLED', False)
        return bool(val) and bool(self.forum_chat_id)

    async def create_topic(
        self,
        bot: Bot,
        ticket_id: int,
        title: str,
        user_card_text: str,
        ai_enabled: bool = False,
    ) -> tuple[int | None, int | None]:
        """Создать тему в форуме. Возвращает (forum_topic_id, control_msg_id)."""
        if not self.forum_enabled:
            return None, None
        try:
            topic = await bot.create_forum_topic(
                chat_id=self.forum_chat_id,
                name=f'#{ticket_id} {title[:50]}',
            )
            forum_topic_id = topic.message_thread_id

            # Отправить карточку пользователя
            await bot.send_message(
                chat_id=self.forum_chat_id,
                message_thread_id=forum_topic_id,
                text=user_card_text,
                parse_mode='HTML',
            )

            # Отправить кнопки управления
            kb = self._build_control_keyboard(ticket_id, ai_enabled)
            ctrl_msg = await bot.send_message(
                chat_id=self.forum_chat_id,
                message_thread_id=forum_topic_id,
                text='⚙️ <b>Управление тикетом</b>',
                parse_mode='HTML',
                reply_markup=kb,
            )
            return forum_topic_id, ctrl_msg.message_id
        except TelegramAPIError as e:
            logger.error('Ошибка создания форум-темы', ticket_id=ticket_id, error=e)
            return None, None

    def _build_control_keyboard(self, ticket_id: int, ai_enabled: bool) -> InlineKeyboardMarkup:
        ai_btn_text = '🤖 ИИ вкл' if ai_enabled else '🤖 ИИ выкл'
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=ai_btn_text,
                        callback_data=f'forum_ai_toggle:{ticket_id}',
                    ),
                    InlineKeyboardButton(
                        text='🔒 Закрыть',
                        callback_data=f'forum_close_ticket:{ticket_id}',
                    ),
                ]
            ]
        )

    async def send_message(
        self,
        bot: Bot,
        forum_topic_id: int,
        text: str,
        role: str = 'system',
    ) -> bool:
        """Отправить сообщение в форум-тему.
        role: 'user' | 'ai' | 'operator' | 'system'
        """
        if not self.forum_enabled or not forum_topic_id:
            return False
        prefix_map = {
            'user': '👤 <b>Пользователь:</b>',
            'ai': '🤖 <b>ИИ:</b>',
            'operator': '👨‍💼 <b>Оператор:</b>',
            'system': '⚙️ <b>Система:</b>',
        }
        prefix = prefix_map.get(role, '⚙️ <b>Система:</b>')
        full_text = f'{prefix}\n{text}'
        try:
            await bot.send_message(
                chat_id=self.forum_chat_id,
                message_thread_id=forum_topic_id,
                text=full_text[:4096],
                parse_mode='HTML',
            )
            return True
        except TelegramAPIError as e:
            logger.error('Ошибка отправки в форум', forum_topic_id=forum_topic_id, error=e)
            return False

    async def close_topic(self, bot: Bot, forum_topic_id: int) -> bool:
        """Закрыть форум-тему и отправить уведомление."""
        if not self.forum_enabled or not forum_topic_id:
            return False
        try:
            await bot.send_message(
                chat_id=self.forum_chat_id,
                message_thread_id=forum_topic_id,
                text='⚙️ <b>Система:</b>\n🔒 Тикет закрыт',
                parse_mode='HTML',
            )
            await bot.close_forum_topic(
                chat_id=self.forum_chat_id,
                message_thread_id=forum_topic_id,
            )
            return True
        except TelegramAPIError as e:
            logger.error('Ошибка закрытия форум-темы', forum_topic_id=forum_topic_id, error=e)
            return False

    async def update_ai_button(
        self,
        bot: Bot,
        forum_topic_id: int,
        control_msg_id: int,
        ticket_id: int,
        ai_enabled: bool,
    ) -> bool:
        """Обновить кнопки управления (смена текста кнопки AI)."""
        if not self.forum_enabled or not forum_topic_id or not control_msg_id:
            return False
        try:
            kb = self._build_control_keyboard(ticket_id, ai_enabled)
            await bot.edit_message_reply_markup(
                chat_id=self.forum_chat_id,
                message_id=control_msg_id,
                reply_markup=kb,
            )
            return True
        except TelegramAPIError as e:
            logger.error('Ошибка обновления кнопок форума', error=e)
            return False

    def format_user_card(
        self,
        user,
        subscription=None,
    ) -> str:
        """Форматировать карточку пользователя для форума."""
        lines = ['👤 <b>Информация о пользователе</b>']
        name = getattr(user, 'first_name', '') or ''
        last = getattr(user, 'last_name', '') or ''
        full_name = f'{name} {last}'.strip() or '—'
        lines.append(f'Имя: {full_name}')

        tg_id = getattr(user, 'telegram_id', None)
        username = getattr(user, 'username', None)
        if tg_id:
            lines.append(f'Telegram ID: <code>{tg_id}</code>')
        if username:
            lines.append(f'Username: @{username}')

        balance_kopeks = getattr(user, 'balance_kopeks', 0) or 0
        balance_rub = balance_kopeks / 100
        lines.append(f'Баланс: {balance_rub:.2f} руб.')

        if subscription:
            status = getattr(subscription, 'status', '—')
            lines.append(f'Статус подписки: {status}')
            end_date = getattr(subscription, 'end_date', None)
            if end_date:
                try:
                    lines.append(f'До: {end_date.strftime("%d.%m.%Y")}')
                except Exception:
                    pass
            traffic_limit = getattr(subscription, 'traffic_limit_bytes', None)
            traffic_used = getattr(subscription, 'used_traffic_bytes', None)
            if traffic_limit:
                limit_gb = traffic_limit / (1024**3)
                used_gb = (traffic_used or 0) / (1024**3)
                lines.append(f'Трафик: {used_gb:.1f}/{limit_gb:.1f} ГБ')
        else:
            lines.append('Подписка: нет')

        return '\n'.join(lines)


ticket_forum_service = TicketForumService()
