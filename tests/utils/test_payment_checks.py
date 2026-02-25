"""Тесты для утилит проверки платёжных операций."""

import pytest

from app.utils.payment_checks import TopupRestrictionResult, check_topup_restriction, validate_payment_amount


class MockUser:
    """Мок пользователя для тестирования."""

    def __init__(self, restriction_topup: bool = False, restriction_reason: str | None = None):
        self.restriction_topup = restriction_topup
        self.restriction_reason = restriction_reason
        self.language = 'ru'


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
def mock_user() -> MockUser:
    """Мок пользователя без ограничений."""
    return MockUser(restriction_topup=False)


@pytest.fixture
def restricted_user() -> MockUser:
    """Мок пользователя с ограничением на пополнение."""
    return MockUser(restriction_topup=True, restriction_reason='Подозрительная активность')


def test_check_topup_restriction_no_restriction(mock_user: MockUser, mock_settings: MockSettings) -> None:
    """Проверка что пользователь без ограничений может пополнять баланс."""
    result = check_topup_restriction(mock_user)

    assert isinstance(result, TopupRestrictionResult)
    assert result.is_restricted is False
    assert result.message is None
    assert result.keyboard is None


def test_check_topup_restriction_with_restriction(restricted_user: MockUser, mock_settings: MockSettings) -> None:
    """Проверка что пользователь с ограничением не может пополнять баланс."""
    result = check_topup_restriction(restricted_user)

    assert isinstance(result, TopupRestrictionResult)
    assert result.is_restricted is True
    assert result.message is not None
    assert '🚫' in result.message
    assert 'Пополнение ограничено' in result.message
    assert 'Подозрительная активность' in result.message
    assert result.keyboard is not None
    assert len(result.keyboard.inline_keyboard) == 2  # Обжаловать + Назад


def test_check_topup_restriction_default_reason(mock_settings: MockSettings) -> None:
    """Проверка что используется дефолтная причина если не указана."""
    user = MockUser(restriction_topup=True, restriction_reason=None)
    result = check_topup_restriction(user)

    assert result.is_restricted is True
    assert result.message is not None
    assert 'Действие ограничено администратором' in result.message


def test_check_topup_restriction_no_support_url(restricted_user: MockUser, monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверка клавиатуры когда URL поддержки не настроен."""
    mock_settings_no_url = MockSettings()
    mock_settings_no_url.get_support_contact_url = lambda: None
    monkeypatch.setattr('app.utils.payment_checks.settings', mock_settings_no_url)

    result = check_topup_restriction(restricted_user)

    assert result.is_restricted is True
    assert result.keyboard is not None
    # Только кнопка "Назад" без кнопки "Обжаловать"
    assert len(result.keyboard.inline_keyboard) == 1


def test_validate_payment_amount_valid() -> None:
    """Проверка валидации корректной суммы."""
    error = validate_payment_amount(
        amount_kopeks=10000,  # 100₽
        min_amount_kopeks=5000,  # 50₽
        max_amount_kopeks=100000,  # 1000₽
    )

    assert error is None


def test_validate_payment_amount_too_low() -> None:
    """Проверка валидации слишком маленькой суммы."""
    error = validate_payment_amount(
        amount_kopeks=3000,  # 30₽
        min_amount_kopeks=5000,  # 50₽
        max_amount_kopeks=100000,  # 1000₽
    )

    assert error is not None
    assert '❌' in error
    assert '50' in error


def test_validate_payment_amount_too_high() -> None:
    """Проверка валидации слишком большой суммы."""
    error = validate_payment_amount(
        amount_kopeks=150000,  # 1500₽
        min_amount_kopeks=5000,  # 50₽
        max_amount_kopeks=100000,  # 1000₽
    )

    assert error is not None
    assert '❌' in error
    assert '1000' in error


def test_validate_payment_amount_edge_cases() -> None:
    """Проверка граничных значений."""
    # Минимальная допустимая сумма
    error_min = validate_payment_amount(
        amount_kopeks=5000,  # Ровно 50₽
        min_amount_kopeks=5000,
        max_amount_kopeks=100000,
    )
    assert error_min is None

    # Максимальная допустимая сумма
    error_max = validate_payment_amount(
        amount_kopeks=100000,  # Ровно 1000₽
        min_amount_kopeks=5000,
        max_amount_kopeks=100000,
    )
    assert error_max is None

    # На 1 копейку меньше минимума
    error_below = validate_payment_amount(
        amount_kopeks=4999,
        min_amount_kopeks=5000,
        max_amount_kopeks=100000,
    )
    assert error_below is not None

    # На 1 копейку больше максимума
    error_above = validate_payment_amount(
        amount_kopeks=100001,
        min_amount_kopeks=5000,
        max_amount_kopeks=100000,
    )
    assert error_above is not None


def test_validate_payment_amount_zero_and_negative() -> None:
    """Проверка нулевых и отрицательных значений."""
    # Ноль
    error_zero = validate_payment_amount(
        amount_kopeks=0,
        min_amount_kopeks=5000,
        max_amount_kopeks=100000,
    )
    assert error_zero is not None

    # Отрицательное значение
    error_neg = validate_payment_amount(
        amount_kopeks=-1000,
        min_amount_kopeks=5000,
        max_amount_kopeks=100000,
    )
    assert error_neg is not None
