"""Точка входа в боте: оплата подписки за другого человека.

Три шага: плательщик вводит идентификатор получателя (``recipient_lookup``),
видит карточку с ценами по периодам (``sponsored_payment_service.quote_for_recipient``)
и списывает деньги со своего баланса (``sponsored_payment_service.pay_from_balance``).

Карточка получателя намеренно скупая — только имя и цены периодов. Тариф,
дата окончания подписки и число устройств получателя не показываются: это
чужие данные, а плательщику для решения «оплатить или нет» нужна только цена.

Цена, показанная в карточке, и есть та цена, которая спишется: она приходит
из ``SponsoredQuote.options`` и кладётся в FSM-хранилище как есть, без
пересчёта на шаге оплаты (см. докстринг ``sponsored_payment_service`` — там
это принципиальное решение, а не оптимизация).
"""

from __future__ import annotations

import structlog
from aiogram import Bot, Dispatcher, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InaccessibleMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.subscription import get_subscription_by_user_id
from app.database.crud.user import get_user_by_id
from app.database.models import User
from app.keyboards.inline import get_back_keyboard, get_insufficient_balance_keyboard
from app.localization.texts import get_texts
from app.services.price_breakdown import format_price_lines
from app.services.recipient_lookup import RecipientIsPayerError, resolve_recipient
from app.services.sponsored_payment_service import (
    InsufficientBalanceError,
    SponsoredQuote,
    pay_from_balance,
    quote_for_recipient,
)
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


class SponsoredPaymentStates(StatesGroup):
    """Шаги сценария «оплатить подписку другому человеку»."""

    # Ждём от плательщика @ник / telegram id / email одним сообщением.
    waiting_for_recipient = State()
    # Получатель найден, карточка с периодами показана — ждём нажатия кнопки.
    waiting_for_period = State()


# Callback-данные объявлены здесь же (а не в общем реестре — его в проекте нет,
# соседние хендлеры тоже используют литералы прямо в register_handlers).
CALLBACK_START = 'sponsored_payment_start'
_CALLBACK_PERIOD_PREFIX = 'sponsored_pay_period:'

# Ключи данных FSM, под которыми лежит зафиксированная на шаге quote цена.
_DATA_RECIPIENT_ID = 'sponsored_recipient_id'
_DATA_SUBSCRIPTION_ID = 'sponsored_subscription_id'
_DATA_OPTIONS = 'sponsored_options'
# Расшифровка цены по периоду (period_days -> готовый HTML-текст) — считается
# один раз вместе с quote и просто показывается после выбора периода, без
# пересчёта (см. докстрин модуля про фиксацию цены).
_DATA_BREAKDOWN = 'sponsored_breakdown'


def _build_period_keyboard(quote: SponsoredQuote, texts) -> types.InlineKeyboardMarkup:
    """Кнопки периодов с ценой. Ничего, кроме периода и цены, в подписи нет —
    это и есть требование «без тарифа и даты окончания» на уровне клавиатуры."""
    buttons = [
        [
            types.InlineKeyboardButton(
                text=f'{period} дн. — {texts.format_price(price)}',
                callback_data=f'{_CALLBACK_PERIOD_PREFIX}{period}',
            )
        ]
        for period, price in quote.options
    ]
    buttons.append([types.InlineKeyboardButton(text=texts.BACK, callback_data='back_to_menu')])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


@error_handler
async def start_sponsored_payment(callback: types.CallbackQuery, db_user: User, state: FSMContext):
    """Кнопка «Оплатить другому» — просим ввести получателя одним сообщением."""
    texts = get_texts(db_user.language)

    prompt = texts.t(
        'SPONSORED_PAYMENT_ASK_RECIPIENT',
        '👤 Кому оплачиваем подписку?\n\n'
        'Пришлите одним сообщением что-то одно:\n'
        '• @ник получателя в Telegram\n'
        '• его Telegram ID\n'
        '• email, привязанный к его аккаунту\n\n'
        'Telegram ID (код для оплаты) получатель может посмотреть в своём '
        'балансе в боте и переслать вам.',
    )

    await state.set_state(SponsoredPaymentStates.waiting_for_recipient)

    if callback.message and not isinstance(callback.message, InaccessibleMessage):
        await callback.message.edit_text(prompt, reply_markup=get_back_keyboard(db_user.language))
    await callback.answer()


