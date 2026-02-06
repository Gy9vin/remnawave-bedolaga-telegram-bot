"""Unit-тесты для проверки совпадения расчёта цены между старым и новым кодом.

Эти тесты проверяют что новый сервис SubscriptionPurchaseService.calculate_pricing()
возвращает ТЕ ЖЕ цены что и старый код в utils/pricing_utils.py.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PERIOD_PRICES, settings

# Импортируем модуль для патчинга
from app.database.crud.subscription import calculate_subscription_total_cost
from app.database.models import PromoGroup, ServerSquad, User
from app.services.subscription_purchase_service import (
    MiniAppSubscriptionPurchaseService,
    PurchaseOptionsContext,
    PurchasePeriodConfig,
    PurchaseSelection,
)


logger = logging.getLogger(__name__)


# ============================================================================
# Фикстуры
# ============================================================================


@pytest.fixture
def mock_db() -> AsyncSession:
    """Мокированная сессия БД."""
    db = MagicMock(spec=AsyncSession)

    # Мокируем результат execute() для возврата результата с fetchall()
    mock_result = MagicMock()
    mock_result.fetchall = MagicMock(return_value=[])
    mock_result.scalar_one_or_none = MagicMock(return_value=None)
    mock_result.scalar = MagicMock(return_value=None)

    db.execute = AsyncMock(return_value=mock_result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


@pytest.fixture
def mock_user() -> User:
    """Мокированный пользователь без промогруппы."""
    user = MagicMock(spec=User)
    user.id = 1
    user.telegram_id = 123456789
    user.language = 'ru'
    user.balance_kopeks = 100000  # 1000₽
    user.promo_group = None
    user.get_primary_promo_group = MagicMock(return_value=None)
    user.get_promo_discount = MagicMock(return_value=0)  # Без скидок
    # Promo offer discount
    user.promo_offer_discount_percent = 0
    user.promo_offer_expires_at = None
    return user


@pytest.fixture
def mock_user_with_promo() -> User:
    """Мокированный пользователь с промогруппой (10% скидка)."""
    promo_group = MagicMock(spec=PromoGroup)
    promo_group.id = 1
    promo_group.name = 'VIP'
    promo_group.base_period_discount_percent = 10
    promo_group.additional_traffic_discount_percent = 10
    promo_group.additional_servers_discount_percent = 10
    promo_group.additional_devices_discount_percent = 10
    promo_group.get_discount_percent = MagicMock(return_value=10)  # 10% на всё

    user = MagicMock(spec=User)
    user.id = 2
    user.telegram_id = 987654321
    user.language = 'ru'
    user.balance_kopeks = 100000
    user.promo_group = promo_group
    user.get_primary_promo_group = MagicMock(return_value=promo_group)
    user.get_promo_discount = MagicMock(return_value=10)  # 10% скидка
    # Promo offer discount
    user.promo_offer_discount_percent = 0
    user.promo_offer_expires_at = None
    return user


@pytest.fixture
def mock_server_squad() -> ServerSquad:
    """Мокированный сервер (сквад)."""
    server = MagicMock(spec=ServerSquad)
    server.id = 1
    server.squad_uuid = 'test-squad-uuid-1'
    server.display_name = 'Test Server DE'
    server.country_code = 'DE'
    server.price_kopeks = 5000  # 50₽/мес
    server.is_available = True
    server.is_full = False
    return server


@pytest.fixture
def mock_server_squads(mock_server_squad: ServerSquad) -> list[ServerSquad]:
    """Список мокированных серверов."""
    server2 = MagicMock(spec=ServerSquad)
    server2.id = 2
    server2.squad_uuid = 'test-squad-uuid-2'
    server2.display_name = 'Test Server NL'
    server2.country_code = 'NL'
    server2.price_kopeks = 3000  # 30₽/мес
    server2.is_available = True
    server2.is_full = False
    return [mock_server_squad, server2]


# ============================================================================
# Вспомогательные функции
# ============================================================================


async def mock_get_server_ids_by_uuids(db: AsyncSession, uuids: list[str]) -> list[int]:
    """Мок для get_server_ids_by_uuids - возвращает ID серверов по UUID."""
    # Возвращаем ID согласно UUID
    ids = []
    for uuid in uuids:
        if uuid == 'test-squad-uuid-1':
            ids.append(1)
        elif uuid == 'test-squad-uuid-2':
            ids.append(2)
    return ids


async def mock_get_servers_monthly_prices(
    db: AsyncSession,
    server_ids: list[int],
    *,
    user: User | None = None,
) -> list[int]:
    """Мок для get_servers_monthly_prices - возвращает цены серверов."""
    # Заглушка: первый сервер 5000, второй 3000
    prices = []
    for server_id in server_ids:
        if server_id == 1:
            prices.append(5000)
        elif server_id == 2:
            prices.append(3000)
        else:
            prices.append(0)
    return prices


async def mock_get_server_squad_by_uuid(db: AsyncSession, uuid: str) -> ServerSquad | None:
    """Мок для get_server_squad_by_uuid."""
    if uuid == 'test-squad-uuid-1':
        server = MagicMock(spec=ServerSquad)
        server.id = 1
        server.squad_uuid = uuid
        server.display_name = 'Test Server DE'
        server.price_kopeks = 5000
        server.is_available = True
        server.is_full = False
        return server
    if uuid == 'test-squad-uuid-2':
        server = MagicMock(spec=ServerSquad)
        server.id = 2
        server.squad_uuid = uuid
        server.display_name = 'Test Server NL'
        server.price_kopeks = 3000
        server.is_available = True
        server.is_full = False
        return server
    return None


def create_purchase_context(
    user: User,
    period_config: 'PurchasePeriodConfig',
) -> PurchaseOptionsContext:
    """Создаёт контекст покупки для тестов."""
    return PurchaseOptionsContext(
        user=user,
        subscription=None,
        currency='RUB',
        balance_kopeks=user.balance_kopeks,
        periods=[period_config],
        default_period=period_config,
        period_map={period_config.id: period_config},
        server_uuid_to_id={},
        payload={},
    )


def create_period_config(period_days: int, user: User | None = None) -> PurchasePeriodConfig:
    """Создаёт конфигурацию периода для тестов."""
    base_price_original = PERIOD_PRICES.get(period_days, 0)

    # Расчёт скидки периода (как в calculate_subscription_total_cost)
    from app.database.crud.subscription import _get_discount_percent

    promo_group = user.promo_group if user else None
    period_discount_percent = _get_discount_percent(
        user,
        promo_group,
        'period',
        period_days=period_days,
    )

    base_discount_total = base_price_original * period_discount_percent // 100
    base_price = base_price_original - base_discount_total

    from app.utils.pricing_utils import calculate_months_from_days

    months = calculate_months_from_days(period_days)

    return PurchasePeriodConfig(
        id=f'period_{period_days}',
        days=period_days,
        months=months,
        label=f'{period_days} дней',
        base_price=base_price,
        base_price_label=f'{base_price / 100:.2f}₽',
        base_price_original=base_price_original,
        base_price_original_label=f'{base_price_original / 100:.2f}₽' if base_price_original != base_price else None,
        discount_percent=period_discount_percent,
        per_month_price=base_price // months if months > 0 else base_price,
        per_month_price_label=f'{base_price // months / 100:.2f}₽/мес' if months > 0 else '',
        traffic=MagicMock(),  # Не используется напрямую в calculate_pricing
        servers=MagicMock(),
        devices=MagicMock(),
    )


# ============================================================================
# Тесты
# ============================================================================


@pytest.mark.asyncio
async def test_price_match_basic_subscription(mock_db: AsyncSession, mock_user: User):
    """Тест: базовая подписка на 30 дней без доплат и скидок."""

    period_days = 30
    traffic_gb = 0  # Без доплаты за трафик
    devices = settings.DEFAULT_DEVICE_LIMIT  # Без доплаты за устройства
    server_uuids = []  # Без дополнительных серверов

    # ============ Старый код (calculate_subscription_total_cost) ============
    with patch(
        'app.database.crud.subscription.get_servers_monthly_prices', side_effect=mock_get_servers_monthly_prices
    ):
        old_total, old_details = await calculate_subscription_total_cost(
            mock_db,
            period_days=period_days,
            traffic_gb=traffic_gb,
            server_squad_ids=[],
            devices=devices,
            user=mock_user,
        )

    # ============ Новый код (сервис) ============
    service = MiniAppSubscriptionPurchaseService()

    period_config = create_period_config(period_days, user=mock_user)
    context = create_purchase_context(mock_user, period_config)

    selection = PurchaseSelection(
        period=period_config,
        traffic_value=traffic_gb,
        servers=server_uuids,
        devices=devices,
    )

    with (
        patch(
            'app.services.subscription_purchase_service.get_server_ids_by_uuids',
            side_effect=mock_get_server_ids_by_uuids,
        ),
        patch('app.database.crud.subscription.get_servers_monthly_prices', side_effect=mock_get_servers_monthly_prices),
        patch('app.services.subscription_purchase_service.get_user_active_promo_discount_percent', return_value=0),
    ):
        new_pricing = await service.calculate_pricing(mock_db, context, selection)

    # ============ Сравнение ============
    logger.info('=== test_price_match_basic_subscription ===')
    logger.info(f'Старый код: {old_total} коп.')
    logger.info(f'Новый код: {new_pricing.final_total} коп.')
    logger.info(f'Старый breakdown: {old_details}')
    logger.info(f'Новый details: {new_pricing.details}')

    # Финальная цена должна совпадать
    assert new_pricing.final_total == old_total, (
        f'Цены НЕ совпадают! Старый: {old_total / 100:.2f}₽, Новый: {new_pricing.final_total / 100:.2f}₽'
    )

    # Базовая цена должна совпадать
    assert new_pricing.details['base_price'] == old_details['base_price']


@pytest.mark.asyncio
async def test_price_match_with_promo_discount(
    mock_db: AsyncSession,
    mock_user_with_promo: User,
):
    """Тест: подписка с промогруппой (10% скидка на всё)."""

    period_days = 30
    traffic_gb = 0
    devices = settings.DEFAULT_DEVICE_LIMIT
    server_uuids = []

    # ============ Старый код ============
    with patch(
        'app.database.crud.subscription.get_servers_monthly_prices', side_effect=mock_get_servers_monthly_prices
    ):
        old_total, old_details = await calculate_subscription_total_cost(
            mock_db,
            period_days=period_days,
            traffic_gb=traffic_gb,
            server_squad_ids=[],
            devices=devices,
            user=mock_user_with_promo,
        )

    # ============ Новый код ============
    service = MiniAppSubscriptionPurchaseService()
    period_config = create_period_config(period_days, user=mock_user_with_promo)
    context = create_purchase_context(mock_user_with_promo, period_config)

    selection = PurchaseSelection(
        period=period_config,
        traffic_value=traffic_gb,
        servers=server_uuids,
        devices=devices,
    )

    with (
        patch(
            'app.services.subscription_purchase_service.get_server_ids_by_uuids',
            side_effect=mock_get_server_ids_by_uuids,
        ),
        patch('app.database.crud.subscription.get_servers_monthly_prices', side_effect=mock_get_servers_monthly_prices),
        patch('app.services.subscription_purchase_service.get_user_active_promo_discount_percent', return_value=0),
    ):
        new_pricing = await service.calculate_pricing(mock_db, context, selection)

    # ============ Сравнение ============
    logger.info('=== test_price_match_with_promo_discount ===')
    logger.info(f'Старый код: {old_total} коп. (скидка: {old_details.get("total_discount", 0)} коп.)')
    logger.info(f'Новый код: {new_pricing.final_total} коп.')

    assert new_pricing.final_total == old_total, (
        f'Цены с промогруппой НЕ совпадают! Старый: {old_total / 100:.2f}₽, Новый: {new_pricing.final_total / 100:.2f}₽'
    )


@pytest.mark.asyncio
async def test_price_match_with_traffic(mock_db: AsyncSession, mock_user: User):
    """Тест: подписка с выбранным трафиком (100 ГБ)."""

    period_days = 30
    traffic_gb = 100
    devices = settings.DEFAULT_DEVICE_LIMIT
    server_uuids = []

    # ============ Старый код ============
    with patch(
        'app.database.crud.subscription.get_servers_monthly_prices', side_effect=mock_get_servers_monthly_prices
    ):
        old_total, old_details = await calculate_subscription_total_cost(
            mock_db,
            period_days=period_days,
            traffic_gb=traffic_gb,
            server_squad_ids=[],
            devices=devices,
            user=mock_user,
        )

    # ============ Новый код ============
    service = MiniAppSubscriptionPurchaseService()
    period_config = create_period_config(period_days, user=mock_user)
    context = create_purchase_context(mock_user, period_config)

    selection = PurchaseSelection(
        period=period_config,
        traffic_value=traffic_gb,
        servers=server_uuids,
        devices=devices,
    )

    with (
        patch(
            'app.services.subscription_purchase_service.get_server_ids_by_uuids',
            side_effect=mock_get_server_ids_by_uuids,
        ),
        patch('app.database.crud.subscription.get_servers_monthly_prices', side_effect=mock_get_servers_monthly_prices),
        patch('app.services.subscription_purchase_service.get_user_active_promo_discount_percent', return_value=0),
    ):
        new_pricing = await service.calculate_pricing(mock_db, context, selection)

    # ============ Сравнение ============
    logger.info('=== test_price_match_with_traffic ===')
    logger.info(f'Старый код: {old_total} коп. (трафик: {old_details.get("traffic_price", 0)} коп.)')
    logger.info(
        f'Новый код: {new_pricing.final_total} коп. (трафик: {new_pricing.details.get("total_traffic_price", 0)} коп.)'
    )

    assert new_pricing.final_total == old_total, (
        f'Цены с трафиком НЕ совпадают! Старый: {old_total / 100:.2f}₽, Новый: {new_pricing.final_total / 100:.2f}₽'
    )


@pytest.mark.asyncio
async def test_price_match_with_countries(mock_db: AsyncSession, mock_user: User):
    """Тест: подписка с выбранными странами (1 сервер)."""

    period_days = 30
    traffic_gb = 0
    devices = settings.DEFAULT_DEVICE_LIMIT
    server_uuids = ['test-squad-uuid-1']

    # ============ Старый код ============
    with patch(
        'app.database.crud.subscription.get_servers_monthly_prices', side_effect=mock_get_servers_monthly_prices
    ):
        old_total, old_details = await calculate_subscription_total_cost(
            mock_db,
            period_days=period_days,
            traffic_gb=traffic_gb,
            server_squad_ids=[1],  # ID сервера
            devices=devices,
            user=mock_user,
        )

    # ============ Новый код ============
    service = MiniAppSubscriptionPurchaseService()
    period_config = create_period_config(period_days, user=mock_user)
    context = create_purchase_context(mock_user, period_config)

    selection = PurchaseSelection(
        period=period_config,
        traffic_value=traffic_gb,
        servers=server_uuids,
        devices=devices,
    )

    with (
        patch(
            'app.services.subscription_purchase_service.get_server_ids_by_uuids',
            side_effect=mock_get_server_ids_by_uuids,
        ),
        patch('app.database.crud.subscription.get_servers_monthly_prices', side_effect=mock_get_servers_monthly_prices),
        patch('app.services.subscription_purchase_service.get_user_active_promo_discount_percent', return_value=0),
    ):
        new_pricing = await service.calculate_pricing(mock_db, context, selection)

    # ============ Сравнение ============
    logger.info('=== test_price_match_with_countries ===')
    logger.info(f'Старый код: {old_total} коп. (серверы: {old_details.get("servers_final", 0)} коп.)')
    logger.info(
        f'Новый код: {new_pricing.final_total} коп. (серверы: {new_pricing.details.get("total_servers_price", 0)} коп.)'
    )

    assert new_pricing.final_total == old_total, (
        f'Цены с серверами НЕ совпадают! Старый: {old_total / 100:.2f}₽, Новый: {new_pricing.final_total / 100:.2f}₽'
    )


@pytest.mark.asyncio
async def test_price_match_with_devices(mock_db: AsyncSession, mock_user: User):
    """Тест: подписка с дополнительными устройствами."""

    period_days = 30
    traffic_gb = 0
    devices = settings.DEFAULT_DEVICE_LIMIT + 3  # +3 устройства
    server_uuids = []

    # ============ Старый код ============
    with patch(
        'app.database.crud.subscription.get_servers_monthly_prices', side_effect=mock_get_servers_monthly_prices
    ):
        old_total, old_details = await calculate_subscription_total_cost(
            mock_db,
            period_days=period_days,
            traffic_gb=traffic_gb,
            server_squad_ids=[],
            devices=devices,
            user=mock_user,
        )

    # ============ Новый код ============
    service = MiniAppSubscriptionPurchaseService()
    period_config = create_period_config(period_days, user=mock_user)
    context = create_purchase_context(mock_user, period_config)

    selection = PurchaseSelection(
        period=period_config,
        traffic_value=traffic_gb,
        servers=server_uuids,
        devices=devices,
    )

    with (
        patch(
            'app.services.subscription_purchase_service.get_server_ids_by_uuids',
            side_effect=mock_get_server_ids_by_uuids,
        ),
        patch('app.database.crud.subscription.get_servers_monthly_prices', side_effect=mock_get_servers_monthly_prices),
        patch('app.services.subscription_purchase_service.get_user_active_promo_discount_percent', return_value=0),
    ):
        new_pricing = await service.calculate_pricing(mock_db, context, selection)

    # ============ Сравнение ============
    logger.info('=== test_price_match_with_devices ===')
    logger.info(f'Старый код: {old_total} коп. (устройства: {old_details.get("devices_price", 0)} коп.)')
    logger.info(
        f'Новый код: {new_pricing.final_total} коп. (устройства: {new_pricing.details.get("total_devices_price", 0)} коп.)'
    )

    assert new_pricing.final_total == old_total, (
        f'Цены с устройствами НЕ совпадают! Старый: {old_total / 100:.2f}₽, Новый: {new_pricing.final_total / 100:.2f}₽'
    )


@pytest.mark.asyncio
async def test_price_match_full_options(
    mock_db: AsyncSession,
    mock_user_with_promo: User,
):
    """Тест: подписка со всеми опциями (период 90 дней, трафик, серверы, устройства, промогруппа)."""

    period_days = 90
    traffic_gb = 200
    devices = settings.DEFAULT_DEVICE_LIMIT + 5
    server_uuids = ['test-squad-uuid-1', 'test-squad-uuid-2']

    # ============ Старый код ============
    with patch(
        'app.database.crud.subscription.get_servers_monthly_prices', side_effect=mock_get_servers_monthly_prices
    ):
        old_total, old_details = await calculate_subscription_total_cost(
            mock_db,
            period_days=period_days,
            traffic_gb=traffic_gb,
            server_squad_ids=[1, 2],  # ID серверов
            devices=devices,
            user=mock_user_with_promo,
        )

    # ============ Новый код ============
    service = MiniAppSubscriptionPurchaseService()
    period_config = create_period_config(period_days, user=mock_user_with_promo)
    context = create_purchase_context(mock_user_with_promo, period_config)

    selection = PurchaseSelection(
        period=period_config,
        traffic_value=traffic_gb,
        servers=server_uuids,
        devices=devices,
    )

    with (
        patch(
            'app.services.subscription_purchase_service.get_server_ids_by_uuids',
            side_effect=mock_get_server_ids_by_uuids,
        ),
        patch('app.database.crud.subscription.get_servers_monthly_prices', side_effect=mock_get_servers_monthly_prices),
        patch('app.services.subscription_purchase_service.get_user_active_promo_discount_percent', return_value=0),
    ):
        new_pricing = await service.calculate_pricing(mock_db, context, selection)

    # ============ Сравнение ============
    logger.info('=== test_price_match_full_options ===')
    logger.info(f'Старый код: {old_total} коп.')
    logger.info(f'  - Базовая: {old_details.get("base_price", 0)} коп.')
    logger.info(f'  - Трафик: {old_details.get("traffic_price", 0) - old_details.get("traffic_discount", 0)} коп.')
    logger.info(f'  - Серверы: {old_details.get("servers_final", 0)} коп.')
    logger.info(f'  - Устройства: {old_details.get("devices_price", 0) - old_details.get("devices_discount", 0)} коп.')
    logger.info(f'  - Скидки: -{old_details.get("total_discount", 0)} коп.')

    logger.info(f'Новый код: {new_pricing.final_total} коп.')
    logger.info(f'  - Базовая: {new_pricing.details.get("base_price", 0)} коп.')
    logger.info(f'  - Трафик: {new_pricing.details.get("total_traffic_price", 0)} коп.')
    logger.info(f'  - Серверы: {new_pricing.details.get("total_servers_price", 0)} коп.')
    logger.info(f'  - Устройства: {new_pricing.details.get("total_devices_price", 0)} коп.')

    assert new_pricing.final_total == old_total, (
        f'Цены со всеми опциями НЕ совпадают! '
        f'Старый: {old_total / 100:.2f}₽, '
        f'Новый: {new_pricing.final_total / 100:.2f}₽'
    )


# ============================================================================
# Дополнительные проверки
# ============================================================================


@pytest.mark.asyncio
async def test_price_calculation_breakdown_structure(mock_db: AsyncSession, mock_user: User):
    """Тест: структура breakdown/details совпадает между старым и новым кодом."""

    period_days = 60
    traffic_gb = 50
    devices = settings.DEFAULT_DEVICE_LIMIT + 2
    server_uuids = ['test-squad-uuid-1']

    # Старый код
    with patch(
        'app.database.crud.subscription.get_servers_monthly_prices', side_effect=mock_get_servers_monthly_prices
    ):
        old_total, old_details = await calculate_subscription_total_cost(
            mock_db,
            period_days=period_days,
            traffic_gb=traffic_gb,
            server_squad_ids=[1],  # ID сервера
            devices=devices,
            user=mock_user,
        )

    # Новый код
    service = MiniAppSubscriptionPurchaseService()
    period_config = create_period_config(period_days, user=mock_user)
    context = create_purchase_context(mock_user, period_config)

    selection = PurchaseSelection(
        period=period_config,
        traffic_value=traffic_gb,
        servers=server_uuids,
        devices=devices,
    )

    with (
        patch(
            'app.services.subscription_purchase_service.get_server_ids_by_uuids',
            side_effect=mock_get_server_ids_by_uuids,
        ),
        patch('app.database.crud.subscription.get_servers_monthly_prices', side_effect=mock_get_servers_monthly_prices),
        patch('app.services.subscription_purchase_service.get_user_active_promo_discount_percent', return_value=0),
    ):
        new_pricing = await service.calculate_pricing(mock_db, context, selection)

    # Проверки структуры
    assert 'base_price' in old_details
    assert 'base_price' in new_pricing.details

    assert 'total_traffic_price' in old_details
    assert 'total_traffic_price' in new_pricing.details

    assert 'total_servers_price' in old_details
    assert 'total_servers_price' in new_pricing.details

    assert 'total_devices_price' in old_details
    assert 'total_devices_price' in new_pricing.details

    logger.info('Структура breakdown/details совпадает между старым и новым кодом.')
