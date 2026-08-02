# Совместимость RemnaWave Panel v2 ↔ v3 — дизайн

**Дата:** 2026-08-02
**Статус:** дизайн утверждён владельцем (двойная совместимость), на ревью спеки
**Репозиторий:** `remnawave-bedolaga-telegram-bot` (backend `app/`)
**Источник схем v3:** `https://cdn.docs.rw/docs/openapi.json` (Remnawave API v3.1.0), локальная копия для имплементеров: `/private/tmp/claude-501/-Users-mihail-Desktop-Serv-remnawave-bedolaga-telegram-bot/51fe188b-49c5-47a2-a076-66f1e20ae1cf/scratchpad/rw-openapi.json`

## Проблема / цель
RemnaWave v3.1.0 (backend v3) — ломающий релиз: **user идентифицируется числовым `id`, а не UUID** (поле `uuid` удалено из user-объектов; пути `/api/users/{uuid}` → `/api/users/{userId}`), часть lookup-эндпоинтов удалена. Наша интеграция построена на user-UUID (`remnawave_uuid`), поэтому на v3 бот ломается: KeyError при парсинге юзера, 404/неверные пути при всех user-операциях. У владельца уже есть панель на v3, при этом другие могут оставаться на v2.

**Цель:** бот работает и с v2, и с v3 — автоопределение версии, единый внутренний интерфейс, без потери данных и без обязательной ручной перенастройки. Обратимо.

## Точные факты из v3 OpenAPI (проверено)
- **User-объект v3** (ответ `POST/PATCH/GET /api/users`): `id:number`, `shortUuid:string`, `username`, `status`, `telegramId`, `email`, `tag`, `expireAt`, `trafficLimitBytes`, `trafficLimitStrategy`, `subscriptionUrl`, `activeInternalSquads[]`, `userTraffic` — **поля `uuid` НЕТ**.
- **`POST /api/users/resolve`**: request `{id?|shortUuid?|username?}` → response `{id, username, shortUuid}`. Мост для бэкфилла.
- **`GET /api/users/stream`**: query `cursor,size,status,trafficLimitStrategy,telegramId,email,tag,externalSquadUuid`; response `{users:[...], nextCursor, hasMore}` (курсорная пагинация).
- **Живы в v3:** `GET /api/users/by-username/{username}`, `GET /api/users/by-short-uuid/{shortUuid}`, `GET /api/subscriptions/by-id/{userId}`, `by-username`, `by-short-uuid`.
- **Удалены в v3:** `by-telegram-id`, `by-email`, `by-tag`, `by-id`; `bandwidth-stats/.../legacy`.
- **`ip-control` → `connections`**: `POST /api/connections/by-user/{userId}`, `POST /api/connections/drop`.
- **External squads:** `responseHeaders` → `responseHeadersAdd`(object)+`responseHeadersRemove`(array).
- **HTTP-коды:** DELETE→204, async bulk→202 (без `affectedRows`), create→201. Наш `_make_request` пустое тело переносит (JSONDecodeError→`{'raw_response':''}`), `affectedRows` мы не читаем → не блокер.
- **Auth:** без изменений для клиента (Bearer/X-Api-Key; токены v3 авто-мигрирует). Env-переименования (`JWT_AUTH_SECRET`→`APP_SECRET`) — на стороне панели, не наш код.
- **Узлы/сквады/inbound** сохранили `uuid` (v3 лишь добавил им числовой `id`) — их usage не трогаем.

## Что уже храним (БД)
- `User.remnawave_uuid` (models.py:2088, unique) — v2 user-UUID.
- `Subscription.subscription_url` (2305), `remnawave_short_uuid` (2353), `remnawave_uuid` (2354), `remnawave_short_id` (2355).
- ещё таблица с `remnawave_uuid NOT NULL` (2614) — проверить назначение (устройства/коннекты).
`remnawave_short_uuid` = `shortUuid`, стабилен в обеих версиях → основа бэкфилла.

## Архитектура

### 1. Определение версии панели
- Новый конфиг `REMNAWAVE_API_VERSION: str = 'auto'` (`auto`|`2`|`3`).
- `auto`: клиент один раз пробит версию и кэширует (на процесс). Проба — устойчивый маркер v3: например `GET /api/users/stream?size=1` (в v3 → 200 `{users,nextCursor,hasMore}`; в v2 эндпоинта нет → 404/иное) ИЛИ доступный version/health-эндпоинт панели. Точную пробу зафиксировать в плане по OpenAPI обеих версий; при неоднозначности — считать v2 (безопасный дефолт для текущих) и залогировать. Значение доступно как `client.api_version` (2|3).

