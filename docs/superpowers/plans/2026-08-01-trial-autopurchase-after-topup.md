# Авто-активация платного триала после пополнения — план

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Платный триал в кабинете активируется автоматически после пополнения баланса — как обычный тариф.

**Architecture:** Зеркалим готовый tariff-autopurchase-after-topup флоу для платного триала: (1) общая функция активации триала, (2) `activate_trial` сохраняет корзину `trial_purchase` + 402, (3) `_auto_purchase_trial` handler в диспетчере автопокупки, (4) фронт-карточка кладёт корзину и ведёт на пополнение с подставленной суммой.

**Tech Stack:** Python/FastAPI/aiogram/SQLAlchemy-async (bot), React/TS/Vite/vitest (cabinet).

## Global Constraints
- Денежный путь: активация триала и списание `TRIAL_ACTIVATION_PRICE` — ОДНА логика в двух точках (эндпоинт + автопокупка), без дублирования; при ошибке после списания — компенсирующий возврат.
- Автопокупка триала best-effort в вебхуке: её сбой не роняет зачисление баланса.
- Идемпотентность: триал уже использован / есть активная подписка / баланс < цены / `TRIAL_PAYMENT_ENABLED=off` → НЕ активировать и НЕ списывать (`return False`). Никакого двойного триала/списания.
- Цена — актуальная `TRIAL_ACTIVATION_PRICE` на момент активации (не из корзины).
- Bot-тесты: `.venv/bin/python3 -m pytest`. Cabinet: `npx tsc --noEmit` + `npm run build` + `npx vitest run`.
- Публичные репо — без секретов. Коммиты на русском, БЕЗ `Co-Authored-By`.
- Локали кабинета: новые ключи только в `ru.json`+`en.json` (не трогать `fa.json`/`zh.json`); прогнать locale-тест.

---

### T1 — Backend: общая функция активации платного триала

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/purchase.py` (эндпоинт `activate_trial`, строки ~1409-1619 — вынести ядро)
- Create/Modify: место для общей функции — новый модуль `app/services/trial_activation_service.py` ИЛИ функция в `app/cabinet/routes/subscription_modules/` (выбрать по месту; сервис предпочтительнее для импорта из auto_purchase без циклов)
- Test: `tests/services/test_trial_activation_core.py`

**Interfaces:**
- Produces: `async def activate_paid_trial_core(db, user, *, bot=None) -> Subscription` — активирует триал (создаёт `create_trial_subscription` с параметрами из trial-тарифа/настроек, синкает Remnawave, шлёт уведомления). НЕ содержит проверок «уже использован/активна подписка» (их делают вызыватели) — но списание цены вынести в отдельный шаг или параметр, чтобы автопокупка могла списывать сама. Точную границу (что внутри core, что снаружи) определить при рефакторинге, сохранив текущее поведение эндпоинта.

**Шаги (TDD):**
1. Прочитать `activate_trial` целиком (`purchase.py:1409-1619`), выделить ядро активации (после списания цены): вычисление параметров триала из `get_trial_tariff`/settings, `create_trial_subscription`, `SubscriptionService.create_remnawave_user`, уведомления (email/WS/админ).
2. Написать провальный тест `test_activate_paid_trial_core_creates_subscription`: мокнуть зависимости, вызвать `activate_paid_trial_core(db, user)`, проверить, что создаётся триал-подписка с корректными duration/traffic/device из настроек.
3. Вынести ядро в `activate_paid_trial_core`, вызвать её из `activate_trial` вместо инлайна. Поведение эндпоинта при достаточном балансе идентично (тот же ответ `SubscriptionResponse`).
4. `.venv/bin/python3 -m pytest tests/services/test_trial_activation_core.py -q` — зелёно. `python3 -m py_compile` на изменённых файлах.
5. Коммит (рус, без Co-Authored-By).

### T2 — Backend: activate_trial сохраняет корзину trial_purchase + 402

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/purchase.py` (`activate_trial`, ветка нехватки баланса ~1447-1451)
- Test: `tests/cabinet/test_trial_insufficient_saves_cart.py`

