"""Тесты для функции validate_user_can_purchase из subscription_purchase_service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.subscription_purchase_service import (
    PurchaseValidationResult,
    validate_user_can_purchase,
)


def create_mock_user(
    telegram_id: int = 123456789,
    username: str | None = 'testuser',
    restriction_subscription: bool = False,
    restriction_reason: str | None = None,
):
    """Создать мок объекта User для тестов."""
    user = MagicMock()
    user.telegram_id = telegram_id
    user.username = username
    user.restriction_subscription = restriction_subscription
    user.restriction_reason = restriction_reason
    user.id = 1
    return user


@pytest.mark.anyio
async def test_validate_user_can_purchase_allowed():
    """Тест: пользователь без ограничений может совершить покупку."""
    user = create_mock_user(
        telegram_id=123456789,
        username='testuser',
        restriction_subscription=False,
    )

    # Мокаем BlacklistService
    with patch('app.services.blacklist_service.blacklist_service') as mock_blacklist:
        mock_blacklist.is_user_blacklisted = AsyncMock(return_value=(False, None))

        result = await validate_user_can_purchase(user)

        # Проверяем, что вызван метод проверки черного списка
        mock_blacklist.is_user_blacklisted.assert_called_once_with(123456789, 'testuser')

        # Проверяем результат
        assert isinstance(result, PurchaseValidationResult)
        assert result.can_purchase is True
        assert result.error_message is None
        assert result.error_code is None


@pytest.mark.anyio
async def test_validate_user_blacklisted():
    """Тест: пользователь в черном списке не может совершить покупку."""
    user = create_mock_user(
        telegram_id=987654321,
        username='blacklisteduser',
        restriction_subscription=False,
    )

    blacklist_reason = 'Нарушение правил использования'

    # Мокаем BlacklistService - пользователь в blacklist
    with patch('app.services.blacklist_service.blacklist_service') as mock_blacklist:
        mock_blacklist.is_user_blacklisted = AsyncMock(return_value=(True, blacklist_reason))

        result = await validate_user_can_purchase(user)

        # Проверяем, что вызван метод проверки черного списка
        mock_blacklist.is_user_blacklisted.assert_called_once_with(987654321, 'blacklisteduser')

        # Проверяем результат
        assert isinstance(result, PurchaseValidationResult)
        assert result.can_purchase is False
        assert result.error_message == blacklist_reason
        assert result.error_code == 'blacklisted'


@pytest.mark.anyio
async def test_validate_user_blacklisted_without_reason():
    """Тест: пользователь в черном списке без указания причины."""
    user = create_mock_user(
        telegram_id=111222333,
        username='testuser2',
        restriction_subscription=False,
    )

    # Мокаем BlacklistService - пользователь в blacklist без причины
    with patch('app.services.blacklist_service.blacklist_service') as mock_blacklist:
        mock_blacklist.is_user_blacklisted = AsyncMock(return_value=(True, None))

        result = await validate_user_can_purchase(user)

        # Проверяем результат - должно быть дефолтное сообщение
        assert isinstance(result, PurchaseValidationResult)
        assert result.can_purchase is False
        assert result.error_message == 'Пользователь находится в черном списке'
        assert result.error_code == 'blacklisted'


@pytest.mark.anyio
async def test_validate_user_restricted():
    """Тест: пользователь с ограничением restriction_subscription не может совершить покупку."""
    restriction_reason = 'Временная блокировка по решению администратора'
    user = create_mock_user(
        telegram_id=555666777,
        username='restricteduser',
        restriction_subscription=True,
        restriction_reason=restriction_reason,
    )

    # Мокаем BlacklistService - пользователь НЕ в blacklist
    with patch('app.services.blacklist_service.blacklist_service') as mock_blacklist:
        mock_blacklist.is_user_blacklisted = AsyncMock(return_value=(False, None))

        result = await validate_user_can_purchase(user)

        # Проверяем результат
        assert isinstance(result, PurchaseValidationResult)
        assert result.can_purchase is False
        assert result.error_message == restriction_reason
        assert result.error_code == 'restricted'


@pytest.mark.anyio
async def test_validate_user_restricted_without_reason():
    """Тест: пользователь с ограничением без указания причины."""
    user = create_mock_user(
        telegram_id=888999000,
        username='restricteduser2',
        restriction_subscription=True,
        restriction_reason=None,
    )

    # Мокаем BlacklistService
    with patch('app.services.blacklist_service.blacklist_service') as mock_blacklist:
        mock_blacklist.is_user_blacklisted = AsyncMock(return_value=(False, None))

        result = await validate_user_can_purchase(user)

        # Проверяем результат - должна быть дефолтная причина
        assert isinstance(result, PurchaseValidationResult)
        assert result.can_purchase is False
        assert result.error_message == 'Действие ограничено администратором'
        assert result.error_code == 'restricted'


@pytest.mark.anyio
async def test_validate_user_without_telegram_id():
    """Тест: пользователь без telegram_id (email-only) - пропускаем проверку blacklist."""
    user = create_mock_user(
        telegram_id=None,  # Email-only user
        username=None,
        restriction_subscription=False,
    )

    # Мокаем BlacklistService - не должен вызываться
    with patch('app.services.blacklist_service.blacklist_service') as mock_blacklist:
        mock_blacklist.is_user_blacklisted = AsyncMock(return_value=(False, None))

        result = await validate_user_can_purchase(user)

        # Проверяем, что blacklist НЕ проверялся (нет telegram_id)
        mock_blacklist.is_user_blacklisted.assert_not_called()

        # Проверяем результат - разрешена покупка
        assert isinstance(result, PurchaseValidationResult)
        assert result.can_purchase is True
        assert result.error_message is None
        assert result.error_code is None


@pytest.mark.anyio
async def test_validate_user_explicit_telegram_id():
    """Тест: передача explicit telegram_id и username вместо извлечения из user."""
    user = create_mock_user(
        telegram_id=111111111,
        username='originaluser',
        restriction_subscription=False,
    )

    explicit_telegram_id = 222222222
    explicit_username = 'explicituser'

    # Мокаем BlacklistService
    with patch('app.services.blacklist_service.blacklist_service') as mock_blacklist:
        mock_blacklist.is_user_blacklisted = AsyncMock(return_value=(False, None))

        result = await validate_user_can_purchase(
            user,
            telegram_id=explicit_telegram_id,
            username=explicit_username,
        )

        # Проверяем, что использовались явные параметры, а не из user
        mock_blacklist.is_user_blacklisted.assert_called_once_with(
            explicit_telegram_id,
            explicit_username,
        )

        # Проверяем результат
        assert isinstance(result, PurchaseValidationResult)
        assert result.can_purchase is True


@pytest.mark.anyio
async def test_validate_user_blacklist_takes_priority_over_restriction():
    """Тест: проверка blacklist выполняется раньше, чем проверка restriction."""
    user = create_mock_user(
        telegram_id=333444555,
        username='bothrestricteduser',
        restriction_subscription=True,  # Есть restriction
        restriction_reason='Restriction reason',
    )

    blacklist_reason = 'Blacklist reason'

    # Мокаем BlacklistService - пользователь в blacklist
    with patch('app.services.blacklist_service.blacklist_service') as mock_blacklist:
        mock_blacklist.is_user_blacklisted = AsyncMock(return_value=(True, blacklist_reason))

        result = await validate_user_can_purchase(user)

        # Проверяем, что вернулась ошибка blacklist, а не restriction
        assert isinstance(result, PurchaseValidationResult)
        assert result.can_purchase is False
        assert result.error_message == blacklist_reason
        assert result.error_code == 'blacklisted'


@pytest.mark.anyio
async def test_validate_user_with_empty_username():
    """Тест: пользователь без username (username=None)."""
    user = create_mock_user(
        telegram_id=666777888,
        username=None,  # Пользователь без username
        restriction_subscription=False,
    )

    # Мокаем BlacklistService
    with patch('app.services.blacklist_service.blacklist_service') as mock_blacklist:
        mock_blacklist.is_user_blacklisted = AsyncMock(return_value=(False, None))

        result = await validate_user_can_purchase(user)

        # Проверяем, что вызван метод с None в username
        mock_blacklist.is_user_blacklisted.assert_called_once_with(666777888, None)

        # Проверяем результат
        assert isinstance(result, PurchaseValidationResult)
        assert result.can_purchase is True
        assert result.error_message is None
        assert result.error_code is None
