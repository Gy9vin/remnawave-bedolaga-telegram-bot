"""
Найти пользователей которые пытались купить подписку в период бага
(ArgumentError / KeyError) и у которых нет активной подписки.

Использование:
  python3 find_affected_purchases.py               # показать список
  python3 find_affected_purchases.py --compensate  # начислить бонус на баланс
"""

import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.database.database import AsyncSessionLocal
from app.database.models import Subscription, User

# Период бага — поправь если нужно
BUG_START = datetime(2026, 3, 29, 0, 0, 0, tzinfo=timezone.utc)
BUG_END   = datetime(2026, 3, 29, 23, 59, 59, tzinfo=timezone.utc)

# Сколько копеек начислить в качестве компенсации (0 = только показать)
COMPENSATION_KOPEKS = 0  # поставь например 50000 = 500 руб


async def main():
    compensate = '--compensate' in sys.argv

    async with AsyncSessionLocal() as db:
        # Найти все незавершённые/неуспешные попытки покупки за период
        # Ищем по platega / yookassa / и пр. — платежи созданы но not paid
        # ИЛИ просто смотрим кто пытался купить через кабинет (нет подписки, но был в сети)
        
        # 1. Транзакции типа subscription_purchase которые не завершились
        r = await db.execute(
            text("""
                SELECT DISTINCT t.user_id, u.telegram_id, u.first_name, u.username,
                       u.balance_kopeks,
                       t.amount_kopeks, t.created_at, t.description
                FROM transactions t
                JOIN users u ON u.id = t.user_id
                WHERE t.type IN ('subscription_purchase', 'subscription_renewal', 'purchase')
                  AND t.created_at BETWEEN :start AND :end
                  AND (t.is_completed = FALSE OR t.is_completed IS NULL)
                ORDER BY t.created_at DESC
            """),
            {'start': BUG_START, 'end': BUG_END},
        )
        failed_tx = r.fetchall()

        # 2. Пользователи с балансом списанным за период но без активной подписки
        r2 = await db.execute(
            text("""
                SELECT DISTINCT t.user_id, u.telegram_id, u.first_name, u.username,
                       u.balance_kopeks,
                       SUM(t.amount_kopeks) as total_charged,
                       MAX(t.created_at) as last_attempt
                FROM transactions t
                JOIN users u ON u.id = t.user_id
                LEFT JOIN subscriptions s ON s.user_id = t.user_id 
                    AND s.status IN ('active', 'trial')
                WHERE t.type IN ('subscription_purchase', 'subscription_renewal', 'purchase')
                  AND t.created_at BETWEEN :start AND :end
                  AND t.amount_kopeks < 0
                  AND s.id IS NULL
                GROUP BY t.user_id, u.telegram_id, u.first_name, u.username, u.balance_kopeks
                ORDER BY last_attempt DESC
            """),
            {'start': BUG_START, 'end': BUG_END},
        )
        charged_no_sub = r2.fetchall()

        print('=' * 72)
        print(f'ПЕРИОД БАГА: {BUG_START} → {BUG_END}')
        print('=' * 72)
        
        print(f'\n[1] Незавершённые транзакции покупки: {len(failed_tx)}')
        for row in failed_tx:
            print(f'  user_id={row[0]} tg={row[1]} {row[2]} @{row[3]}  '
                  f'баланс={row[4]/100:.2f}₽  '
                  f'сумма={abs(row[5])/100:.2f}₽  {str(row[6])[:19]}  {row[7] or ""}')

        print(f'\n[2] Деньги списаны но нет активной подписки: {len(charged_no_sub)}')
        for row in charged_no_sub:
            print(f'  user_id={row[0]} tg={row[1]} {row[2]} @{row[3]}  '
                  f'баланс={row[4]/100:.2f}₽  '
                  f'списано={abs(row[5])/100:.2f}₽  последняя_попытка={str(row[6])[:19]}')

        if not compensate:
            print('\n[i] Запусти с --compensate чтобы вернуть деньги пострадавшим')
            print('    Или измени COMPENSATION_KOPEKS для бонусного начисления')
            return

        # Компенсация: вернуть списанное + начислить бонус
        compensated = 0
        for row in charged_no_sub:
            user_id = row[0]
            charged = abs(row[5])
            total_return = charged + COMPENSATION_KOPEKS

            await db.execute(
                text("""
                    UPDATE users SET balance_kopeks = balance_kopeks + :amount
                    WHERE id = :uid
                """),
                {'amount': total_return, 'uid': user_id},
            )
            await db.execute(
                text("""
                    INSERT INTO transactions (user_id, type, amount_kopeks, is_completed, description, created_at)
                    VALUES (:uid, 'deposit', :amount, TRUE, 'Компенсация за сбой при покупке подписки 29.03.2026', NOW())
                """),
                {'uid': user_id, 'amount': total_return},
            )
            print(f'  ✅ user_id={user_id}: возврат {total_return/100:.2f}₽')
            compensated += 1

        if compensated > 0:
            await db.commit()
            print(f'\n✅ Компенсировано: {compensated} пользователей')
        else:
            print('\nПострадавших с реальными списаниями не найдено — деньги не терялись')


if __name__ == '__main__':
    asyncio.run(main())