**Interfaces:**
- Consumes: `user_cart_service.save_user_cart` (ставит intent при `return_to_cart:True`).
- Produces: при нехватке баланса на платный триал — корзина `cart_mode:'trial_purchase'` + HTTP 402.

**Шаги (TDD):**
1. Написать провальный тест: `TRIAL_PAYMENT_ENABLED=True`, `TRIAL_ACTIVATION_PRICE=1000`, у юзера баланс `0`. Вызвать `activate_trial` → ожидать `HTTPException 402` c detail `code=='insufficient_funds'`, `cart_mode=='trial_purchase'`, `cart_saved==True`; и что `user_cart_service.save_user_cart` вызван с `cart_mode='trial_purchase'`, `return_to_cart=True`, `total_price=1000`.
2. Запустить — падает (сейчас 400, корзина не сохраняется).
3. В `activate_trial` заменить ветку `balance < price` (строки ~1447-1451): сохранить корзину
```python
cart_data = {
    'cart_mode': 'trial_purchase',
    'total_price': price_kopeks,
    'missing_amount': price_kopeks - user.balance_kopeks,
    'user_id': user.id,
    'saved_cart': True,
    'return_to_cart': True,
    'source': 'cabinet',
    'description': 'Активация пробной подписки',
}
try:
    await user_cart_service.save_user_cart(user.id, cart_data)
except Exception as e:
    logger.error('Error saving trial cart for auto-purchase (cabinet)', error=e)
raise HTTPException(
    status_code=status.HTTP_402_PAYMENT_REQUIRED,
    detail={
        'code': 'insufficient_funds',
        'message': f'Недостаточно средств. Не хватает {settings.format_price(price_kopeks - user.balance_kopeks, round_kopeks=False)}',
        'missing_amount': price_kopeks - user.balance_kopeks,
        'cart_saved': True,
        'cart_mode': 'trial_purchase',
    },
)
```
   (импорт `user_cart_service` уже есть в файле.)
4. `.venv/bin/python3 -m pytest tests/cabinet/test_trial_insufficient_saves_cart.py -q` — зелёно.
5. Коммит.

### T3 — Backend: _auto_purchase_trial + регистрация в диспетчере

**Files:**
- Modify: `app/services/subscription_auto_purchase_service.py` (новый handler + ветка в `_process_single_cart` ~3151-3170)
- Test: `tests/services/test_auto_purchase_trial.py`

**Interfaces:**
- Consumes: `activate_paid_trial_core` (T1), `subtract_user_balance`, `create_transaction`, `notify_user_subscription_activated`, `_delete_cart_for_subscription`, `clear_subscription_checkout_draft`.
- Produces: `async def _auto_purchase_trial(db, user, cart_data, *, bot=None) -> bool`; роутинг `cart_mode == 'trial_purchase'` в `_process_single_cart`.

**Шаги (TDD):**
1. Написать провальные тесты `test_auto_purchase_trial.py`:
   - `test_trial_activated_when_affordable`: `TRIAL_PAYMENT_ENABLED=True`, price=1000, баланс=1000, триал не использован, активной подписки нет → `_auto_purchase_trial` возвращает True, списывает 1000, вызывает `activate_paid_trial_core`.
   - `test_skip_when_trial_already_used`: `is_trial_already_used()=True` → False, без списания.
   - `test_skip_when_insufficient_balance`: баланс < price → False, без списания.
   - `test_dispatcher_routes_trial_purchase`: `_process_single_cart` с `cart_mode='trial_purchase'` зовёт `_auto_purchase_trial`.