### 2. БД: numeric id
- Добавить `User.remnawave_id` (BigInteger, nullable) — v3 numeric id. `remnawave_uuid` НЕ удаляем (v2 живёт на нём).
- Alembic-миграция в диапазоне `9xxx` (стиль форка). Только add column + index, без бэкфилла в миграции.
- (Опц.) `Subscription`: переиспользовать существующую `remnawave_short_id` под numeric id подписки, если применимо; иначе хранить id на User достаточно.

### 3. Клиент: инкапсуляция версии
Вся развилка — внутри `app/external/remnawave_api.py`. Метод-хелпер `_user_path_ref(*, uuid=None, remna_id=None)` возвращает сегмент пути: v2 → `uuid`, v3 → `str(remna_id)`. Публичные методы клиента расширяем так, чтобы принимать оба идентификатора (сигнатуры обратносовместимы — добавляем `remna_id: int | None = None` рядом с `uuid`), а вызыватели передают то, что есть в записи.
- **User-операции** (`get/delete/enable/disable/reset-traffic/revoke/subscription-request-history/accessible-nodes/PATCH`): путь через `_user_path_ref`; на v3 — `{id}`, на v2 — `{uuid}`.
- **Парсер user** (`RemnaWaveUser`): читать `id` (v3) / `uuid` (v2); всегда сохранять `short_uuid`, `subscription_url`. Внутренняя модель несёт оба идентификатора (`uuid: str|None`, `id: int|None`, `short_uuid`).
- **Lookup:**
  - by-telegram-id: v2 → `/api/users/by-telegram-id/{tg}`; v3 → `GET /api/users/stream?telegramId={tg}&size=1` (взять первый из `users`).
  - by-email: v2 → `/api/users/by-email/{email}`; v3 → `stream?email=`.
  - by-username / by-short-uuid: единый путь (живы в обеих).
- **resolve (v3):** `POST /api/users/resolve {shortUuid}` → `id` — для бэкфилла и точечного разрешения.
- **subscriptions/by-uuid → by-id** на v3.
- **connections (ip-control):** проверить наличие вызовов в клиенте/сервисах; на v3 путь `/api/connections/...`.
- **external squad responseHeaders:** на v3 отдавать `responseHeadersAdd`/`Remove`.

### 4. Бэкфилл numeric id (v3)
- Ленивый: при user-операции на v3, если у записи нет `remnawave_id`, но есть `remnawave_short_uuid` → `resolve(shortUuid)` → сохранить `remnawave_id`, продолжить операцию. Нет shortUuid → fallback `by-username`/`stream(telegramId)`.
- (Опц.) батч-задача синка: пройтись по активным юзерам без `remnawave_id`, разрешить и заполнить. Идемпотентно, best-effort.

### 5. Сопутствующие модули (проверить/поправить)
`app/services/remnawave_service.py`, `remnawave_sync_service.py`, `remnawave_retry_queue.py`, `app/webserver/remnawave_webhook.py` (какой идентификатор шлёт v3-вебхук — сверить с OpenAPI webhook-схемой/событиями), cabinet `app/cabinet/routes/admin_remnawave.py`, `app/webapi/routes/remnawave.py`. Задача — чтобы они работали через клиентский интерфейс, а не собирали пути с uuid напрямую.

## Обработка ошибок / крайние случаи
- Неопределённая версия (`auto` проба упала) → лог + дефолт v2; оператор может форсить `REMNAWAVE_API_VERSION=3`.
- v3-юзер без `remnawave_id` и без `remnawave_short_uuid` (битая запись) → lookup по telegramId/username; если не нашли — как «panel user missing» (наш существующий путь пересоздания).
- Не ломать v2: при `api_version==2` поведение и пути идентичны текущим (регресс-тесты на v2-путях).
- Смешанные данные (мигрировал на v3, но записи со старым uuid) — resolve/lookup закрывает.

## Тестирование
- **Unit (обе версии):** параметризовать по `api_version`.
  - Путь user-операций: v2 → `/api/users/{uuid}`, v3 → `/api/users/{id}` (мок `_make_request`, проверка URL).
  - Парсер: v3-ответ (`id`, без `uuid`) → внутренняя модель с `id`+`short_uuid`; v2-ответ (`uuid`) → с `uuid`.
  - Lookup by-telegram-id: v2 прямой эндпоинт; v3 → `stream?telegramId=` + разбор `{users:[...]}`.
  - resolve-бэкфилл: нет `remnawave_id`, есть shortUuid → `resolve` вызван, id сохранён.
  - Детект версии: проба возвращает v3-сигнатуру → `api_version==3`; ошибка пробы → v2.
- **Регресс:** существующие тесты RemnaWave на v2 зелёные без изменений.
- **py_compile + import-test** затронутых модулей.

## Вне объёма
- Полное удаление v2-пути (двойная совместимость сохраняется).
- Узлы/сквады/inbound uuid (не менялись).
- Панельные env/инфра (APP_SECRET и т.п.) — зона оператора.
- Новые v3-фичи (digest-стата, webhook-URLs, redis-streams) — отдельно при желании.