@error_handler
async def process_recipient_query(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    """Обрабатывает введённый идентификатор получателя."""
    texts = get_texts(db_user.language)
    query = (message.text or '').strip()

    try:
        recipient = await resolve_recipient(db, query, payer_id=db_user.id)
    except RecipientIsPayerError:
        # Это не «не найдено» — человек сам себя ищет, ему нужна другая подсказка.
        await message.answer(
            texts.t(
                'SPONSORED_PAYMENT_SELF',
                '❌ Это вы. Для своей подписки используйте обычное продление в разделе «Моя подписка».',
            ),
            reply_markup=get_back_keyboard(db_user.language),
        )
        await state.clear()
        return

    if recipient is None:
        # Остаёмся в том же состоянии — пользователь мог опечататься, ему
        # нужно позволить ввести данные заново без повторного нажатия кнопки.
        await message.answer(
            texts.t(
                'SPONSORED_PAYMENT_NOT_FOUND',
                '❌ Пользователь не найден. Проверьте данные и введите снова.',
            )
        )
        return

    quote = await quote_for_recipient(db, recipient)

    if quote.subscription_id is None or not quote.options:
        # У получателя нет подписки, которую можно продлить сейчас — платить
        # нечего, а подбор тарифа для чужого аккаунта это отдельный сценарий.
        await message.answer(
            texts.t(
                'SPONSORED_PAYMENT_NO_SUBSCRIPTION',
                '❌ У получателя сейчас нет подписки, которую можно продлить.',
            ),
            reply_markup=get_back_keyboard(db_user.language),
        )
        await state.clear()
        return

    # Расшифровка цены по каждому периоду — считается один раз здесь же (из уже
    # посчитанного quote.price_lines_by_period) и просто показывается после
    # выбора периода, без пересчёта.
    breakdown_by_period: dict[str, str] = {}
    for period, price in quote.options:
        price_lines = quote.price_lines_by_period.get(period)
        if price_lines:
            breakdown_by_period[str(period)] = format_price_lines(price_lines, price)

    # Цена фиксируется здесь и просто переносится в списание — см. докстринг
    # модуля и sponsored_payment_service.
    await state.update_data(
        **{
            _DATA_RECIPIENT_ID: quote.recipient_id,
            _DATA_SUBSCRIPTION_ID: quote.subscription_id,
            _DATA_OPTIONS: {str(period): price for period, price in quote.options},
            _DATA_BREAKDOWN: breakdown_by_period,
        }
    )
    await state.set_state(SponsoredPaymentStates.waiting_for_period)

    card_text = texts.t(
        'SPONSORED_PAYMENT_CARD',
        '💳 Оплата подписки для {name}\n\nВыберите период:',
    ).format(name=quote.recipient_display_name)
    await message.answer(card_text, reply_markup=_build_period_keyboard(quote, texts))


@error_handler
async def handle_period_selection(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Нажатие на период — списывает деньги с баланса плательщика."""
    texts = get_texts(db_user.language)
    data = await state.get_data()

    recipient_id = data.get(_DATA_RECIPIENT_ID)
    subscription_id = data.get(_DATA_SUBSCRIPTION_ID)
    options: dict = data.get(_DATA_OPTIONS) or {}

    period_raw = (callback.data or '').removeprefix(_CALLBACK_PERIOD_PREFIX)
    price_kopeks = options.get(period_raw)

    if recipient_id is None or subscription_id is None or price_kopeks is None:
        # Карточка устарела (перезапуск бота, TTL хранилища FSM и т.п.) —
        # безопаснее попросить начать заново, чем гадать с ценой.
        await callback.answer(
            texts.t('SPONSORED_PAYMENT_EXPIRED', '❌ Сессия оплаты устарела, начните заново'),
            show_alert=True,
        )
        return

    period_days = int(period_raw)

    recipient = await get_user_by_id(db, recipient_id)
    if recipient is None:
        await callback.answer(texts.t('SPONSORED_PAYMENT_NOT_FOUND', '❌ Пользователь не найден'), show_alert=True)
        await state.clear()
        return

    subscription = await get_subscription_by_user_id(db, recipient.id)
    if subscription is None or subscription.id != subscription_id:
        # Подписка получателя успела измениться между показом карточки и
        # нажатием кнопки — зафиксированная цена больше не гарантированно
        # соответствует тому, что мы продлеваем.
        await callback.answer(
            texts.t('SPONSORED_PAYMENT_EXPIRED', '❌ Сессия оплаты устарела, начните заново'),
            show_alert=True,
        )
        await state.clear()
        return

    try:
        await pay_from_balance(
            db,
            payer=db_user,
            recipient=recipient,
            subscription=subscription,
            period_days=period_days,
            price_kopeks=price_kopeks,
        )
    except InsufficientBalanceError:
        missing_kopeks = price_kopeks - (db_user.balance_kopeks or 0)
        text = texts.t(
            'SPONSORED_PAYMENT_INSUFFICIENT_BALANCE',
            '❌ Недостаточно средств на балансе. Не хватает {amount}.',
        ).format(amount=texts.format_price(missing_kopeks))
        if callback.message and not isinstance(callback.message, InaccessibleMessage):
            await callback.message.edit_text(
                text,
                reply_markup=get_insufficient_balance_keyboard(db_user.language, amount_kopeks=missing_kopeks),
            )
        await callback.answer()
        return

    await state.clear()

    success_text = texts.t(
        'SPONSORED_PAYMENT_SUCCESS',
        '✅ Подписка для {name} успешно оплачена на {days} дн.',
    ).format(name=recipient.full_name, days=period_days)

    breakdown_by_period: dict = data.get(_DATA_BREAKDOWN) or {}
    breakdown_text = breakdown_by_period.get(period_raw)
    if breakdown_text:
        success_text += (
            '\n\n' + texts.t('PRICE_BREAKDOWN_TITLE', '💰 Из чего сложилась цена:') + '\n' + breakdown_text
        )

    if callback.message and not isinstance(callback.message, InaccessibleMessage):
        await callback.message.edit_text(success_text, reply_markup=get_back_keyboard(db_user.language))
    await callback.answer()

    # Уведомление получателя — best-effort и намеренно ПОСЛЕ ответа плательщику:
    # оплата уже совершена, сбой отправки не должен превращаться в ошибку платежа.
    await _notify_recipient(callback.bot, recipient, db_user, period_days)


async def _notify_recipient(bot: Bot, recipient: User, payer: User, period_days: int) -> None:
    """Сообщает получателю, что подписку оплатили за него.

    Получатель мог заблокировать бота или удалить чат — любая ошибка отправки
    только логируется, платёж к этому моменту уже применён и откатывать его
    из-за недоступности уведомления нельзя.
    """
    if not recipient.telegram_id:
        return

    texts = get_texts(recipient.language)
    text = texts.t(
        'SPONSORED_PAYMENT_RECIPIENT_NOTIFICATION',
        '🎁 Вашу подписку оплатил {name} — {days} дн.',
    ).format(name=payer.full_name, days=period_days)

    try:
        await bot.send_message(chat_id=recipient.telegram_id, text=text)
    except Exception as error:
        logger.warning(
            'Не удалось уведомить получателя об оплате подписки',
            recipient_id=recipient.id,
            error=str(error)[:200],
        )


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(start_sponsored_payment, F.data == CALLBACK_START)
    dp.message.register(process_recipient_query, SponsoredPaymentStates.waiting_for_recipient)
    dp.callback_query.register(handle_period_selection, F.data.startswith(_CALLBACK_PERIOD_PREFIX))
