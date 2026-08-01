# Авто-активация платного триала после пополнения — дизайн

**Дата:** 2026-08-01
**Статус:** дизайн утверждён владельцем («делай»), готов к плану
**Репозитории:** `remnawave-bedolaga-telegram-bot` (backend `app/`) + `bedolaga-cabinet` (frontend `src/`)

## Проблема / цель
Карточка «Пробная подписка» в кабинете (платный триал, `TRIAL_ACTIVATION_PRICE`, напр. 10₽) при нехватке баланса показывает кнопку **«Пополнить баланс»**, которая ведёт на общее пополнение (`<Link to="/balance">`) и **не сохраняет корзину** под триал. После оплаты автопокупке нечего активировать → деньги оседают на балансе, триал не активируется. Юзер ждёт, что после оплаты триал активируется сам.

**Цель:** сделать «Пополнить баланс» на карточке триала полностью автоматическим — как у обычных тарифов: пополнил → вебхук сам активирует триал (даже если юзер закрыл кабинет).

## Что уже есть (переиспользуем)
- **Обычные тарифы уже так работают:** `/purchase-tariff` при нехватке баланса сохраняет корзину (`cart_mode:'tariff_purchase'`, `return_to_cart:True`) + возвращает 402; после пополнения `send_cart_notification_after_topup` → `auto_purchase_saved_cart_after_topup` → `_process_single_cart` выкупает корзину. Флаг `AUTO_PURCHASE_AFTER_TOPUP_ENABLED` у владельца **включён**.
- **Маркер намерения:** `user_cart_service.save_user_cart` ставит `cart_topup_intent:{uid}` (TTL `CART_AUTOPURCHASE_INTENT_TTL_SECONDS`=3600), когда в корзине `return_to_cart:True`. Автопокупка гейтится `has_topup_intent`.
- **Диспетчер:** `_process_single_cart` (`app/services/subscription_auto_purchase_service.py:3063`) роутит по `cart_mode`: `extend`/`tariff_purchase`/`daily_tariff_purchase`/`add_devices`/`add_traffic`/`subscription_purchase`. Триал-режима НЕТ.
- **Активация триала:** `POST /subscription/trial` → `activate_trial` (`app/cabinet/routes/subscription_modules/purchase.py:1409`). Сейчас при нехватке баланса кидает **400** и корзину не сохраняет. Ядро активации (списание `TRIAL_ACTIVATION_PRICE` → `create_trial_subscription` → Remnawave → уведомления) — длинный инлайн-блок в эндпоинте.
- **Фронт:** переиспользуемый `src/components/InsufficientBalancePrompt.tsx` — props `missingAmountKopeks`, `onBeforeTopUp` (сохранить корзину перед пополнением), навигация на `/balance/top-up?amount=…&returnTo=…` с подставленной суммой. Триал-карточка — `src/components/dashboard/TrialOfferCard.tsx`.

## Архитектура

### Backend (бот, `app/`)
1. **Общая функция активации триала.** Вынести ядро из `activate_trial` (после проверки/списания цены) в переиспользуемую async-функцию — напр. `activate_paid_trial_core(db, user, *, bot=None) -> Subscription` (в trial-сервисе или в `subscription_modules`). Возвращает созданную подписку. Списание `TRIAL_ACTIVATION_PRICE` может быть внутри или отдельным шагом — но обе точки (эндпоинт и автопокупка) используют ОДНУ логику активации, без дублирования денежного пути. Поведение эндпоинта при достаточном балансе не меняется.
2. **`activate_trial`: сохранение корзины + 402.** Когда `requires_payment` и `balance < TRIAL_ACTIVATION_PRICE`: сохранить корзину `{cart_mode:'trial_purchase', total_price:price, missing_amount, return_to_cart:True, source:'cabinet', description:'Активация пробной подписки'}` через `user_cart_service.save_user_cart` и вернуть **402** с телом `{code:'insufficient_funds', message, missing_amount, cart_saved:True, cart_mode:'trial_purchase'}` (зеркало `/purchase-tariff`). Заменяет нынешний 400.
3. **`_auto_purchase_trial(db, user, cart_data, *, bot=None) -> bool`** в `subscription_auto_purchase_service.py`. Гарды (все → `return False` без списания): триал уже использован (`is_trial_already_used`), есть активная подписка, `TRIAL_PAYMENT_ENABLED` выключен, баланс < текущей `TRIAL_ACTIVATION_PRICE`. Иначе: списать цену, активировать через общую функцию (п.1), удалить корзину (`_delete_cart_for_subscription`) + `clear_subscription_checkout_draft`, WS `notify_user_subscription_activated`, уведомления админам/email (как `_auto_purchase_tariff`). Компенсирующий возврат при ошибке после списания (как в других `_auto_*`). Регистрация в `_process_single_cart`: `if cart_mode == 'trial_purchase': return await _auto_purchase_trial(...)`.

### Frontend (кабинет, `src/`)
4. **API + карточка.** Добавить в `src/api/*` функцию сохранения корзины триала — вызвать `POST /subscription/trial` и трактовать 402 `insufficient_funds` как «корзина сохранена» (или тонкий враппер, дергающий эндпоинт и глотающий 402). В `TrialOfferCard.tsx` при `!canAfford` заменить `<Link to="/balance">` на `InsufficientBalancePrompt` с `missingAmountKopeks = price_kopeks − balanceKopeks` и `onBeforeTopUp = saveTrialCart`. Кнопка «Оплатить с баланса»/«Активировать» и зелёная «Сразу купить подписку» — без изменений.

## Обработка ошибок / крайние случаи
- Автопокупка триала best-effort в рамках вебхука: её сбой не должен ронять зачисление баланса (обёртки как у `_auto_purchase_tariff`).
- Идемпотентность: если между сохранением корзины и вебхуком юзер уже активировал триал вручную (баланса хватило) — `_auto_purchase_trial` видит активную подписку/использованный триал → `return False`, корзина чистится в `_process_single_cart` (DISABLED-гард) либо handler'ом; двойного триала/двойного списания нет.
- Пополнение больше цены / частичное: маркер намерения одноразовый, гасится только при успехе — как у тарифов.
- Пополнение ради «просто денег» (без корзины/намерения) триал НЕ активирует.
- Цена берётся актуальная из `TRIAL_ACTIVATION_PRICE` на момент активации (не из корзины) — защита от рассинхрона.

## Тестирование
- **Backend unit:** (а) `activate_trial` при нехватке баланса сохраняет корзину `trial_purchase` и отдаёт 402 (не 400); при достатке — активирует как раньше. (б) `_auto_purchase_trial`: single-flow активирует триал и списывает цену; гарды (триал использован / есть активная подписка / баланс < цены / TRIAL_PAYMENT_ENABLED=off) → не активирует и не списывает; сбой активации → возврат средств. (в) `_process_single_cart` роутит `trial_purchase` в новый handler.
- **Frontend:** сборка `tsc`+`build`+`vitest`; карточка при `!canAfford` рендерит prompt с missing=price−balance и зовёт `onBeforeTopUp` перед переходом на `/balance/top-up?amount=…`.

## Вне объёма
- Бесплатный триал (`requires_payment=false`) — активируется сразу, авто-топап не нужен.
- Бот-флоу триала (не кабинет-карточка) — общий backend-handler им доступен, но UI бота не трогаем.
- Изменение цены/параметров триала, новых способов оплаты.
