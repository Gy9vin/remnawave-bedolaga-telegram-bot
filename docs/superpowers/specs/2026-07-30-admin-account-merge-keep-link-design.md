# Админ-объединение аккаунтов с сохранением ссылки подписки — дизайн

**Дата:** 2026-07-30
**Статус:** дизайн утверждён владельцем, готов к плану
**Репозитории:** `remnawave-bedolaga-telegram-bot` (backend `app/`) + `bedolaga-cabinet` (frontend `src/`)
**Связано:** Подпроект B из [[2026-07-25-account-merge-choose-survivor-design]] (пользовательское объединение уже сделано).

## Проблема / цель
Админу нужно объединять два аккаунта клиента (например TG + Яндекс, или два TG) с **выбором приоритетного аккаунта** и, главное, **без смены ссылки подписки** — чтобы клиенту не переимпортировать конфиг в приложении. Сейчас админский `POST /cabinet/admin/users/merge` объединяет, но: (1) нет UI, (2) итоговая подписка выбирается по «более поздней дате», а не по той, где реально висят устройства, поэтому ссылка может смениться.

## Решения (утверждены владельцем)
- Запуск **из карточки пользователя** (кнопка «Объединить с другим аккаунтом»).
- В сравнении **показывать привязку устройств живьём из панели RemnaWave** (какие устройства/приложения висят на каждой подписке — подсказка, какой ссылкой пользуется клиент).
- Админ явно выбирает **приоритетный аккаунт** (выживает) и **какую подписку/ссылку сохранить**.
- **Дни складываются** (как в пользовательском merge).
- Кнопка/эндпоинты под правом **`users:edit`** (как у текущего merge).
- **Мультиподписка:** показываем все подписки обоих аккаунтов; склеиваем **выбранную с выбранной** (по одной с каждой стороны), остальные подписки просто переносятся на выжившего.

## Что уже есть
- `POST /cabinet/admin/users/merge` → `admin_merge_users` (`app/cabinet/routes/admin_user_linking.py:361`), право `users:edit`, схема `AdminMergeUsersRequest{primary_user_id, secondary_user_id}`, зовёт `execute_merge(primary_user_id, secondary_user_id)`.
- `execute_merge` со складыванием подписок (`_handle_subscription_merge`, `_combine_subscription_end_dates`).
- Frontend `adminUsersApi.mergeUsers(...)` (`src/api/adminUsers.ts:915`) — без UI.
- «Ссылка подписки» = `subscription.subscription_url` / `subscription_crypto_link` / `remnawave_short_uuid` (`app/database/models.py`), приходит из панельного пользователя RemnaWave. Обновление telegram_id/email в панели `short_uuid` НЕ меняет → ссылка стабильна, пока сохраняется панельный юзер этой подписки.

## Архитектура

### Backend (`remnawave-bedolaga-telegram-bot/app/`)

1. **Метод панели: устройства конкретного юзера.** В `app/external/remnawave_api.py` добавить `get_user_hwid_devices(uuid: str) -> dict` (GET `/api/hwid/devices/{uuid}` — вернуть `{devices: [...], total}`; поля устройства: hwid, platform/app, last seen — что отдаёт панель). Изолированный метод, обёрнут в существующий error-handling.

2. **Превью-эндпоинт для админ-слияния.** Новый `GET /cabinet/admin/users/merge/preview?primary_user_id=&secondary_user_id=` (право `users:edit`) → по обоим юзерам вернуть:
   - для каждого: базовое (id, способы входа, баланс, рефералы, дата регистрации);
   - список подписок с полями `{subscription_id, tariff_name, end_date, status, subscription_url, remnawave_short_uuid, devices_count, devices: [{app, platform, last_seen}]}` — `devices_*` берутся из панели через `get_user_hwid_devices` по `remnawave_uuid` подписки.
   Ответ — Pydantic `AdminMergePreviewResponse{primary: AdminMergePreviewUser, secondary: AdminMergePreviewUser}`.

3. **`AdminMergeUsersRequest`** (`admin_user_linking.py:63`): добавить `keep_subscription_id: int | None = None` — id подписки, чью ссылку сохранить. `None` → поведение как сейчас (combine по поздней дате).

