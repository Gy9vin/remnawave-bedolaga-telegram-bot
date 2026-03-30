"""
Скрипт для восстановления потерянных рефералов.

Пользователи, которые пришли по реф-ссылке через кабинет
(Telegram WebApp — другой localStorage), но реф не записался.

Запуск: python3 scripts/fix_lost_referrals.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import structlog
from sqlalchemy import select

from app.database.engine import get_session_maker
from app.database.crud.user import get_user_by_telegram_id, get_user_by_referral_code
from app.services.referral_service import process_referral_registration

logger = structlog.get_logger(__name__)

# Потеряшки: (telegram_id, referral_code)
LOST_REFERRALS = [
    (6288305269, "ref3b63UZZG"),
    (6041335621, "refyS9uSfgU"),
    (453596870,  "refyS9uSfgU"),
    (1935864648, "refV30UAUl7"),
    (5171727463, "ref2KxCqC72"),
    (8135246513, "ref86ucZn6b"),
    (7900226054, "ref7dCu86X7"),
    (1332402535, "ref6uEKUONE"),
]


async def fix_lost_referrals() -> None:
    session_maker = get_session_maker()

    fixed = 0
    skipped = 0
    errors = 0

    async with session_maker() as db:
        for telegram_id, referral_code in LOST_REFERRALS:
            try:
                user = await get_user_by_telegram_id(db, telegram_id)
                if not user:
                    print(f"[SKIP] telegram_id={telegram_id} — пользователь не найден в БД")
                    skipped += 1
                    continue

                if user.referred_by_id:
                    print(f"[SKIP] telegram_id={telegram_id} (user_id={user.id}) — реферал уже назначен (referrer_id={user.referred_by_id})")
                    skipped += 1
                    continue

                referrer = await get_user_by_referral_code(db, referral_code)
                if not referrer:
                    print(f"[SKIP] telegram_id={telegram_id} — реф-код {referral_code!r} не найден")
                    skipped += 1
                    continue

                if referrer.id == user.id:
                    print(f"[SKIP] telegram_id={telegram_id} — само-реферал, пропускаем")
                    skipped += 1
                    continue

                print(f"[FIX]  telegram_id={telegram_id} (user_id={user.id}) ← referral_code={referral_code!r} → referrer_id={referrer.id} (@{referrer.username or referrer.first_name})")

                user.referred_by_id = referrer.id
                await db.flush()
                await process_referral_registration(db, user.id, referrer.id, bot=None)
                await db.commit()

                print(f"       ✅ Готово: user_id={user.id} → referred_by_id={referrer.id}")
                fixed += 1

            except Exception as e:
                await db.rollback()
                print(f"[ERROR] telegram_id={telegram_id}: {e}")
                errors += 1

    print()
    print(f"Итого: исправлено={fixed}, пропущено={skipped}, ошибок={errors}")


if __name__ == "__main__":
    asyncio.run(fix_lost_referrals())
