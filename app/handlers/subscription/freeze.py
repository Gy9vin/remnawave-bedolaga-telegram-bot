"""Хендлеры заморозки и разморозки подписки через Telegram-бот (спека §11)."""

import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.services.subscription_service import FreezeNotAllowedError, SubscriptionService
from app.utils.timezone import format_local_datetime

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _get_freeze_confirm_keyboard(language: str) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения заморозки."""
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.t('FREEZE_CONFIRM_YES', '✅ Заморозить'),
                    callback_data='subscription_freeze_confirm',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.t('FREEZE_CONFIRM_CANCEL', '❌ Отмена'),
                    callback_data='subscription_freeze_cancel',
                )
            ],
        ]
    )


def _freeze_error_message(texts, reason: str) -> str:
    """Локализованное сообщение об ошибке заморозки по коду причины."""
    if reason == 'already_frozen':
        return texts.t('FREEZE_ERROR_ALREADY_FROZEN', '❌ Подписка уже заморожена.')
    if reason == 'email_not_verified':
        cabinet_url = settings._normalized_cabinet_url()
        msg = texts.t(
            'FREEZE_ERROR_EMAIL_NOT_VERIFIED',
            '❌ Для заморозки необходимо привязать почту в кабинете.',
        )
        if cabinet_url:
            profile_url = f'{cabinet_url}/profile'
            msg += f'\n<a href="{profile_url}">Перейти в кабинет</a>'
        return msg
    if reason == 'too_few_days':
        return texts.t(
            'FREEZE_ERROR_TOO_FEW_DAYS',
            f'❌ Недостаточно дней для заморозки (нужно не менее {settings.FREEZE_MIN_DAYS_REMAINING} дней).',
        ).format(min_days=settings.FREEZE_MIN_DAYS_REMAINING)
    if reason == 'invalid_status':
        return texts.t('FREEZE_ERROR_INVALID_STATUS', '❌ Заморозка недоступна в текущем статусе подписки.')
    if reason == 'trial_not_allowed':
        return texts.t('FREEZE_ERROR_TRIAL_NOT_ALLOWED', '❌ Заморозка недоступна для пробной подписки.')
    if reason == 'daily_paused':
        return texts.t('FREEZE_ERROR_DAILY_PAUSED', '❌ Отмените паузу суточной подписки перед заморозкой.')
    if reason == 'in_grace':
        return texts.t('FREEZE_ERROR_IN_GRACE', '❌ Заморозка недоступна в период дополнительного времени.')
    if reason == 'freeze_disabled':
        return texts.t('FREEZE_ERROR_DISABLED', '❌ Функция заморозки временно недоступна.')
    return texts.t('FREEZE_ERROR_UNKNOWN', f'❌ Ошибка заморозки: {reason}').format(reason=reason)


# ---------------------------------------------------------------------------
# Хендлеры
# ---------------------------------------------------------------------------


async def handle_freeze_request(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    """Шаг 1: показать confirm-диалог заморозки."""
    from aiogram.types import InaccessibleMessage

    texts = get_texts(db_user.language)

    if not settings.FREEZE_SUBSCRIPTIONS_ENABLED:
        await callback.answer(texts.t('FREEZE_ERROR_DISABLED', '❌ Функция заморозки временно недоступна.'), show_alert=True)
        return

    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    # В multi-tariff режиме: для простоты берём subscription из db_user
    # (в single-tariff mode это db_user.subscription)
    await db.refresh(db_user)
    subscription = db_user.subscription

    if not subscription:
        await callback.answer(texts.t('NO_SUBSCRIPTION_ERROR', '❌ У вас нет активной подписки'), show_alert=True)
        return

    days_left = getattr(subscription, 'days_left', 0)
    max_days = settings.FREEZE_MAX_DAYS

    confirm_text = texts.t(
        'FREEZE_CONFIRM_TEXT',
        (
            '❄️ <b>Заморозка подписки</b>\n\n'
            'Пока подписка заморожена, VPN не работает. '
            'Дни сохраняются и возвращаются при разморозке.\n\n'
            '⏱ Авто-разморозка через <b>{max_days} дней</b>.\n'
            '📅 Сохраняется дней: <b>{days_left}</b>\n\n'
            'Разморозить можно здесь или в кабинете по почте.\n\n'
            'Подтвердить заморозку?'
        ),
    ).format(max_days=max_days, days_left=days_left)

    try:
        await callback.message.edit_text(
            confirm_text,
            reply_markup=_get_freeze_confirm_keyboard(db_user.language),
            parse_mode='HTML',
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


async def handle_freeze_confirm(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    """Шаг 2: выполнить заморозку после подтверждения."""
    from aiogram.types import InaccessibleMessage

    texts = get_texts(db_user.language)

    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    await db.refresh(db_user)
    subscription = db_user.subscription

    if not subscription:
        await callback.answer(texts.t('NO_SUBSCRIPTION_ERROR', '❌ У вас нет активной подписки'), show_alert=True)
        return

    subscription_service = SubscriptionService()
    try:
        await subscription_service.freeze_subscription(user=db_user, subscription=subscription, db=db)
        await db.commit()
        await db.refresh(subscription)
    except FreezeNotAllowedError as e:
        error_msg = _freeze_error_message(texts, e.reason)
        try:
            await callback.message.edit_text(
                error_msg,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=texts.t('BACK_TO_SUBSCRIPTION', '← Назад к подписке'),
                                callback_data='subscription_info',
                            )
                        ]
                    ]
                ),
            )
        except TelegramBadRequest:
            await callback.answer(error_msg[:200], show_alert=True)
        await callback.answer()
        return
    except Exception as e:
        logger.error('Ошибка заморозки подписки', error=str(e), user_id=db_user.id)
        await callback.answer(texts.t('FREEZE_ERROR_UNEXPECTED', '❌ Произошла ошибка. Попробуйте позже.'), show_alert=True)
        return

    # Формируем сообщение об успехе
    frozen_days = getattr(subscription, 'frozen_days_banked', 0) or 0
    auto_unfreeze = getattr(subscription, 'frozen_auto_unfreeze_at', None)
    auto_unfreeze_str = format_local_datetime(auto_unfreeze, '%d.%m.%Y') if auto_unfreeze else '—'

    success_text = texts.t(
        'FREEZE_SUCCESS_TEXT',
        (
            '❄️ <b>Подписка заморожена!</b>\n\n'
            '💾 Сохранено дней: <b>{days}</b>\n'
            '📅 Авто-разморозка: <b>{auto_date}</b>\n\n'
            'Разморозить можно здесь или в личном кабинете по почте.'
        ),
    ).format(days=frozen_days, auto_date=auto_unfreeze_str)

    # Клавиатура после заморозки: кнопка "Разморозить" + "Назад"
    texts_obj = get_texts(db_user.language)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts_obj.t('UNFREEZE_BUTTON', '▶️ Разморозить подписку'),
                    callback_data='subscription_unfreeze',
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts_obj.t('BACK_TO_MENU', '← Главное меню'),
                    callback_data='back_to_menu',
                )
            ],
        ]
    )

    try:
        await callback.message.edit_text(success_text, reply_markup=keyboard, parse_mode='HTML')
    except TelegramBadRequest:
        pass
    await callback.answer()


async def handle_freeze_cancel(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    """Отмена заморозки — возврат к экрану подписки."""
    from app.handlers.subscription.purchase import show_subscription_info

    await callback.answer(get_texts(db_user.language).t('FREEZE_CANCELLED', 'Отменено.'))
    await show_subscription_info(callback, db_user, db)


async def handle_unfreeze(callback: types.CallbackQuery, db_user: User, db: AsyncSession) -> None:
    """Немедленная разморозка без confirm."""
    from aiogram.types import InaccessibleMessage

    texts = get_texts(db_user.language)

    if isinstance(callback.message, InaccessibleMessage):
        await callback.answer()
        return

    await db.refresh(db_user)
    subscription = db_user.subscription

    if not subscription:
        await callback.answer(texts.t('NO_SUBSCRIPTION_ERROR', '❌ У вас нет активной подписки'), show_alert=True)
        return

    if not getattr(subscription, 'is_frozen', False):
        await callback.answer(texts.t('UNFREEZE_NOT_FROZEN', '❌ Подписка не заморожена.'), show_alert=True)
        # Обновляем экран на случай рассинхронизации
        from app.handlers.subscription.purchase import show_subscription_info
        await show_subscription_info(callback, db_user, db)
        return

    subscription_service = SubscriptionService()
    try:
        await subscription_service.unfreeze_subscription(user=db_user, subscription=subscription, db=db, reason='manual')
        await db.commit()
        await db.refresh(subscription)
    except Exception as e:
        logger.error('Ошибка разморозки подписки', error=str(e), user_id=db_user.id)
        await callback.answer(texts.t('UNFREEZE_ERROR_UNEXPECTED', '❌ Произошла ошибка. Попробуйте позже.'), show_alert=True)
        return

    new_end_date = getattr(subscription, 'end_date', None)
    new_end_str = format_local_datetime(new_end_date, '%d.%m.%Y') if new_end_date else '—'

    success_text = texts.t(
        'UNFREEZE_SUCCESS_TEXT',
        (
            '▶️ <b>Подписка разморожена!</b>\n\n'
            '📅 Продлена до: <b>{new_date}</b>'
        ),
    ).format(new_date=new_end_str)

    try:
        await callback.message.edit_text(
            success_text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=get_texts(db_user.language).t('BACK_TO_SUBSCRIPTION', '← К подписке'),
                            callback_data='subscription_info',
                        )
                    ]
                ]
            ),
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


# ---------------------------------------------------------------------------
# Регистрация хендлеров
# ---------------------------------------------------------------------------


def register_handlers(dp: Dispatcher) -> None:
    """Зарегистрировать freeze/unfreeze хендлеры в диспетчере."""
    dp.callback_query.register(handle_freeze_request, F.data == 'subscription_freeze_request')
    dp.callback_query.register(handle_freeze_confirm, F.data == 'subscription_freeze_confirm')
    dp.callback_query.register(handle_freeze_cancel, F.data == 'subscription_freeze_cancel')
    dp.callback_query.register(handle_unfreeze, F.data == 'subscription_unfreeze')
