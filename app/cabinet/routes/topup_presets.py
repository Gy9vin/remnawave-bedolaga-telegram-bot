"""Готовые суммы пополнения баланса.

Суммы равны ценам действующих периодов, чтобы человек пополнил ровно на тариф и
купил без остатка на балансе. Готовых сумм в конфиге нет — выводим из цен.
"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.config import settings
from app.database.models import User

logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/balance', tags=['Cabinet Balance'])

# Больше четырёх кнопок в ряд на телефоне не помещается.
_MAX_PRESETS = 4


def build_presets(period_prices: object) -> list[dict[str, int]]:
    """Собрать список готовых сумм из карты «дни → цена в копейках».

    Ключи бывают строковыми: period_prices хранится в JSON-колонке. Мусор
    пропускаем молча — из-за одной кривой записи экран пополнения не должен
    падать, он и без готовых сумм работоспособен.
    """
    if not isinstance(period_prices, dict):
        return []

    by_amount: dict[int, int] = {}
    for raw_days, raw_price in period_prices.items():
        try:
            days = int(raw_days)
            price = int(raw_price)
        except (TypeError, ValueError):
            continue
        if isinstance(raw_price, bool) or price <= 0 or days <= 0:
            continue
        # Одинаковые цены схлопываем, оставляя короткий период: две кнопки с
        # одной суммой выглядят как ошибка интерфейса.
        if price not in by_amount or days < by_amount[price]:
            by_amount[price] = days

    presets = [
        {'amount_kopeks': amount, 'label_days': days}
        for amount, days in sorted(by_amount.items(), key=lambda item: item[0])
    ]
    return presets[:_MAX_PRESETS]


@router.get('/topup-presets')
async def get_topup_presets(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Готовые суммы пополнения для текущего пользователя."""
    sales_mode = settings.get_sales_mode()

    period_prices: dict[Any, Any] = {}
    if sales_mode == 'tariffs':
        from app.database.crud.tariff import get_tariffs_for_user

        try:
            # get_primary_promo_group() покрывает и legacy FK promo_group, и
            # новую M2M-таблицу user_promo_groups — как в purchase.py
            # (get_purchase_options). getattr(user, 'promo_group_id', None)
            # видит только legacy-колонку и тихо теряет тарифы для тех, у
            # кого промогруппа назначена через M2M.
            promo_group = user.get_primary_promo_group() if hasattr(user, 'get_primary_promo_group') else None
            if promo_group is None:
                promo_group = getattr(user, 'promo_group', None)
            promo_group_id = promo_group.id if promo_group else None
            tariffs = await get_tariffs_for_user(db, promo_group_id)
        except Exception as tariff_error:
            logger.warning(
                'Failed to load tariffs for topup presets',
                user_id=user.id,
                error=str(tariff_error)[:200],
            )
            tariffs = []
        for tariff in tariffs:
            prices = getattr(tariff, 'period_prices', None)
            if isinstance(prices, dict):
                for days, price in prices.items():
                    period_prices.setdefault(days, price)
    else:
        period_prices = dict(getattr(settings, 'CLASSIC_PERIOD_PRICES', {}) or {})

    return {'presets': build_presets(period_prices), 'sales_mode': sales_mode}
