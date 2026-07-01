# Broadcast: заблокировавшие бота с активной подпиской — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** По каждой Telegram-рассылке сохранять, кто заблокировал бота, и показывать (в кабинете и в боте) список тех из них, у кого **активная подписка** — с тарифом, датой окончания и остатком дней.

**Architecture:** Telegram-рассылка уже собирает `blocked_telegram_ids` (`broadcast_service.py:285,377`), но выбрасывает их. Сохраняем список в новую JSON-колонку `broadcast_history.blocked_user_ids`. По требованию считаем пересечение с активными подписками и отдаём таблицу.

**Tech Stack:** Python/FastAPI/SQLAlchemy async + Alembic; aiogram; React/TS cabinet.

## Global Constraints

- Два репо: бот `/Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot`, кабинет `/Users/mihail/Desktop/Serv/bedolaga-cabinet`. Прямой push в main разрешён.
- Коммиты описательные (заголовок+тело), БЕЗ `Co-Authored-By`.
- После правок .py: `.venv/bin/python3 -m py_compile` + pytest через `.venv/bin/python3` (системный python3 = 3.9, нет `datetime.UTC`).
- Alembic head = `0093`; новая ревизия `0094`, `down_revision='0093'`. Образец добавления колонки в broadcast_history: `migrations/alembic/versions/0006_add_broadcast_email_columns.py`.
- Хранимые id = **telegram_id** (получатели рассылки — telegram_id).
- «Активная подписка» = `Subscription.status == SubscriptionStatus.ACTIVE.value` И `end_date > now` (см. `Subscription.is_active`, models.py ~2182). Тариф — `Tariff.name` (models.py:1672). `Subscription.end_date` (models.py:2106), `Subscription.tariff_id` (2161).
- Колонки таблицы: telegram_id, username, email, тариф, дата окончания, осталось дней.

---

## Task 1: Модель + миграция (колонка blocked_user_ids)

**Files:**
- Modify: `app/database/models.py` (класс `BroadcastHistory`, ~строка 2990, рядом с `blocked_count`)
- Create: `migrations/alembic/versions/0094_add_broadcast_blocked_user_ids.py`
- Test: `tests/test_broadcast_blocked_column.py`

**Interfaces produces:** `BroadcastHistory.blocked_user_ids` (JSON, nullable) — список telegram_id.

- [ ] Step 1: В модели `BroadcastHistory` добавить (после `blocked_count`):
```python
    blocked_user_ids = Column(JSON, nullable=True)  # telegram_id, заблокировавшие бота в этой рассылке
```
Убедиться, что `JSON` импортирован из sqlalchemy в models.py (если нет — добавить в существующий импорт).

- [ ] Step 2: Миграция `0094_add_broadcast_blocked_user_ids.py` (по образцу 0006):
```python
"""add broadcast_history.blocked_user_ids

Revision ID: 0094
Revises: 0093
"""
from typing import Union
import sqlalchemy as sa
from alembic import op

revision: str = '0094'
down_revision: Union[str, None] = '0093'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('broadcast_history', sa.Column('blocked_user_ids', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('broadcast_history', 'blocked_user_ids')
```

- [ ] Step 3: Тест — колонка есть в модели:
```python
def test_broadcast_history_has_blocked_user_ids():
    from app.database.models import BroadcastHistory
    assert 'blocked_user_ids' in BroadcastHistory.__table__.columns
```
- [ ] Step 4: `.venv/bin/python3 -m pytest tests/test_broadcast_blocked_column.py -v` (pass); `py_compile` модели и миграции.
- [ ] Step 5: Commit `feat(broadcast): колонка blocked_user_ids в broadcast_history (+миграция 0094)`.

---

## Task 2: Сохранять blocked_telegram_ids в рассылке

**Files:**
- Modify: `app/services/broadcast_service.py` (`_send_batched` ~строка 280-398, `_run_broadcast` ~200-234, `_safe_status_update`/`_mark_finished` ~448-540)
- Test: `tests/cabinet/test_broadcast_persists_blocked.py`

**Interfaces:** после завершения Telegram-рассылки `broadcast_history.blocked_user_ids` содержит список telegram_id заблокировавших.

- [ ] Step 1: `_send_batched` уже накапливает `blocked_telegram_ids` (строка 285/377). Изменить его возврат с `(sent, failed, blocked, was_cancelled)` на `(sent, failed, blocked, was_cancelled, blocked_telegram_ids)`. Обновить распаковку в `_run_broadcast` (строка ~200).
- [ ] Step 2: В `_run_broadcast` при финализации передать список в запись. Проще всего — прямо перед/во время `_mark_finished` записать в БД:
```python
async with AsyncSessionLocal() as session:
    bh = await session.get(BroadcastHistory, broadcast_id)
    if bh is not None:
        bh.blocked_user_ids = blocked_telegram_ids
        await session.commit()
```
(выполнять только для telegram/both каналов; для чистого email — пропустить). Разместить до `_mark_finished`.
- [ ] Step 3: Тест: замокать отправку так, чтобы часть telegram_id вернула 'blocked', и проверить, что после `_run_broadcast`/`_send_batched` список блокировок содержит нужные id. Если полный прогон сложен — юнит на `_send_batched` возврат (что 5-й элемент = список заблокированных). Использовать существующие моки бота.
- [ ] Step 4: pytest + py_compile.
- [ ] Step 5: Commit `feat(broadcast): сохранять telegram_id заблокировавших бота в blocked_user_ids`.

