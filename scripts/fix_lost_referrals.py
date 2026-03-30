"""
Скрипт восстановления потерянных рефералов 15-30 марта 2026.

Причина потери: пользователь переходил по реф-ссылке в браузере,
затем авторизовывался через Telegram WebApp (deeplink) — разный localStorage.

Использование:
  python3 scripts/fix_lost_referrals.py            # dry run — только показать
  python3 scripts/fix_lost_referrals.py --apply    # применить в БД
"""

import asyncio
import sys

from sqlalchemy import select

from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.services.referral_service import process_referral_registration


# Потеряшки: ("tg:TELEGRAM_ID", "referral_code")
# Источник: анализ nginx access.log + bot.log за 15-30 марта 2026
LOST_REFERRALS = [
    # 22 марта
    ("tg:436781661",  "refqApI9o3O"),
    # 23 марта
    ("tg:1819652291", "refwzdBRveo"),
    # 28 марта
    ("tg:5582012892", "refwaG1AWUR"),
    # 30 марта
    ("tg:6288305269", "ref3b63UZZG"),
    ("tg:6041335621", "refyS9uSfgU"),
    ("tg:453596870",  "refyS9uSfgU"),
    ("tg:1935864648", "refV30UAUl7"),
    ("tg:5171727463", "ref2KxCqC72"),
    ("tg:8135246513", "ref86ucZn6b"),
    ("tg:7900226054", "ref7dCu86X7"),
    ("tg:1332402535", "ref6uEKUONE"),
]

DRY_RUN = "--apply" not in sys.argv


async def main() -> None:
    if DRY_RUN:
        print("=== DRY RUN (передай --apply чтобы применить) ===\n")
    else:
        print("=== ПРИМЕНЯЕМ ИЗМЕНЕНИЯ ===\n")

    fixed = skipped = errors = 0

    async with AsyncSessionLocal() as db:
        for identifier, referral_code in LOST_REFERRALS:
            try:
                # Получаем пользователя
                if isinstance(identifier, int):
                    user = await db.get(User, identifier)
                elif isinstance(identifier, str) and identifier.startswith("tg:"):
                    tg_id = int(identifier[3:])
                    result = await db.execute(select(User).where(User.telegram_id == tg_id))
                    user = result.scalar_one_or_none()
                else:
                    user = await db.get(User, int(identifier))

                if not user:
                    print(f"[SKIP] {identifier} — пользователь не найден в БД")
                    skipped += 1
                    continue

                if user.referred_by_id:
                    print(f"[SKIP] {identifier} (user_id={user.id}) — реферал уже назначен (referrer_id={user.referred_by_id})")
                    skipped += 1
                    continue

                # Находим реферера по коду
                result = await db.execute(select(User).where(User.referral_code == referral_code))
                referrer = result.scalar_one_or_none()

                if not referrer:
                    print(f"[SKIP] {identifier} — реф-код {referral_code!r} не найден")
                    skipped += 1
                    continue

                if referrer.id == user.id:
                    print(f"[SKIP] {identifier} — само-реферал, пропускаем")
                    skipped += 1
                    continue

                print(f"[FIX]  {identifier} (user_id={user.id}) ← {referral_code!r} → referrer_id={referrer.id} (@{referrer.username or referrer.first_name})")

                if not DRY_RUN:
                    user.referred_by_id = referrer.id
                    await db.flush()
                    await process_referral_registration(db, user.id, referrer.id, bot=None)
                    await db.commit()
                    print(f"       ✅ Готово")

                fixed += 1

            except Exception as e:
                if not DRY_RUN:
                    await db.rollback()
                print(f"[ERROR] {identifier}: {e}")
                errors += 1

    print(f"\nИтого: {'будет исправлено' if DRY_RUN else 'исправлено'}={fixed}, пропущено={skipped}, ошибок={errors}")
    if DRY_RUN and fixed > 0:
        print("Запусти с --apply чтобы применить изменения.")


if __name__ == "__main__":
    asyncio.run(main())
