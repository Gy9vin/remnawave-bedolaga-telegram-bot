"""
Найти пользователей у которых:
- Есть пополнение баланса после 17:00 29.03.2026 (период бага)
- НЕТ активной подписки
- Балans > 0 (деньги есть, но подписка не активирована)

Использование:
  python3 fix_cart_users.py           # показать список
  python3 fix_cart_users.py --notify  # отправить уведомление в бот с кнопкой "Купить подписку"
"""

import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import text

from app.database.database import AsyncSessionLocal


BUG_START = datetime(2026, 3, 29, 16, 0, 0, tzinfo=timezone.utc)   # ~19:00 мск


async def main():
    notify = '--notify' in sys.argv

    async with AsyncSessionLocal() as db:
        # Пополнения за период бага, пользователи без активной подписки
        r = await db.execute(text("""
            SELECT
                u.id,
                u.telegram_id,
                u.first_name,
                u.username,
                u.balance_kopeks,
                t.amount_kopeks   AS topup,
                t.created_at      AS topup_at
            FROM transactions t
            JOIN users u ON u.id = t.user_id
            WHERE t.type = 'deposit'
              AND t.is_completed = TRUE
              AND t.created_at >= :since
              AND u.balance_kopeks > 0
              AND NOT EXISTS (
                  SELECT 1 FROM subscriptions s
                  WHERE s.user_id = u.id
                    AND s.status IN ('active', 'trial')
              )
            ORDER BY t.created_at DESC
        """), {'since': BUG_START})

        rows = r.fetchall()

        print('=' * 72)
        print(f'Пополнили баланс после {BUG_START} и нет активной подписки: {len(rows)}')
        print('=' * 72)
        for row in rows:
            uid, tg_id, name, username, balance, topup, topup_at = row
            print(f'  user_id={uid}  tg={tg_id}  {name} @{username}  '
                  f'баланс={balance/100:.2f}₽  пополнил={topup/100:.2f}₽  {str(topup_at)[:19]}')

        if not rows:
            print('  Пострадавших не найдено!')
            return

        if not notify:
            print('\n[i] Запусти с --notify чтобы отправить им уведомление с кнопкой купить')
            return

        # Отправить уведомление
        try:
            from aiogram import Bot
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
            from app.config import settings

            bot = Bot(token=settings.BOT_TOKEN)
            sent = 0
            for row in rows:
                uid, tg_id, name, username, balance, topup, topup_at = row
                if not tg_id:
                    continue
                try:
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text='🛒 Купить подписку', callback_data='buy_subscription')
                    ]])
                    await bot.send_message(
                        tg_id,
                        f'Привет! Твой баланс пополнен на {topup/100:.0f}₽.\n'
                        f'Текущий баланс: {balance/100:.0f}₽\n\n'
                        f'Из-за временной технической неполадки подписка не активировалась автоматически. '
                        f'Нажми кнопку ниже чтобы купить подписку.',
                        reply_markup=kb,
                    )
                    sent += 1
                    print(f'  ✅ Отправлено tg={tg_id} {name}')
                except Exception as e:
                    print(f'  ❌ Не удалось отправить tg={tg_id}: {e}')

            await bot.session.close()
            print(f'\nОтправлено уведомлений: {sent}/{len(rows)}')
        except Exception as e:
            print(f'Ошибка при отправке: {e}')


if __name__ == '__main__':
    asyncio.run(main())