---

## Task 3: Запрос «заблокировали + активная подписка»

**Files:**
- Modify: `app/database/crud/` (новый модуль `broadcast_reports.py` ИЛИ функция в существующем broadcast crud — выбрать по месту; предпочесть новый файл `app/database/crud/broadcast_reports.py`)
- Test: `tests/crud/test_broadcast_blocked_report.py`

**Interfaces produces:**
`get_broadcast_blocked_active_subscribers(db, broadcast_id) -> list[dict]` — по `broadcast_history.blocked_user_ids` (telegram_id) join `users` (по telegram_id) + активная `subscriptions`, вернуть отсортированный по остатку дней список:
`{'telegram_id','username','email','tariff_name','end_date'(iso),'days_left'(int)}`. Только активные подписки (`status=='active' and end_date>now`).

- [ ] Step 1: Failing-тест (реальный SQLite-харнесс, как в `tests/crud/test_google_migration_crud.py` — переиспользовать паттерн `_patch_jsonb_for_sqlite`, но здесь нужны таблицы User, Subscription, Tariff, BroadcastHistory — создать их `.__table__.create`). Сценарий: рассылка с blocked_user_ids=[100,200,300]; юзер tg=100 с активной подпиской (end через 30д) → в списке с days_left≈30; tg=200 с истёкшей → исключён; tg=300 не заблокирован/без подписки → исключён.
- [ ] Step 2: Реализация: прочитать `blocked_user_ids`; если пусто → `[]`; `select(User, Subscription, Tariff)` join по `User.telegram_id.in_(ids)`, `Subscription.user_id==User.id`, `Subscription.status=='active'`, `Subscription.end_date>now`, `Tariff.id==Subscription.tariff_id (outerjoin)`; `days_left = (end_date - now).days`. Вернуть список dict, sort по days_left.
- [ ] Step 3-5: pytest, py_compile, commit `feat(broadcast): запрос заблокировавших бота с активной подпиской`.

---

## Task 4: Cabinet endpoint + таблица в истории рассылок

**Files:**
- Modify: `app/cabinet/routes/admin_broadcasts.py` (+ эндпоинт)
- Modify: `bedolaga-cabinet/src/api/adminBroadcasts.ts` (+ метод)
- Modify: `bedolaga-cabinet/src/pages/AdminBroadcasts.tsx` (кнопка/секция + таблица в записи истории)
- Modify: `bedolaga-cabinet/src/locales/ru.json`, `en.json`
- Test: `tests/cabinet/test_broadcast_blocked_route.py`

**Interfaces:** `GET /cabinet/admin/broadcasts/{id}/blocked-active` (require_permission('broadcasts:read')) → `{count, users:[...]}` из Task 3.

- [ ] Step 1: Эндпоинт (по образцу соседних в admin_broadcasts.py) + тест регистрации/вызова (мок CRUD), как в `tests/cabinet/test_admin_google_migration_routes.py`.
- [ ] Step 2: Фронт: `adminBroadcastsApi.getBlockedActive(id)`; в истории рассылок у Telegram-записи с `blocked_count>0` — раскрывающаяся секция/кнопка «Заблокировали с активной подпиской (N)» → таблица (username/id, email, тариф, дата окончания, осталось дней), сортировка по дням. Классы — как на существующих страницах (свериться).
- [ ] Step 3: Локали ru+en для заголовков/колонок.
- [ ] Step 4: `npx tsc --noEmit` + `npm run build`; бэкенд pytest+py_compile.
- [ ] Step 5: Commit в оба репо раздельно (описательно).

---

## Task 5: Бот — счётчик + топ-N в итоге рассылки

**Files:**
- Modify: `app/handlers/admin/messages.py` (место, где показывается итог рассылки — рядом с выводом `blocked_count`, ~строки 172-220) ИЛИ метод сервиса, шлющий админу итог. Найти фактический рендер результата (progress/finish) и дополнить.
- Test: расширить/добавить в `tests/handlers/` при возможности; минимум py_compile + import-test.

**Interfaces:** в финальном сообщении о рассылке добавляется строка `🚫 Заблокировали с активной подпиской: Y` и, если Y>0, топ-10: `@username — <тариф>, ещё N дн.` + при усечении «…полный список в кабинете».

- [ ] Step 1: Найти, где бот формирует итог рассылки с `blocked_count`. После завершения (status completed/partial) вызвать `get_broadcast_blocked_active_subscribers(db, broadcast_id)` (Task 3), вывести Y = len и топ-10 (sort по days_left). Ограничить длину сообщения.
- [ ] Step 2: py_compile + import-test модулей с `register_handlers`. Прогнать смоук, что рендер не падает при пустом списке.
- [ ] Step 3: Commit `feat(broadcast): в итоге рассылки показывать заблокировавших с активной подпиской (бот)`.

---

## Self-Review
- Покрытие: сохранение id (Task1,2), запрос активных подписчиков (Task3), кабинет-таблица (Task4), бот-итог (Task5). Колонки: username/id, email, тариф, дата, дни — во всех точках.
- Работает для НОВЫХ рассылок (старые blocked_user_ids=NULL → список пуст, count 0). Это ожидаемо (историю блокировок раньше не сохраняли).
- Только Telegram-канал даёт «заблокировал бота»; email-рассылки не затрагиваются.