2. Запустить — падают.
3. Реализовать `_auto_purchase_trial` по образцу `_auto_purchase_tariff` (`subscription_auto_purchase_service.py:808`): гарды → `lock_user_for_pricing` → проверка баланса vs `settings.TRIAL_ACTIVATION_PRICE` → `subtract_user_balance(..., mark_as_paid_subscription=True)` → `activate_paid_trial_core(db, user, bot=bot)` (если списание вынесено внутрь core — не списывать дважды; согласовать с T1) → `create_transaction(SUBSCRIPTION_PAYMENT)` → `_delete_cart_for_subscription` + `clear_subscription_checkout_draft` → уведомления (WS `notify_user_subscription_activated`, админ, email) → `return True`. Компенсирующий возврат при ошибке после списания (как в `_auto_purchase_tariff`, строки ~1001-1032). Добавить ветку в `_process_single_cart`:
```python
if cart_mode == 'trial_purchase':
    return await _auto_purchase_trial(db, user, cart_data, bot=bot)
```
4. `.venv/bin/python3 -m pytest tests/services/test_auto_purchase_trial.py -q` — зелёно. `py_compile`.
5. Коммит.

### T4 — Frontend: карточка триала кладёт корзину и ведёт на пополнение

**Files:**
- Modify: `src/components/dashboard/TrialOfferCard.tsx`
- Modify: `src/api/*` (функция сохранения корзины триала через `POST /subscription/trial`)
- Modify: `src/locales/ru.json` + `src/locales/en.json` (если нужны новые ключи; переиспользовать существующие где можно)
- Test: `src/components/dashboard/TrialOfferCard.test.tsx` (новый)

**Interfaces:**
- Consumes: `InsufficientBalancePrompt` (`missingAmountKopeks`, `onBeforeTopUp`), эндпоинт `POST /subscription/trial` (возвращает 402 `insufficient_funds` с `cart_saved`).
- Produces: при `!canAfford` карточка сохраняет корзину триала и ведёт на `/balance/top-up?amount=…&returnTo=…`.

**Шаги (TDD):**
1. Найти существующий API-модуль триала (`src/api/subscription*` или где `activateTrial`); добавить `saveTrialCart()` — POST на `/subscription/trial`, при 402 `insufficient_funds` трактовать как успех (корзина сохранена), прочие ошибки — пробрасывать.
2. Написать провальный тест `TrialOfferCard.test.tsx`: `requires_payment=true`, price 1000, balance 0 → рендерится `InsufficientBalancePrompt` (или кнопка с missing=1000); клик вызывает `onBeforeTopUp` (saveTrialCart) ПЕРЕД навигацией на `/balance/top-up`.
3. В `TrialOfferCard.tsx` заменить `!canAfford` ветку (`<Link to="/balance">`, строки ~176-188) на `InsufficientBalancePrompt` с `missingAmountKopeks = trialInfo.price_kopeks - balanceKopeks` и `onBeforeTopUp={saveTrialCart}`. Кнопка «Оплатить с баланса» (canAfford) и «Сразу купить подписку» — без изменений. Мок jsdom-теста — как в `SuccessNotificationModal.test.tsx`.
4. Гейт: `npx tsc --noEmit` (0), `npm run build`, `npx vitest run src/components/dashboard/TrialOfferCard.test.tsx`, `npx vitest run src/locales/locales.test.ts`. Все зелёные.
5. Коммит.

---

## Порядок выполнения
T1 → T2 → T3 (бэкенд) → T4 (фронт). Затем финальное ревью веток обоих репо и мерж в main.

## Self-Review
- Спека покрыта: T1=архитектура п.1, T2=п.2, T3=п.3, T4=п.4. Тесты спеки → в шагах каждой задачи.
- Денежный путь: списание согласовано между core (T1) и auto (T3) — явно отмечено «не списывать дважды».
- Типы/имена согласованы: `activate_paid_trial_core`, `_auto_purchase_trial`, `cart_mode='trial_purchase'` — одинаково во всех задачах.
