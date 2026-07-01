# Google Auth Sunset — миграция пользователей перед отключением входа через Google

**Дата:** 2026-07-01
**Статус:** утверждён дизайн, готов к плану реализации
**Репозитории:** `remnawave-bedolaga-telegram-bot` (backend `app/`) + `bedolaga-cabinet` (frontend `src/`)

## Контекст и проблема

С 7 июля 2026 в РФ действует ограничение (штраф ~50 000 ₽) за наличие на сайте авторизации через иностранные сервисы (Google / Apple). В системе:

- **Sign in with Apple отсутствует** (все `apple*` в коде — это Apple IAP, покупки, а не вход).
- Вход через OAuth поддерживает 4 провайдера (`app/database/crud/user.py:1564` `OAUTH_PROVIDER_COLUMNS`): `google → google_id`, `yandex → yandex_id`, `discord → discord_id`, `vk → vk_id`.
- Под ограничение попадает только **Google**. Яндекс/VK — российские, не трогаем.

Замеры по БД (`users`):

| Метрика | Значение |
|---|---|
| С привязанным `google_id` | **278** |
| Из них зарегистрированы через Google (`auth_type='google'`) — «гугл-онли» | **215** |
| yandex / discord / vk | 1088 / 0 / 0 |

`google_id` хранит Google OpenID `sub` (числовой идентификатор аккаунта), а **не** email — email лежит отдельно в `users.email` (`app/cabinet/auth/oauth_providers.py:236`). Значит 278 — это реально входившие через Google, а не «у кого почта на gmail».

**Риск:** 215 «гугл-онли» пользователей при простом отключении Google потеряют доступ (у них нет ни пароля, ни Telegram).

## Цель

Дать всем 278 Google-пользователям способ входа без Google **до** его отключения:
1. Разослать им email с предупреждением и персональной кнопкой «Задать пароль» (magic-link).
2. По клику — лендинг задания пароля; после этого вход по email + паролю.
3. Также сообщить о возможности привязать Telegram / Яндекс / обычную почту в кабинете.

Отключение самого Google-входа — **отдельная фаза 2**, после миграции (вне этого спека).

## Что уже готово в коде (переиспользуем)

- **Задание пароля end-to-end:** `POST /auth/password/forgot` → email → `{CABINET_URL}/reset-password?token=...` → `POST /auth/password/reset` ставит `password_hash`. Лендинг `bedolaga-cabinet/src/pages/ResetPassword.tsx`.
- **Отправка email:** `app/cabinet/services/email_service.py` (SMTP настроен), кастомные шаблоны через `app/cabinet/routes/admin_email_templates.py` + `email_template_overrides.get_rendered_override`.
- **Массовые email:** `app/services/broadcast_service.py::EmailBroadcastService` (rate-limit 8/сек, прогресс в `BroadcastHistory`). Но шлёт **одинаковый** HTML и **не** поддерживает персональные токены → для magic-link не подходит напрямую.
- **Привязка аккаунтов:** `bedolaga-cabinet/src/pages/ConnectedAccounts.tsx` (Telegram + google/yandex/discord/vk + email) — линковка уже работает.

## Ключевые технические ограничения (учтены в дизайне)

1. **Срок жизни reset-токена по умолчанию — 1 час** (`app/config.py:1199 CABINET_PASSWORD_RESET_EXPIRE_HOURS = 1`). Для массовой акции мало → инвайты выпускают **отдельный долгоживущий токен** (новый конфиг, дефолт 30 дней).
2. **Вход по email требует `password_hash` И `email_verified = True`** (`app/cabinet/routes/auth.py:48,79`). У Google-юзеров пароля нет, verified не гарантирован → при отправке инвайта **проставляем `email_verified = True`** (email из Google подтверждён).
3. `POST /auth/password/reset` ищет юзера по токену без ограничений по `auth_type` → для Google-юзера пароль поставится, `google_id` **не трогаем** (останется до фазы 2).
4. **email уникален** в БД (`app/database/models.py:1916 unique=True`), логин ищет `scalar_one_or_none` по `func.lower(email)` → неоднозначности дублей нет.

## Выбранный подход

Отдельная изолированная фича **«Google migration»** (а не расширение общего broadcast): свой bulk-сервис по образцу `EmailBroadcastService`, который на каждого пользователя генерит персональный set-password токен и шлёт письмо. Переиспользует `email_service`, механизм reset-токенов и лендинг `ResetPassword.tsx`.

Отклонённая альтернатива: добавить в общий broadcast переменную `{{set_password_url}}` с генерацией токена на получателя — повышает связность broadcast-сервиса, дольше. Не берём.

## Архитектура

### Backend (`remnawave-bedolaga-telegram-bot/app/`)