4. **Combine с выбором сохраняемой ссылки.** В `_handle_subscription_merge` / `execute_merge` пробросить `keep_subscription_id`. Когда он задан и указывает на одну из подписок объединяемой пары:
   - **сохраняемая подписка = указанная** (не «с более поздней датой»);
   - её `remnawave_uuid`/`remnawave_short_uuid`/`subscription_url`/`subscription_crypto_link` **не трогаем** → ссылка неизменна;
   - `end_date` сохраняемой = `end_date + max(0, other.end - now)` (складываем остаток второй, `_combine_subscription_end_dates`);
   - панельный юзер **второй** (не сохранённой) подписки удаляется отложенно после commit (существующий паттерн `flush_remnawave_deletions`);
   - тариф/трафик/лимиты — от сохранённой подписки.
   - Пожизненная сохранённая — просто остаётся, складывать нечего.
   - Мультиподписка: правило применяется к выбранной паре; непересекающиеся подписки второго аккаунта переносятся на выжившего как есть.

5. **`admin_merge_users`**: принять `keep_subscription_id`, провалидировать что он принадлежит одному из двух юзеров, пробросить в `execute_merge`. Пост-merge `resync_user_subscriptions_with_panel` обновит telegram_id/email у сохранённого панельного юзера (ссылку не меняет).

### Frontend (`bedolaga-cabinet/src/`)

1. **API** `src/api/adminUsers.ts`: `getMergePreview(primaryId, secondaryId)`; расширить `mergeUsers(primaryId, secondaryId, keepSubscriptionId?)`.
2. **Кнопка** в карточке юзера `src/pages/AdminUserDetail.tsx`: «Объединить с другим аккаунтом» (видна при праве `users:edit`).
3. **Модалка/шаг поиска** второго аккаунта (по TG ID / email / id) — переиспользовать существующий admin-поиск юзеров.
4. **Экран сравнения** (новый компонент, напр. `src/components/admin/userDetail/AdminMergePanel.tsx`): две колонки, по каждой — способы входа/баланс/рефералы/дата, и подписки с тарифом/датой/**ссылкой** и **📱 привязкой устройств** (кол-во + приложения из панели). Radio «Приоритетный аккаунт» (survivor) и radio «Сохранить эту ссылку» (на подписке). Плашка-предупреждение: второй аккаунт закроется, данные переедут в приоритетный, выбранная ссылка останется.
5. Confirm → `mergeUsers(primaryId, secondaryId, keepSubscriptionId)`; после успеха — рефетч карточки.

## Обработка ошибок / крайние случаи
- `keep_subscription_id` не принадлежит паре → 400.
- У аккаунта нет подписки → в колонке «нет подписки», выбирать нечего с этой стороны; combine складывать нечего.
- Панель недоступна при превью → показать подписки без устройств + пометку «устройства не загружены» (не блокировать слияние).
- RemnaWave-удаление второй подписки — отложенно после commit БД (как сейчас).
- Обнуление уник-полей поглощённого и ревокация refresh-токенов — как в `execute_merge`.

## Тестирование
- **Backend unit (`account_merge_service`):** combine с `keep_subscription_id` = ранняя-по-дате подписка → она сохранена (её `remnawave_short_uuid`/`subscription_url` не изменились), `end_date` += остаток второй, у второй панель помечена на удаление; `keep_subscription_id=None` → прежнее поведение.
- **Backend endpoint:** `GET /merge/preview` — форма ответа + устройства из замоканного RemnaWave API; `POST /merge` с `keep_subscription_id` — валидация чужого id (400).
- **RemnaWave API:** `get_user_hwid_devices` — парсинг ответа (мок).
- **Frontend:** сборка `tsc`+`build`; выбор приоритета/ссылки формирует payload `{primary_user_id, secondary_user_id, keep_subscription_id}`; отображение устройств.

## Вне объёма
- Слияние >2 аккаунтов за раз.
- Перенос устройств/HWID между панельными юзерами (сохраняем ссылку целиком, устройства едут вместе с ней).
- Изменение самой ссылки/ротация short_uuid.
