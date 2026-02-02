"""Тесты для утилит проверки платёжных операций."""

import pytest

from app.utils.payment_checks import check_topup_restriction, validate_payment_amount


class MockUser:
    """Мок пользователя для тестирования."""

    def __init__(self, restriction_topup: bool = False, restriction_reason: str | None = None):
        self.restriction_topup = restriction_topup
        self.restriction_reason = restriction_reason


class MockTexts:
    """Мок менеджера локализации."""

    def __init__(self):
        self.BACK = '« Назад'

    def t(self, key: str, default: str) -> str:
        """Возвращает дефолтное значение."""
        return default

    def format_price(self, amount_kopeks: int) -> str:
        """Форматирует цену."""
        rubles = amount_kopeks / 100
        return f'{rubles:.0f}₽'


class MockSettings:
    """Мок настроек."""

    @staticmethod
    def get_support_contact_url() -> str | None:
        """Возвращает URL поддержки."""
        return 'https://t.me/support'


@pytest.fixture
def mock_settings(monkeypatch: pytest.MonkeyPatch) -> MockSettings:
    """Мок настроек приложения."""
    mock_settings_obj = MockSettings()
    monkeypatch.setattr('app.utils.payment_checks.settings', mock_settings_obj)
    return mock_settings_obj


@pytest.fixture
def mock_texts() -> MockTexts:
    """Мок менеджера локализации."""
    return MockTexts()


@pytest.fixture
def mock_user() -> MockUser:
    """Мок пользователя без ограничений."""
    return MockUser(restriction_topup=False)


@pytest.fixture
def restricted_user() -> MockUser:
    """Мок пользователя с ограничением на пополнение."""
    return MockUser(restriction_topup=True, restriction_reason='Подозрительная активность')


@pytest.mark.asyncio
async def test_check_topup_restriction_no_restriction(
    mock_user: MockUser, mock_texts: MockTexts, mock_settings: MockSettings
) -> None:
    """Проверка что пользователь без ограничений может пополнять баланс."""
    is_restricted, message, keyboard = await check_topup_restriction(mock_user, mock_texts)

    assert is_restricted is False
    assert message == ''
    assert len(keyboard.inline_keyboard) == 0


@pytest.mark.asyncio
async def test_check_topup_restriction_with_restriction(
    restricted_user: MockUser, mock_texts: MockTexts, mock_settings: MockSettings
) -> None:
    """Проверка что пользователь с ограничением не может пополнять баланс."""
    is_restricted, message, keyboard = await check_topup_restriction(restricted_user, mock_texts)

    assert is_restricted is True
    assert '🚫' in message
    assert 'Пополнение ограничено' in message
    assert 'Подозрительная активность' in message
    assert len(keyboard.inline_keyboard) == 2  # Обжаловать + Назад


@pytest.mark.asyncio
async def test_check_topup_restriction_default_reason(mock_texts: MockTexts, mock_settings: MockSettings) -> None:
    """Проверка что используется дефолтная причина если не указана."""
    user = MockUser(restriction_topup=True, restriction_reason=None)
    is_restricted, message, keyboard = await check_topup_restriction(user, mock_texts)

    assert is_restricted is True
    assert 'Действие ограничено администратором' in message


@pytest.mark.asyncio
async def test_check_topup_restriction_no_support_url(
    restricted_user: MockUser, mock_texts: MockTexts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверка клавиатуры когда URL поддержки не настроен."""
    # Переопределяем метод для возврата None
    mock_settings_no_url = MockSettings()
    mock_settings_no_url.get_support_contact_url = lambda: None
    monkeypatch.setattr('app.utils.payment_checks.settings', mock_settings_no_url)

    is_restricted, message, keyboard = await check_topup_restriction(restricted_user, mock_texts)

    assert is_restricted is True
    # Только кнопка "Назад" без кнопки "Обжаловать"
    assert len(keyboard.inline_keyboard) == 1
    assert keyboard.inline_keyboard[0][0].text == '« Назад'


def test_validate_payment_amount_valid(mock_texts: MockTexts) -> None:
    """Проверка валидации корректной суммы."""
    is_valid, error = validate_payment_amount(
        amount=10000,  # 100₽
        min_amount=5000,  # 50₽
        max_amount=100000,  # 1000₽
        texts=mock_texts,
    )

    assert is_valid is True
    assert error is None


def test_validate_payment_amount_too_low(mock_texts: MockTexts) -> None:
    """Проверка валидации слишком маленькой суммы."""
    is_valid, error = validate_payment_amount(
        amount=3000,  # 30₽
        min_amount=5000,  # 50₽
        max_amount=100000,  # 1000₽
        texts=mock_texts,
    )

    assert is_valid is False
    assert error is not None
    assert '❌' in error
    assert '50₽' in error


def test_validate_payment_amount_too_high(mock_texts: MockTexts) -> None:
    """Проверка валидации слишком большой суммы."""
    is_valid, error = validate_payment_amount(
        amount=150000,  # 1500₽
        min_amount=5000,  # 50₽
        max_amount=100000,  # 1000₽
        texts=mock_texts,
    )

    assert is_valid is False
    assert error is not None
    assert '❌' in error
    assert '1000₽' in error


def test_validate_payment_amount_edge_cases(mock_texts: MockTexts) -> None:
    """Проверка граничных значений."""
    # Минимальная допустимая сумма
    is_valid_min, error_min = validate_payment_amount(
        amount=5000,  # Ровно 50₽
        min_amount=5000,
        max_amount=100000,
        texts=mock_texts,
    )
    assert is_valid_min is True
    assert error_min is None

    # Максимальная допустимая сумма
    is_valid_max, error_max = validate_payment_amount(
        amount=100000,  # Ровно 1000₽
        min_amount=5000,
        max_amount=100000,
        texts=mock_texts,
    )
    assert is_valid_max is True
    assert error_max is None

    # На 1 копейку меньше минимума
    is_valid_below, error_below = validate_payment_amount(
        amount=4999,
        min_amount=5000,
        max_amount=100000,
        texts=mock_texts,
    )
    assert is_valid_below is False
    assert error_below is not None

    # На 1 копейку больше максимума
    is_valid_above, error_above = validate_payment_amount(
        amount=100001,
        min_amount=5000,
        max_amount=100000,
        texts=mock_texts,
    )
    assert is_valid_above is False
    assert error_above is not None


def test_validate_payment_amount_zero_and_negative(mock_texts: MockTexts) -> None:
    """Проверка нулевых и отрицательных значений."""
    # Ноль
    is_valid_zero, error_zero = validate_payment_amount(
        amount=0,
        min_amount=5000,
        max_amount=100000,
        texts=mock_texts,
    )
    assert is_valid_zero is False
    assert error_zero is not None

    # Отрицательное значение
    is_valid_neg, error_neg = validate_payment_amount(
        amount=-1000,
        min_amount=5000,
        max_amount=100000,
        texts=mock_texts,
    )
    assert is_valid_neg is False
    assert error_neg is not None
