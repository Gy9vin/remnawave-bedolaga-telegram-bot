# Совместимость RemnaWave v2 ↔ v3 — план

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Бот одинаково работает с панелью RemnaWave v2 (UUID) и v3.1.0 (numeric id) — автоопределение версии, вся развилка спрятана в клиенте, v2 без регресса.

**Architecture:** Клиент определяет версию панели и инкапсулирует различия. v2 = путь по user-UUID; v3 = путь по numeric `id` (парсер читает `id`+`shortUuid`, без `uuid`), удалённые lookup'ы заменены на `/api/users/stream`, numeric id бэкфиллится через `POST /api/users/resolve {shortUuid}`. Новая nullable-колонка `User.remnawave_id`.

**Tech Stack:** Python/aiohttp-клиент, SQLAlchemy-async, Alembic. Спека: `docs/superpowers/specs/2026-08-02-remnawave-v3-compat-design.md`. OpenAPI v3.1.0 (точные схемы): `/private/tmp/claude-501/-Users-mihail-Desktop-Serv-remnawave-bedolaga-telegram-bot/51fe188b-49c5-47a2-a076-66f1e20ae1cf/scratchpad/rw-openapi.json`.

## Global Constraints
- **v2 без регресса:** при `api_version==2` пути/парсинг/поведение идентичны текущим. Каждая задача добавляет v3-ветку, НЕ меняя v2-ветку. Обязателен параметризованный тест v2-пути.
- **Инкапсуляция в клиенте:** развилка версий — внутри `app/external/remnawave_api.py`; вызыватели не должны собирать `/api/users/...` пути с идентификатором напрямую.
- **Идентификаторы v3 (из OpenAPI, verbatim):** user-объект `id:number`, `shortUuid:string`, `username`, `telegramId`, `email`, `subscriptionUrl`; поля `uuid` нет. `POST /api/users/resolve {id?|shortUuid?|username?}` → `{id, username, shortUuid}`. `GET /api/users/stream?telegramId=&email=&tag=&status=&size=&cursor=` → `{users:[...], nextCursor, hasMore}`. Живы: `by-username/{username}`, `by-short-uuid/{shortUuid}`, `subscriptions/by-id/{userId}`. Удалены: `by-telegram-id`, `by-email`, `by-tag`, `by-id`, `bandwidth-stats/.../legacy`.
- Bot-тесты: `.venv/bin/python3 -m pytest`. После правок `.py` — `py_compile` + import-test затронутых модулей (`BACKUP_LOCATION=/tmp/test_backups .venv/bin/python3 -c "import <module>"`).
- Публичный репо — без секретов. Коммиты на русском, БЕЗ `Co-Authored-By`.
- Alembic-миграция в стиле форка (диапазон `9xxx`), только add-column (+index), без бэкфилла в самой миграции.

---

### T1 — Определение версии панели + конфиг

**Files:** Modify `app/config.py` (+ `REMNAWAVE_API_VERSION`), `app/external/remnawave_api.py` (детект+кэш). Test: `tests/external/test_remnawave_version_detect.py`.

**Interfaces:** Produces `RemnaWaveAPI.api_version` (int 2|3) и `async def _detect_api_version(self) -> int` (кэш на инстанс).

**Steps (TDD):**
1. Конфиг `REMNAWAVE_API_VERSION: str = 'auto'` (значения `auto|2|3`) + геттер `settings.get_remnawave_api_version() -> str`.
2. Провальный тест: (a) `REMNAWAVE_API_VERSION='2'|'3'` → `api_version` == форс; (b) `auto` + проба возвращает v3-сигнатуру (`GET /api/users/stream?size=1` → 200 с `{users,...}`) → 3; (c) `auto` + проба 404/ошибка → 2 (безопасный дефолт) + warning-лог. Мокать `_make_request`.
3. Реализация: `_detect_api_version` — если конфиг форсит, вернуть его; иначе пробовать `stream?size=1` (200 + ключ `users`/`hasMore` → 3; 404/иное → 2), результат кэшировать в `self._api_version`. Проба лениво при первом обращении (или в первом `_make_request`), не в `__init__` (чтобы не делать сеть на конструировании).
4. `.venv/bin/python3 -m pytest tests/external/test_remnawave_version_detect.py -q` — RED→GREEN. py_compile + import-test.
5. Коммит.

### T2 — Миграция БД: User.remnawave_id

**Files:** Modify `app/database/models.py` (User + `remnawave_id`), Create `alembic/versions/9xxx_user_remnawave_id.py` (путь по факту в репо). Test: `tests/database/test_remnawave_id_column.py` (или проверка модели/апгрейда).

