"""CRUD operations for BlacklistException model."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import BlacklistException


async def get_all_exceptions(db: AsyncSession) -> list[BlacklistException]:
    """Get all blacklist exceptions."""
    result = await db.execute(select(BlacklistException).order_by(BlacklistException.id))
    return list(result.scalars().all())


async def get_exception_by_telegram_id(db: AsyncSession, telegram_id: int) -> BlacklistException | None:
    """Get exception by telegram_id."""
    result = await db.execute(select(BlacklistException).where(BlacklistException.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def get_exceptions_count(db: AsyncSession) -> int:
    """Get count of blacklist exceptions."""
    result = await db.execute(select(func.count()).select_from(BlacklistException))
    return result.scalar_one()


async def add_exception(db: AsyncSession, telegram_id: int, comment: str = '') -> BlacklistException:
    """Add a blacklist exception."""
    exc = BlacklistException(telegram_id=telegram_id, comment=comment or None)
    db.add(exc)
    await db.commit()
    await db.refresh(exc)
    return exc


async def remove_exception(db: AsyncSession, telegram_id: int) -> bool:
    """Remove a blacklist exception by telegram_id."""
    exc = await get_exception_by_telegram_id(db, telegram_id)
    if not exc:
        return False
    await db.delete(exc)
    await db.commit()
    return True