1. **CRUD** — `get_google_linked_users(db)` → `User` где `google_id IS NOT NULL AND email IS NOT NULL` (все 278). Плюс агрегатор для статуса: total, из них `auth_type='google'`, из них уже `password_hash IS NOT NULL`.
2. **Email-шаблон** `google_sunset_invite` — дефолтный текст (см. ниже) + переменные `{{set_password_url}}`, `{{cabinet_url}}`, `{{username}}`. Редактируется через существующий overrides-механизм.
3. **Сервис** `GoogleMigrationService` (по образцу `EmailBroadcastService`): фоновая задача, rate-limit 8 писем/сек, прогресс/итоги. На каждого получателя:
   - `email_verified = True`, `email_verified_at = now`;
   - сгенерировать reset-токен с длинным сроком (`GOOGLE_MIGRATION_TOKEN_EXPIRE_DAYS`), записать `password_reset_token` / `password_reset_expires`;
   - отправить письмо, где `{{set_password_url}} = {CABINET_URL}/reset-password?token=<token>`.
   - Идемпотентность: повторный запуск перевыпускает токен и шлёт заново (для «допинать» неоткрывших).
4. **Admin-роуты** (под существующим admin-auth + RBAC):
   - `POST /admin/google-migration/send` — старт рассылки (фон), опц. `{ target: 'all_google' | 'google_only' }`.
   - `GET /admin/google-migration/status` — счётчики (total с Google, google-only, уже с паролем) + статус/прогресс/итоги последнего запуска.
5. **Config:** `GOOGLE_MIGRATION_TOKEN_EXPIRE_DAYS: int = 30`. Плюс задел `GOOGLE_AUTH_ENABLED: bool = True` для фазы 2 (в этом спеке только объявляем, поведение не меняем).

### Frontend (`bedolaga-cabinet/src/`)

1. **Админ-страница «Миграция Google»**: счётчики (278 / 215 / сколько уже с паролем), кнопка «Отправить инвайты», прогресс и статус последнего запуска. Роут + пункт меню в админ-разделе.
2. **`ResetPassword.tsx`**: переиспользуем как есть; косметически — заголовок «Задайте пароль», когда пароля ещё не было (опционально, не блокирует).
3. **Локали** для новых строк (ru/en/fa/zh — минимум ru/en).

### Текст письма (дефолт шаблона)

Кастомный, предоставлен владельцем; в него встраивается кнопка-magic-link «🔐 Задать пароль» (`{{set_password_url}}`). Упоминание Apple оставляем (не вредит, хотя Apple-входа в системе нет). Черновик:

> ⚠️ Друзья, важное — не пропустите!
> С 7 июля вход через Google ID и Apple ID больше не будет работать — такие теперь ограничения 😔
> Чтобы не остаться без доступа к своему кабинету — привяжите другой способ входа уже сейчас 👇
> ✅ Telegram ✅ Яндекс ID ✅ Обычная почта
> 🔐 А можно просто задать пароль для своей почты — и спокойно входить по логину и паролю.
> **[ 🔐 Задать пароль ]** ← персональная кнопка ({{set_password_url}})
> 👉 Также всё можно сделать в личном кабинете → раздел «Профиль». Займёт меньше минуты!
> ❗️ Если вы входите только через Google или Apple — не тяните!
> — EasyTunel ❤️

(Точный HTML и адаптацию раздела «Профиль»/«Connected Accounts» финализируем на этапе плана; проверить фактический пункт меню кабинета, куда ведёт инструкция.)

## Поток данных

1. Админ жмёт «Отправить инвайты» → `POST /admin/google-migration/send`.
2. `GoogleMigrationService` берёт `get_google_linked_users`, батчами: ставит `email_verified=True`, выпускает долгий reset-токен, шлёт письмо с персональным `set_password_url`.
3. Пользователь жмёт кнопку → `ResetPassword.tsx` (`?token=...`) → вводит пароль → `POST /auth/password/reset` → `password_hash` установлен.
4. Далее пользователь входит через `POST /auth/email/login` (email + пароль). Google не трогаем до фазы 2.

## Обработка ошибок

- SMTP не настроен → рассылка помечается failed, статус виден в админке (как в `EmailBroadcastService`).
- Юзер без email в выборке не участвует (фильтр в CRUD).
- Просроченный/невалидный токен на лендинге → существующая ошибка reset-эндпоинта; повторный запуск рассылки перевыпускает токен.
- Rate-limit SMTP — троттлинг 8/сек, как в существующем сервисе.

## Тестирование

- Unit: `get_google_linked_users` (фильтры google_id/email, счётчики).
- Unit: генерация инвайта — `email_verified` выставлен, токен с корректным длинным сроком, `set_password_url` содержит токен.
- Integration: инвайт → `POST /auth/password/reset` с токеном → затем `POST /auth/email/login` проходит для бывшего google-юзера.
- Admin-роут: доступ только под нужной RBAC-ролью; `status` возвращает корректные счётчики.

## Вне объёма (следующие фазы)

- **Фаза 2:** отключение Google-входа (скрыть кнопку в кабинете + отклонять google OAuth через `GOOGLE_AUTH_ENABLED`).
- Yandex/VK/Discord не меняем.