**Interfaces:** Produces `User.remnawave_id: int | None` (BigInteger, nullable, index).

**Steps (TDD):**
1. Найти текущий head миграции (`alembic heads` / последний файл `9xxx_`), взять корректный `down_revision`; сверить merge-head паттерн форка.
2. Провальный тест: модель `User` имеет атрибут `remnawave_id`, дефолт None.
3. Добавить `remnawave_id = Column(BigInteger, nullable=True, index=True)` в модель `User` (рядом с `remnawave_uuid`, models.py:2088). Написать миграцию: `op.add_column('users', sa.Column('remnawave_id', sa.BigInteger(), nullable=True))` + индекс; `downgrade` — drop.
4. Прогнать тест + (если есть) миграционный смоук. py_compile.
5. Коммит.

### T3 — Клиент: единый идентификатор пути + парсер (v2/v3)

**Files:** Modify `app/external/remnawave_api.py`. Test: `tests/external/test_remnawave_user_path_dual.py`.

**Interfaces:**
- Produces `def _resolve_user_path(self, *, uuid=None, remna_id=None) -> str` (v2→uuid, v3→str(remna_id); если на v3 нет remna_id → бросить понятную ошибку/None, которую вызыватель закрывает бэкфиллом в T4).
- Внутренняя модель `RemnaWaveUser` несёт `uuid: str|None`, `id: int|None`, `short_uuid`, `subscription_url`.
- User-методы (`get_user_by_uuid`→оставить имя, `delete_user`, `enable_user`, `disable_user`, `reset_user_traffic`, `revoke_user_subscription`, `update_user`, `get_user_accessible_nodes`, `get_user_subscription_request_history`, `get_user_stats_usage`, `get_user_devices*`) получают доп-параметр `remna_id: int | None = None` (сигнатуры обратносовместимы; v2-вызовы работают как раньше).

**Steps (TDD):**
1. Провальные тесты (параметризовать `api_version` 2 и 3, мок `_make_request`, проверять URL и разбор):
   - v2: `get_user_by_uuid(uuid='U')` → путь `/api/users/U`; парсер ответа с `uuid` → `RemnaWaveUser.uuid=='U'`.
   - v3: `get_user_by_uuid(uuid=None, remna_id=42)` → путь `/api/users/42`; парсер v3-ответа (`id`, `shortUuid`, без `uuid`) → `.id==<n>`, `.short_uuid` заполнен.
   - `enable_user`/`disable_user`/`delete_user`/`reset_user_traffic`/`update_user`/`revoke` — путь через `_resolve_user_path` для обеих версий.
2. Реализация `_resolve_user_path` + прокидка `remna_id` во все user-методы (пути через хелпер). Парсер `_parse_user`/`RemnaWaveUser` читает `id` (v3) и `uuid` (v2), всегда `short_uuid`/`subscription_url`. `update_user` (PATCH /api/users): в теле на v3 слать `id`, на v2 — как сейчас (uuid).
3. `.venv/bin/python3 -m pytest tests/external/test_remnawave_user_path_dual.py -q` — RED→GREEN. Прогнать существующие remnawave-тесты (v2-регресс) — зелёные. py_compile + import.
4. Коммит.

### T4 — Lookup через stream + resolve + ленивый бэкфилл

**Files:** Modify `app/external/remnawave_api.py`. Test: `tests/external/test_remnawave_lookup_resolve.py`.

**Interfaces:**
- `async def resolve_user_id(self, *, short_uuid=None, username=None) -> int | None` (v3: `POST /api/users/resolve`; v2: None/не применимо).
- `get_user_by_telegram_id` / `get_user_by_email`: v2 — как сейчас; v3 — через `GET /api/users/stream?telegramId=|email=&size=1`, разобрать `{users:[...]}`.
- `subscriptions/by-uuid` → `by-id` на v3.

**Steps (TDD):**
1. Провальные тесты: v3 `get_user_by_telegram_id(tg)` → зовёт `stream?telegramId=tg`, возвращает список из `users`; v2 — старый эндпоинт. `resolve_user_id(short_uuid='S')` на v3 → `POST /api/users/resolve` с `{shortUuid:'S'}`, вернуть `id`. На v2 `resolve_user_id` → None.
2. Реализация lookups (развилка) + `resolve_user_id`. `by-username`/`by-short-uuid` — единый путь.
3. RED→GREEN + регресс v2. py_compile + import.
4. Коммит.

