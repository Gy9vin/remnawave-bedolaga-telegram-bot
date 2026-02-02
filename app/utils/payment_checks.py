"""Утилиты для проверки ограничений платежей.

Модуль содержит функции для проверки ограничений на пополнение баланса
и валидации сумм платежей. Используется для унификации логики проверок
в 26+ местах кодовой базы.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram import types

from app.config import settings
from app.localization.texts import get_texts


if TYPE_CHECKING:
    from app.database.models import User


@dataclass
class TopupRestrictionResult:
    """Результат проверки ограничения на пополнение.

    Attributes:
        is_restricted: True если пополнение заблокировано.
        message: Сообщение для пользователя (None если не заблокировано).
        keyboard: Клавиатура с кнопками (None если не заблокировано).
    """

    is_restricted: bool
    message: str | None = None
    keyboard: types.InlineKeyboardMarkup | None = None


def check_topup_restriction(
    db_user: User,
    back_callback_data: str = 'menu_balance',
) -> TopupRestrictionResult:
    """Проверяет ограничение пользователя на пополнение баланса.

    Проверяет флаг restriction_topup у пользователя и формирует
    локализованное сообщение с клавиатурой, если ограничение активно.

    Args:
        db_user: Пользователь из базы данных.
        back_callback_data: Callback data для кнопки "Назад".
            По умолчанию 'menu_balance'.

    Returns:
        TopupRestrictionResult с полями:
            - is_restricted: True если пополнение заблокировано
            - message: HTML-сообщение для пользователя
            - keyboard: InlineKeyboardMarkup с кнопками

    Example:
        result = check_topup_restriction(db_user)
        if result.is_restricted:
            await callback.message.edit_text(
                result.message,
                reply_markup=result.keyboard,
            )
            await callback.answer()
            return
    """
    if not getattr(db_user, 'restriction_topup', False):
        return TopupRestrictionResult(is_restricted=False)

    texts = get_texts(db_user.language)

    # Причина ограничения
    default_reason = texts.t(
        'USER_RESTRICTION_DEFAULT_REASON',
        'Действие ограничено администратором',
    )
    reason = getattr(db_user, 'restriction_reason', None) or default_reason

    # Формируем сообщение
    message = texts.t(
        'USER_RESTRICTION_TOPUP_BLOCKED',
        '🚫 <b>Пополнение ограничено</b>\n\n{reason}\n\nЕсли вы считаете это ошибкой, вы можете обжаловать решение.',
    ).format(reason=reason)

    # Формируем клавиатуру
    keyboard_rows: list[list[types.InlineKeyboardButton]] = []

    support_url = settings.get_support_contact_url()
    if support_url:
        appeal_text = texts.t('USER_RESTRICTION_APPEAL_BUTTON', '🆘 Обжаловать')
        keyboard_rows.append([types.InlineKeyboardButton(text=appeal_text, url=support_url)])

    back_text = texts.t('BACK', '⬅️ Назад')
    keyboard_rows.append([types.InlineKeyboardButton(text=back_text, callback_data=back_callback_data)])

    keyboard = types.InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    return TopupRestrictionResult(
        is_restricted=True,
        message=message,
        keyboard=keyboard,
    )


def validate_payment_amount(
    amount_kopeks: int,
    min_amount_kopeks: int,
    max_amount_kopeks: int,
    language: str = 'ru',
) -> str | None:
    """Проверяет корректность суммы платежа.

    Валидирует сумму на соответствие минимальному и максимальному
    ограничениям с локализованными сообщениями об ошибках.

    Args:
        amount_kopeks: Сумма платежа в копейках.
        min_amount_kopeks: Минимальная допустимая сумма в копейках.
        max_amount_kopeks: Максимальная допустимая сумма в копейках.
        language: Код языка пользователя для локализации.

    Returns:
        Сообщение об ошибке (str) если сумма невалидна,
        None если сумма корректна.

    Example:
        error = validate_payment_amount(
            amount_kopeks=5000,
            min_amount_kopeks=10000,
            max_amount_kopeks=10000000,
            language=db_user.language,
        )
        if error:
            await message.answer(error)
            return
    """
    texts = get_texts(language)

    if amount_kopeks < min_amount_kopeks:
        min_formatted = texts.format_price(min_amount_kopeks)
        return texts.t(
            'PAYMENT_AMOUNT_TOO_LOW',
            f'❌ Минимальная сумма пополнения: {min_formatted}',
        ).format(min_amount=min_formatted)

    if amount_kopeks > max_amount_kopeks:
        max_formatted = texts.format_price(max_amount_kopeks)
        return texts.t(
            'PAYMENT_AMOUNT_TOO_HIGH',
            f'❌ Максимальная сумма пополнения: {max_formatted}',
        ).format(max_amount=max_formatted)

    return None


def validate_payment_amount_rubles(
    amount_rubles: float,
    min_amount_kopeks: int,
    max_amount_kopeks: int,
    language: str = 'ru',
) -> tuple[int | None, str | None]:
    """Проверяет и конвертирует сумму платежа из рублей.

    Конвертирует сумму из рублей в копейки и валидирует
    на соответствие ограничениям.

    Args:
        amount_rubles: Сумма платежа в рублях.
        min_amount_kopeks: Минимальная допустимая сумма в копейках.
        max_amount_kopeks: Максимальная допустимая сумма в копейках.
        language: Код языка пользователя для локализации.

    Returns:
        Кортеж (amount_kopeks, error_message):
            - (int, None) если сумма валидна
            - (None, str) если есть ошибка

    Example:
        amount_kopeks, error = validate_payment_amount_rubles(
            amount_rubles=100.50,
            min_amount_kopeks=10000,
            max_amount_kopeks=10000000,
            language=db_user.language,
        )
        if error:
            await message.answer(error)
            return
        # Используем amount_kopeks
    """
    amount_kopeks = int(amount_rubles * 100)

    error = validate_payment_amount(
        amount_kopeks=amount_kopeks,
        min_amount_kopeks=min_amount_kopeks,
        max_amount_kopeks=max_amount_kopeks,
        language=language,
    )

    if error:
        return None, error

    return amount_kopeks, None