### T5 — Миграция call-sites: ядро сервисов

**Files:** Modify `app/services/remnawave_service.py`, `app/services/subscription_service.py`, `app/services/remnawave_sync_service.py`, `app/services/remnawave_retry_queue.py`. Test: соответствующие сервис-тесты + новый на бэкфилл.

**Interfaces:** Consumes T3/T4. Добавить хелпер `async def get_panel_user_ref(client, *, user=None, subscription=None) -> tuple[str|None,int|None]` (в `remnawave_service` или utils): возвращает `(uuid, remna_id)`; на v3, если `remna_id` пуст, но есть `short_uuid` (из subscription/user) → `resolve_user_id` → сохранить `user.remnawave_id` в БД (ленивый бэкфилл) → вернуть.

**Steps (TDD):**
1. Провальный тест бэкфилла: v3, у User нет `remnawave_id`, есть `remnawave_short_uuid` → при user-операции `resolve_user_id` вызван, `user.remnawave_id` сохранён, дальнейший путь — `/api/users/{id}`.
2. В этих сервисах заменить вызовы `client.<op>(user.remnawave_uuid)` → передавать `remna_id`/резолвить через `get_panel_user_ref` (на v2 поведение неизменно — uuid). Особое внимание: создание юзера (`create_user`) — на v3 сохранить возвращённый `id` в `user.remnawave_id` и `short_uuid` в подписку.
3. `bandwidth-stats/.../legacy` (`remnawave_service.py:3032`) → на v3 использовать не-legacy вариант (сверить путь в OpenAPI: `/api/bandwidth-stats/nodes/{uuid}/users`); v2 — как есть.
4. RED→GREEN + регресс. py_compile + import.
5. Коммит.

### T6 — Миграция call-sites: хендлеры/кабинет/webapi + вебхук

**Files:** Modify user-uuid call-sites в `app/handlers/**`, `app/cabinet/routes/admin_remnawave.py`, `app/webapi/routes/remnawave.py`; вебхук `app/webserver/remnawave_webhook.py` + `app/services/*webhook*` (разбор идентификатора). Test: точечные + вебхук-тест.

**Steps (TDD):**
1. Инвентарь оставшихся call-sites этих модулей (`grep -rn "\.get_user_by_uuid(\|\.enable_user(\|\.disable_user(\|\.delete_user(\|\.reset_user_traffic(\|\.update_user(\|\.revoke_user_subscription(" app/handlers app/cabinet app/webapi`), мигрировать на `get_panel_user_ref`/`remna_id`.
2. **Вебхук:** найти, какой идентификатор шлёт v3-payload (сверить с OpenAPI webhook-схемой/событиями; вероятно `id`/`telegramId`/`shortUuid` вместо `uuid`) в `webhook_service.process_event`; научить разбирать оба (v2 uuid, v3 id/tg) и находить нашего юзера. Провальный тест на v3-payload.
3. RED→GREEN + регресс v2. py_compile + import.
4. Коммит.

### T7 — Периферия v3 + опц. батч-бэкфилл

**Files:** Modify `app/services/happ_management/squad_manager.py` + `remnawave_sync.py` (external-squad `responseHeaders`→`responseHeadersAdd`/`Remove` на v3); (опц.) админ-триггер батч-синка numeric id. Test: точечные.

**Steps (TDD):**
1. Провальный тест: на v3 создание/обновление external squad шлёт `responseHeadersAdd`(object)+`responseHeadersRemove`(array); на v2 — прежний `responseHeaders`.
2. Реализация развилки в happ_management. (Опц.) батч-бэкфилл: разово пройтись по активным юзерам без `remnawave_id`, `resolve_user_id` по `short_uuid`, сохранить — best-effort, идемпотентно (админ-эндпоинт/команда).
3. RED→GREEN + регресс. py_compile + import.
4. Коммит.

---

## Порядок выполнения
T1→T2→T3→T4→T5→T6→T7. Каждая — свежий имплементер + ревьюер; после всех — финальное ревью ветки. Мерж в main.

## Self-Review
- Спека покрыта: детект(T1), колонка(T2), клиент-путь/парсер(T3), lookup/resolve/бэкфилл(T4), call-sites ядра(T5), хендлеры/кабинет/вебхук(T6), периферия(T7).
- v2-регресс — обязательный тест в каждой задаче с версией.
- Имена согласованы: `api_version`, `_resolve_user_path`, `resolve_user_id`, `get_panel_user_ref`, `User.remnawave_id`.
