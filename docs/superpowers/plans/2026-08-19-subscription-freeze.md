# План: Заморозка подписки

**Goal:** Реализовать механизм временной приостановки подписки (заморозки): VPN отключается, дни не тратятся, при разморозке оставшиеся дни возвращаются через сдвиг `end_date`.

**Architecture:** Флаг `is_frozen` на модели `Subscription` вместо нового статуса; `status = DISABLED` при заморозке. Бэкенд — Python/SQLAlchemy + FastAPI (бот-сервер); фронтенд — React/TypeScript (кабинет). Три независимых пути разморозки: кабинет по почте, Telegram-бот, авто-разморозка кроном.

**Tech Stack:** Python 3.11, SQLAlchemy 2 (asyncpg), Alembic, aiogram 3, FastAPI, React 18, TypeScript, Tanstack Query, react-i18next.

**Spec:** `docs/superpowers/specs/2026-08-19-subscription-freeze-design.md`

---

## Global Constraints

- Фича за флагом `settings.FREEZE_SUBSCRIPTIONS_ENABLED` (default `False`); без него всё поведение unchanged.
- После правок `.py` — `python3 -m py_compile <файл>` + import-тест затронутых модулей.
- После правок кабинета — `npx tsc --noEmit` + `npm test` зелёные.
- Миграция строго по паттерну `_OUR_MIGRATION_PATTERN`: числовой префикс `9030`, файл `9030_subscription_freeze.py`, `Revision ID: 9030`, `Revises: 9029`.
- Коммиты на русском (заголовок + тело), без Co-Authored-By.
- Паритет ключей во всех 4 локалях кабинета: `ru.json`, `en.json`, `zh.json`, `fa.json`.
- `unfreeze_subscription` принимает `reason: Literal['manual', 'auto', 'admin']` — три значения (спека §12 явно указывает `reason='admin'` для admin-пути, хотя §5.2 упоминает только `manual`/`auto`; устраняем нестыковку здесь).

---

## File Structure

### Репозиторий бота (`/Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot`)

| Файл | Статус | Ответственность |
|------|--------|----------------|
| `migrations/alembic/versions/9030_subscription_freeze.py` | **Create** | Alembic-миграция: 4 новых колонки + индекс |
| `app/database/models.py` | **Modify** (~2397) | Добавить 4 freeze-поля к классу `Subscription` |
| `app/config.py` | **Modify** (~84) | Добавить `FREEZE_SUBSCRIPTIONS_ENABLED`, `FREEZE_MAX_DAYS`, `FREEZE_MIN_DAYS_REMAINING` |
| `app/services/subscription_service.py` | **Modify** (после ~1108) | Добавить `_validate_freeze_preconditions`, `freeze_subscription`, `unfreeze_subscription`; добавить `FreezeNotAllowedError` |
| `app/services/subscription_auto_purchase_service.py` | **Modify** (~494, ~2490) | Ранние возвраты при `is_frozen` в `_auto_extend_subscription` и `try_auto_extend_expired_after_topup` |
| `app/database/crud/subscription.py` | **Modify** (~1824) | Добавить `Subscription.is_frozen == False` в `get_expired_subscriptions`; добавить `get_subscriptions_for_auto_unfreeze` |
| `app/services/monitoring_service.py` | **Modify** (~442) | Добавить `_check_frozen_subscriptions_for_auto_unfreeze`; вызов в `_monitoring_cycle` |
| `app/services/notification_delivery_service.py` | **Modify** (~40, ~517) | Добавить `SUBSCRIPTION_FROZEN`/`SUBSCRIPTION_UNFROZEN` в `NotificationType`; методы `notify_subscription_frozen`/`unfrozen` |
| `app/webapi/schemas/miniapp.py` | **Modify** (~56, ~700) | Добавить freeze-поля в `MiniAppSubscriptionData`; добавить `MiniAppSubscriptionFreezeRequest`, `MiniAppSubscriptionFreezeResponse`, `MiniAppSubscriptionUnfreezeResponse` |
| `app/cabinet/schemas/subscription.py` | **Modify** (~76) | Добавить 3 freeze-поля в `SubscriptionData` |
| `app/webapi/routes/miniapp.py` | **Modify** (после ~7500) | Добавить `freeze_subscription_endpoint`, `unfreeze_subscription_endpoint` |
| `app/handlers/subscription/freeze.py` | **Create** | Бот-хендлеры: confirm-диалог заморозки, подтверждение, разморозка |
| `app/keyboards/inline.py` | **Modify** (~1231) | Добавить кнопку «Заморозить»/«Разморозить» в `get_subscription_keyboard` |
| `app/handlers/admin/subscriptions.py` | **Modify** (конец файла) | Добавить admin freeze/unfreeze хендлеры |
| `tests/services/test_freeze.py` | **Create** | Юнит-тесты сервисного слоя заморозки |
| `tests/api/test_freeze_endpoints.py` | **Create** | Интеграционные тесты API freeze/unfreeze |

### Репозиторий кабинета (`/Users/mihail/Desktop/Serv/bedolaga-cabinet`)

| Файл | Статус | Ответственность |
|------|--------|----------------|
| `src/api/subscription.ts` | **Modify** (~824) | Добавить `freeze`, `unfreeze` методы в `subscriptionApi` |
| `src/types/index.ts` | **Modify** (~100) | Добавить `is_frozen`, `frozen_days_banked`, `frozen_auto_unfreeze_at` в `Subscription` |
| `src/components/simple/SimpleSubscription.tsx` | **Modify** | Кнопка/CTA заморозки, модал confirm, блок состояния «заморожена» |
| `src/components/simple/SimpleDashboard.tsx` | **Modify** | Hero-статус «Заморожена», скрыть «Подключить устройство» при is_frozen |
| `src/locales/ru.json` | **Modify** | Добавить freeze-ключи (ru) |
| `src/locales/en.json` | **Modify** | Добавить freeze-ключи (en) |
| `src/locales/zh.json` | **Modify** | Добавить freeze-ключи (zh) |
| `src/locales/fa.json` | **Modify** | Добавить freeze-ключи (fa) |

---

## Задачи

---

### Задача 1 — Alembic-миграция `9030_subscription_freeze`

**Результат:** миграция применяется без ошибок, `upgrade`/`downgrade` работают, существующие строки имеют `is_frozen = false`.

**Files:**
- Create: `migrations/alembic/versions/9030_subscription_freeze.py`

**Interfaces:**
- Produces: таблица `subscriptions` + колонки `is_frozen BOOL NOT NULL DEFAULT false`, `frozen_at TIMESTAMPTZ`, `frozen_days_banked INT`, `frozen_auto_unfreeze_at TIMESTAMPTZ`; индекс `ix_subscriptions_frozen_auto_unfreeze (is_frozen, frozen_auto_unfreeze_at)`

**Шаги:**
1. Создать файл `migrations/alembic/versions/9030_subscription_freeze.py` с шапкой:
   ```python
   """Add subscription freeze fields
   Revision ID: 9030
   Revises: 9029
   Create Date: 2026-08-19
   """
   from alembic import op
   import sqlalchemy as sa
   ```
2. Написать `upgrade()` — `op.add_column` × 4 + `op.create_index('ix_subscriptions_frozen_auto_unfreeze', 'subscriptions', ['is_frozen', 'frozen_auto_unfreeze_at'])`.
3. Написать `downgrade()` — `op.drop_index` + `op.drop_column` × 4 в обратном порядке.
4. Прогнать `alembic upgrade head` на тестовой БД, убедиться в `OK`; затем `alembic downgrade -1` — `OK`.
5. Проверить: `SELECT is_frozen FROM subscriptions LIMIT 5` → все `false`.
6. `python3 -m py_compile migrations/alembic/versions/9030_subscription_freeze.py`
7. Коммит: «feat(миграция): 9030 — поля заморозки подписки (is_frozen, frozen_at, frozen_days_banked, frozen_auto_unfreeze_at)».

---

### Задача 2 — Freeze-поля в модели `Subscription`

**Результат:** ORM-модель знает о четырёх freeze-полях; `py_compile` чист.

**Files:**
- Modify: `app/database/models.py` (вставить после строки ~2397, рядом с `is_daily_paused`)

**Interfaces:**
- Produces: атрибуты `Subscription.is_frozen`, `.frozen_at`, `.frozen_days_banked`, `.frozen_auto_unfreeze_at`

**Шаги:**
1. В классе `Subscription` (строка 2394–2397, рядом с `is_daily_paused`) добавить:
   ```python
   # Freeze fields
   is_frozen = Column(Boolean, nullable=False, default=False, server_default='false')
   frozen_at = Column(AwareDateTime(), nullable=True)
   frozen_days_banked = Column(Integer, nullable=True)
   frozen_auto_unfreeze_at = Column(AwareDateTime(), nullable=True)
   ```
2. `python3 -m py_compile app/database/models.py`
3. Быстрый import-тест: `python3 -c "from app.database.models import Subscription; print(Subscription.is_frozen)"`
4. Коммит: «feat(модель): freeze-поля подписки (is_frozen, frozen_at, frozen_days_banked, frozen_auto_unfreeze_at)».

---

### Задача 3 — Конфиг `FREEZE_*`

**Результат:** три новые настройки доступны через `settings.*`; модуль компилируется.

**Files:**
- Modify: `app/config.py` (класс `Settings`, строка ~84)

**Interfaces:**
- Produces: `settings.FREEZE_SUBSCRIPTIONS_ENABLED: bool`, `settings.FREEZE_MAX_DAYS: int`, `settings.FREEZE_MIN_DAYS_REMAINING: int`

**Шаги:**
1. В класс `Settings` добавить три поля (рядом с другими флагами фич):
   ```python
   # Заморозка подписки
   FREEZE_SUBSCRIPTIONS_ENABLED: bool = False
   FREEZE_MAX_DAYS: int = 60
   FREEZE_MIN_DAYS_REMAINING: int = 3
   ```
2. `python3 -m py_compile app/config.py`
3. `python3 -c "from app.config import settings; assert settings.FREEZE_MAX_DAYS == 60"`
4. Коммит: «feat(конфиг): FREEZE_SUBSCRIPTIONS_ENABLED, FREEZE_MAX_DAYS, FREEZE_MIN_DAYS_REMAINING».

---

### Задача 4 — `FreezeNotAllowedError` + `_validate_freeze_preconditions` + `freeze_subscription` + `unfreeze_subscription`

**Результат:** четыре публичных/приватных объекта в `SubscriptionService`; тест падает → реализация → тест зелёный.

**Files:**
- Modify: `app/services/subscription_service.py` (добавить после строки ~1108)
- Create: `tests/services/test_freeze.py`

**Interfaces:**
- Consumes: `Subscription.is_frozen`, `.frozen_at`, `.frozen_days_banked`, `.frozen_auto_unfreeze_at`, `.days_left` (models.py:2506); `disable_remnawave_user(panel_user_id, db)` (строка 1066); `enable_remnawave_user(panel_user_id, db)` (строка 1108); `notification_delivery_service.notify_subscription_frozen/unfrozen`
- Produces:
  - `class FreezeNotAllowedError(Exception): reason: str`
  - `async def _validate_freeze_preconditions(self, user, subscription) -> None`
  - `async def freeze_subscription(self, user: User, subscription: Subscription, db: AsyncSession) -> None`
  - `async def unfreeze_subscription(self, user: User, subscription: Subscription, db: AsyncSession, reason: Literal['manual', 'auto', 'admin'] = 'manual') -> None`

**Шаги:**
1. Написать `tests/services/test_freeze.py` с тестами (используя `MagicMock`/`AsyncMock`):
   - `test_freeze_success` — проверить, что после `freeze_subscription` у мок-подписки `is_frozen=True`, `status=DISABLED`, `frozen_days_banked` == `days_left` до заморозки.
   - `test_freeze_already_frozen` — `FreezeNotAllowedError` с `reason='already_frozen'`.
   - `test_freeze_email_not_verified` — `FreezeNotAllowedError(reason='email_not_verified')`.
   - `test_freeze_too_few_days` — `FreezeNotAllowedError(reason='too_few_days')`.
   - `test_freeze_invalid_status` — `FreezeNotAllowedError(reason='invalid_status')` для `status=EXPIRED`.
   - `test_freeze_trial_not_allowed` — `FreezeNotAllowedError(reason='trial_not_allowed')`.
   - `test_freeze_daily_paused` — `FreezeNotAllowedError(reason='daily_paused')`.
   - `test_freeze_in_grace` — `FreezeNotAllowedError(reason='in_grace')`.
   - `test_freeze_disabled` — `FreezeNotAllowedError(reason='freeze_disabled')` при `FREEZE_SUBSCRIPTIONS_ENABLED=False`.
   - `test_unfreeze_manual` — `end_date` сдвигается на `frozen_duration`, поля обнуляются, `status=ACTIVE`.
   - `test_unfreeze_auto` — аналогично с `reason='auto'`.
   - `test_unfreeze_idempotent` — при `is_frozen=False` нет исключения, нет изменений.
2. Прогнать: `pytest tests/services/test_freeze.py` → все FAILED (ImportError приемлем).
3. Реализовать в `subscription_service.py`:
   ```python
   class FreezeNotAllowedError(Exception):
       def __init__(self, reason: str):
           self.reason = reason
           super().__init__(reason)
   ```
   Метод `_validate_freeze_preconditions(self, user, subscription)` — 8 предусловий из спеки §6 в порядке: `freeze_disabled` → `invalid_status` → `trial_not_allowed` → `too_few_days` → `already_frozen` → `daily_paused` → `in_grace` → `email_not_verified`.

   Метод `freeze_subscription` — последовательность §5.1: валидация → присвоить поля → `db.flush()` → `disable_remnawave_user` → `notify_subscription_frozen`.

   Метод `unfreeze_subscription(reason: Literal['manual', 'auto', 'admin'] = 'manual')` — §5.2: ранний возврат если `not is_frozen` → вычислить `frozen_duration` (защита: если `frozen_at is None` — логировать, пропустить сдвиг) → сдвиг `end_date` → обнулить поля → `db.flush()` → `enable_remnawave_user` → `notify_subscription_unfrozen(reason=reason)`.

4. `python3 -m py_compile app/services/subscription_service.py`
5. Прогнать: `pytest tests/services/test_freeze.py` → все PASSED.
6. Коммит: «feat(сервис): FreezeNotAllowedError, _validate_freeze_preconditions, freeze_subscription, unfreeze_subscription».

---

### Задача 5 — Пропуск автопродления при заморозке

**Результат:** `_auto_extend_subscription` и `try_auto_extend_expired_after_topup` возвращают без продления при `is_frozen=True`.

**Files:**
- Modify: `app/services/subscription_auto_purchase_service.py` (строки ~494, ~2490)

**Interfaces:**
- Consumes: `subscription.is_frozen: bool`
- Produces: при `is_frozen=True` — `logger.debug(...)`, `return` / `return False`

**Шаги:**
1. В `_auto_extend_subscription` (строка ~494), в самом начале тела функции (до `try:`) вставить:
   ```python
   # Не продлевать замороженную подписку
   if getattr(cart_data.get('subscription') if isinstance(cart_data, dict) else None, 'is_frozen', False):
       logger.debug('Автопродление пропущено: подписка заморожена')
       return False
   ```
   Уточнить: `_auto_extend_subscription` принимает `cart_data: dict` — получить подписку через `cart_data` или загрузить по id; в зависимости от того, как передаётся объект. Если подписка — ORM-объект в `cart_data['subscription']`, использовать `cart_data['subscription'].is_frozen`. Если `cart_data` — просто dict с id — загрузить из БД и проверить. Использовать наиболее прямой вариант.

   Более надёжный вариант — добавить проверку в `_prepare_auto_extend_context` (строка ~221):
   ```python
   if subscription.is_frozen:
       logger.debug('Автопродление пропущено: подписка заморожена', subscription_id=subscription.id)
       return None  # или raise специфичное исключение
   ```
   и обработать `None` в вызывающей стороне.

2. В `try_auto_extend_expired_after_topup` (строка ~2507, после получения `subscription`):
   ```python
   if subscription is not None and getattr(subscription, 'is_frozen', False):
       logger.debug('Автопродление-topup пропущено: подписка заморожена', user_id=user.id)
       return False
   ```
3. `python3 -m py_compile app/services/subscription_auto_purchase_service.py`
4. В `tests/services/test_freeze.py` добавить:
   - `test_auto_extend_skipped_when_frozen` — мокировать `_prepare_auto_extend_context`, убедиться что при `is_frozen=True` `_auto_extend_subscription` возвращает без списания.
   - `test_topup_extend_skipped_when_frozen` — при `subscription.is_frozen=True` `try_auto_extend_expired_after_topup` → `False`.
5. `pytest tests/services/test_freeze.py` → PASSED.
6. Коммит: «fix(автопродление): пропускать продление замороженной подписки».

---

### Задача 6 — CRUD: фильтр `is_frozen=False` в `get_expired_subscriptions` + новый `get_subscriptions_for_auto_unfreeze`

**Результат:** замороженные подписки не попадают в свип истечений; крон авто-разморозки получает нужные строки.

**Files:**
- Modify: `app/database/crud/subscription.py` (строка 1824 — `get_expired_subscriptions`; добавить `get_subscriptions_for_auto_unfreeze` рядом)

**Interfaces:**
- Consumes: `Subscription.is_frozen`, `Subscription.frozen_auto_unfreeze_at`; индекс `ix_subscriptions_frozen_auto_unfreeze`
- Produces:
  - `get_expired_subscriptions(db)` — теперь с `Subscription.is_frozen == False`
  - `async def get_subscriptions_for_auto_unfreeze(db: AsyncSession, now: datetime) -> list[Subscription]`

**Шаги:**
1. В `get_expired_subscriptions` (строка ~1833–1841) добавить к `and_(...)`:
   ```python
   Subscription.is_frozen == False,
   ```
2. Рядом (после функции) добавить:
   ```python
   async def get_subscriptions_for_auto_unfreeze(db: AsyncSession, now: datetime) -> list[Subscription]:
       result = await db.execute(
           select(Subscription)
           .options(selectinload(Subscription.user))
           .where(
               and_(
                   Subscription.is_frozen == True,
                   Subscription.frozen_auto_unfreeze_at <= now,
               )
           )
       )
       return list(result.scalars().all())
   ```
3. `python3 -m py_compile app/database/crud/subscription.py`
4. В `tests/api/test_freeze_endpoints.py` добавить юнит-тест `test_get_expired_excludes_frozen` — мокировать БД-запрос, убедиться что `is_frozen=True` строка не возвращается. Добавить `test_get_subscriptions_for_auto_unfreeze` — прошедшая дата → попадает в список, будущая → нет.
5. Прогнать тесты → PASSED.
6. Коммит: «fix(crud): исключить замороженные из get_expired_subscriptions; добавить get_subscriptions_for_auto_unfreeze».

---

### Задача 7 — Крон `_check_frozen_subscriptions_for_auto_unfreeze` в `MonitoringService`

**Результат:** метод вызывается в `_monitoring_cycle`; замороженные подписки с истёкшим `frozen_auto_unfreeze_at` размораживаются.

**Files:**
- Modify: `app/services/monitoring_service.py` (после строки ~443, новый метод; вызов в `_monitoring_cycle`)

**Interfaces:**
- Consumes: `get_subscriptions_for_auto_unfreeze(db, now)` (задача 6); `subscription_service.unfreeze_subscription(user, subscription, db, reason='auto')` (задача 4); `self._run_monitoring_task`
- Produces: `async def _check_frozen_subscriptions_for_auto_unfreeze(self, db: AsyncSession) -> None`

**Шаги:**
1. Добавить в `_monitoring_cycle` (строка ~443) вызов:
   ```python
   await self._run_monitoring_task(
       db, self._check_frozen_subscriptions_for_auto_unfreeze(db), '_check_frozen_subscriptions_for_auto_unfreeze'
   )
   ```
2. Добавить метод:
   ```python
   async def _check_frozen_subscriptions_for_auto_unfreeze(self, db: AsyncSession) -> None:
       from app.database.crud.subscription import get_subscriptions_for_auto_unfreeze
       now = datetime.now(UTC)
       subscriptions = await get_subscriptions_for_auto_unfreeze(db, now)
       for subscription in subscriptions:
           user = subscription.user
           try:
               await self.subscription_service.unfreeze_subscription(
                   user=user, subscription=subscription, db=db, reason='auto'
               )
               await db.commit()
               logger.info('Авто-разморозка выполнена', subscription_id=subscription.id)
           except Exception as e:
               await db.rollback()
               logger.error('Ошибка авто-разморозки', subscription_id=subscription.id, error=str(e))
   ```
3. `python3 -m py_compile app/services/monitoring_service.py`
4. В `tests/api/test_freeze_endpoints.py` добавить `test_auto_unfreeze_cron_triggers` — мокировать `get_subscriptions_for_auto_unfreeze` возвращающую одну замороженную подписку с `frozen_auto_unfreeze_at` в прошлом, убедиться что `unfreeze_subscription` вызван с `reason='auto'`.
5. Прогнать тесты → PASSED.
6. Коммит: «feat(мониторинг): авто-разморозка подписок по истечении FREEZE_MAX_DAYS».

---

### Задача 8 — `NotificationType` + `notify_subscription_frozen` + `notify_subscription_unfrozen`

**Результат:** два новых типа в enum, два новых метода в `NotificationDeliveryService`.

**Files:**
- Modify: `app/services/notification_delivery_service.py` (строки ~35, ~517)

**Interfaces:**
- Consumes: `NotificationType` enum (строка 24); `send_notification(user, notification_type, context, bot, telegram_message, telegram_markup)` (существующий метод); `format_email_datetime` (утилита); `settings.FREEZE_MAX_DAYS`
- Produces:
  - `NotificationType.SUBSCRIPTION_FROZEN = 'subscription_frozen'`
  - `NotificationType.SUBSCRIPTION_UNFROZEN = 'subscription_unfrozen'`
  - `async def notify_subscription_frozen(self, user: User, subscription: Subscription, bot=None, telegram_message=None, telegram_markup=None) -> bool`
  - `async def notify_subscription_unfrozen(self, user: User, subscription: Subscription, reason: Literal['manual', 'auto', 'admin'] = 'manual', bot=None, telegram_message=None, telegram_markup=None) -> bool`

**Шаги:**
1. В `NotificationType` (строка ~35) после `SUBSCRIPTION_RENEWED` добавить:
   ```python
   SUBSCRIPTION_FROZEN = 'subscription_frozen'
   SUBSCRIPTION_UNFROZEN = 'subscription_unfrozen'
   ```
2. После `notify_subscription_expired` (строка ~517) добавить `notify_subscription_frozen`:
   ```python
   async def notify_subscription_frozen(
       self,
       user: User,
       subscription: Subscription,
       bot: Bot | None = None,
       telegram_message: str | None = None,
       telegram_markup: Any | None = None,
   ) -> bool:
       context = {
           'frozen_days_banked': subscription.frozen_days_banked,
           'auto_unfreeze_at': format_email_datetime(subscription.frozen_auto_unfreeze_at),
           'freeze_max_days': settings.FREEZE_MAX_DAYS,
       }
       return await self.send_notification(
           user=user,
           notification_type=NotificationType.SUBSCRIPTION_FROZEN,
           context=context,
           bot=bot,
           telegram_message=telegram_message,
           telegram_markup=telegram_markup,
       )
   ```
3. Добавить `notify_subscription_unfrozen` аналогично, с `context = {'reason': reason, 'new_end_date': format_email_datetime(subscription.end_date)}`.
4. `python3 -m py_compile app/services/notification_delivery_service.py`
5. `python3 -c "from app.services.notification_delivery_service import NotificationType; assert 'subscription_frozen' in [e.value for e in NotificationType]"`
6. Коммит: «feat(уведомления): SUBSCRIPTION_FROZEN/UNFROZEN — типы и методы уведомления о заморозке».

---

### Задача 9 — API-эндпоинты `POST /cabinet/subscription/freeze` и `/unfreeze` + схемы

**Результат:** два новых POST-роута доступны; `FreezeNotAllowedError` → HTTP 422; `SubscriptionData` несёт freeze-поля.

**Files:**
- Modify: `app/webapi/schemas/miniapp.py` (после строки ~700, рядом с `MiniAppDailySubscriptionToggleResponse`)
- Modify: `app/cabinet/schemas/subscription.py` (после строки ~76, рядом с daily-полями)
- Modify: `app/webapi/routes/miniapp.py` (добавить после строки ~7500)

**Interfaces:**
- Consumes: `_authorize_miniapp_user`, `lock_user_for_pricing`, `subscription_service.freeze_subscription/unfreeze_subscription`, `FreezeNotAllowedError`, схемы запроса/ответа
- Produces:
  - `class MiniAppSubscriptionFreezeRequest(BaseModel)` — пустая (action однозначен)
  - `class MiniAppSubscriptionFreezeResponse(BaseModel)` — `success: bool`, `is_frozen: bool`, `frozen_days_banked: int | None`, `frozen_auto_unfreeze_at: datetime | None`, `new_end_date: datetime`
  - `class MiniAppSubscriptionUnfreezeResponse(BaseModel)` — `success: bool`, `is_frozen: bool`, `new_end_date: datetime`
  - `SubscriptionData.is_frozen: bool = False`, `.frozen_days_banked: int | None = None`, `.frozen_auto_unfreeze_at: datetime | None = None`
  - `POST /cabinet/subscription/freeze` → `freeze_subscription_endpoint`
  - `POST /cabinet/subscription/unfreeze` → `unfreeze_subscription_endpoint`

**Шаги:**
1. В `app/webapi/schemas/miniapp.py` добавить три Pydantic-класса (по образцу `MiniAppDailySubscriptionToggleResponse`).
2. В `app/cabinet/schemas/subscription.py` после строки ~76 (блок daily-полей) добавить:
   ```python
   # Freeze fields
   is_frozen: bool = False
   frozen_days_banked: int | None = None
   frozen_auto_unfreeze_at: datetime | None = None
   ```
3. Найти место в `miniapp.py` где сериализуется `SubscriptionData` (поиск по `is_daily_paused =`) и добавить аналогичное присваивание для трёх freeze-полей.
4. Добавить эндпоинты в `miniapp.py`:
   ```python
   @router.post('/cabinet/subscription/freeze')
   async def freeze_subscription_endpoint(
       payload: MiniAppSubscriptionFreezeRequest,
       db: AsyncSession = Depends(get_db_session),
   ):
       from app.services.subscription_service import FreezeNotAllowedError, SubscriptionService
       user = await _authorize_miniapp_user(payload.init_data, db)
       subscription = _get_active_subscription(user)
       if not subscription:
           raise HTTPException(status_code=404, detail={'code': 'no_subscription'})
       try:
           await SubscriptionService().freeze_subscription(user=user, subscription=subscription, db=db)
           await db.commit()
       except FreezeNotAllowedError as e:
           raise HTTPException(status_code=422, detail={'error_code': e.reason})
       return MiniAppSubscriptionFreezeResponse(
           success=True,
           is_frozen=subscription.is_frozen,
           frozen_days_banked=subscription.frozen_days_banked,
           frozen_auto_unfreeze_at=subscription.frozen_auto_unfreeze_at,
           new_end_date=subscription.end_date,
       )

   @router.post('/cabinet/subscription/unfreeze')
   async def unfreeze_subscription_endpoint(
       payload: MiniAppSubscriptionFreezeRequest,
       db: AsyncSession = Depends(get_db_session),
   ):
       from app.services.subscription_service import SubscriptionService
       user = await _authorize_miniapp_user(payload.init_data, db)
       subscription = _get_frozen_subscription(user)
       if not subscription:
           raise HTTPException(status_code=404, detail={'code': 'no_frozen_subscription'})
       await SubscriptionService().unfreeze_subscription(user=user, subscription=subscription, db=db, reason='manual')
       await db.commit()
       return MiniAppSubscriptionUnfreezeResponse(
           success=True, is_frozen=False, new_end_date=subscription.end_date
       )
   ```
   Определить хелперы `_get_active_subscription(user)` и `_get_frozen_subscription(user)` локально.
5. `python3 -m py_compile app/webapi/routes/miniapp.py app/webapi/schemas/miniapp.py app/cabinet/schemas/subscription.py`
6. В `tests/api/test_freeze_endpoints.py` добавить:
   - `test_freeze_endpoint_200` — успешная заморозка.
   - `test_freeze_endpoint_422_already_frozen` — возвращает `{'error_code': 'already_frozen'}`.
   - `test_unfreeze_endpoint_200` — успешная разморозка, `new_end_date` сдвинут.
7. Прогнать тесты → PASSED.
8. Коммит: «feat(API): POST /cabinet/subscription/freeze|unfreeze — эндпоинты заморозки».

---

### Задача 10 — Бот-хендлеры заморозки/разморозки

**Результат:** пользователь может заморозить через confirm-диалог и разморозить одной кнопкой в Telegram.

**Files:**
- Create: `app/handlers/subscription/freeze.py`
- Modify: `app/keyboards/inline.py` (~1231, в `get_subscription_keyboard`)
- Modify: `app/handlers/subscription/__init__.py` (если нужен импорт)

**Interfaces:**
- Consumes: `subscription_service.freeze_subscription/unfreeze_subscription`; `FreezeNotAllowedError`; `get_texts`; `settings.FREEZE_SUBSCRIPTIONS_ENABLED`, `settings.FREEZE_MAX_DAYS`; callback_data `'subscription_freeze_confirm'`, `'subscription_freeze_cancel'`, `'subscription_unfreeze'`
- Produces: хендлеры `handle_freeze_request`, `handle_freeze_confirm`, `handle_freeze_cancel`, `handle_unfreeze`; `register_handlers(dp)` в freeze.py; кнопки «Заморозить» / «Разморозить» в `get_subscription_keyboard`

**Шаги:**
1. В `get_subscription_keyboard` (inline.py, ~1231) после блока daily-pause кнопки добавить:
   ```python
   if settings.FREEZE_SUBSCRIPTIONS_ENABLED and subscription:
       is_frozen = getattr(subscription, 'is_frozen', False)
       if is_frozen:
           keyboard.append([InlineKeyboardButton(
               text=texts.t('UNFREEZE_BUTTON', '▶️ Разморозить подписку'),
               callback_data='subscription_unfreeze'
           )])
       elif not subscription.is_trial and subscription.status == SubscriptionStatus.ACTIVE.value:
           keyboard.append([InlineKeyboardButton(
               text=texts.t('FREEZE_BUTTON', '❄️ Заморозить подписку'),
               callback_data='subscription_freeze_request'
           )])
   ```
2. Создать `app/handlers/subscription/freeze.py` с роутером `router = Router()` и хендлерами:
   - `handle_freeze_request(callback, db_user, db)` — показывает confirm-сообщение: текст со спойлером «VPN отключится, дней сохранено: N, авто-разморозка через {FREEZE_MAX_DAYS}д», кнопки «✅ Заморозить» (`subscription_freeze_confirm`) и «❌ Отмена» (`subscription_freeze_cancel`).
   - `handle_freeze_confirm(callback, db_user, db)` — находит активную подписку → `freeze_subscription(...)` → ответ: «Подписка заморожена. Сохранено дней: {frozen_days_banked}. Авто-разморозка: {дата}.». При `FreezeNotAllowedError` со стандартным локализованным сообщением по `e.reason`.
   - `handle_freeze_cancel(callback)` — `callback.answer('Отменено.')` + редактировать сообщение.
   - `handle_unfreeze(callback, db_user, db)` — находит замороженную подписку → `unfreeze_subscription(reason='manual')` → ответ: «Подписка разморожена. Действует до: {new_end_date}.».
3. `register_handlers(dp)` регистрирует все четыре хендлера по `callback_data`.
4. `python3 -m py_compile app/handlers/subscription/freeze.py app/keyboards/inline.py`
5. `python3 -c "from app.handlers.subscription.freeze import register_handlers"` — OK.
6. Коммит: «feat(бот): хендлеры заморозки/разморозки подписки через Telegram».

---

### Задача 11 — Admin freeze/unfreeze хендлеры в боте

**Результат:** администратор может заморозить/разморозить подписку любого пользователя из admin-панели бота.

**Files:**
- Modify: `app/handlers/admin/subscriptions.py` (добавить в конец файла)

**Interfaces:**
- Consumes: `subscription_service.freeze_subscription/unfreeze_subscription(reason='admin')`; `FreezeNotAllowedError`; callback_data `'admin_sub_freeze:{sub_id}'`, `'admin_sub_unfreeze:{sub_id}'`
- Produces: `handle_admin_freeze_subscription`, `handle_admin_unfreeze_subscription`

**Шаги:**
1. Добавить в `app/handlers/admin/subscriptions.py`:
   - `handle_admin_freeze_subscription(callback, db_user, db)` — из callback.data извлечь `sub_id`, загрузить подписку, загрузить `user = subscription.user`. Вызвать `freeze_subscription(user=user, subscription=subscription, db=db)`. При ошибке `FreezeNotAllowedError` — `callback.answer(f'Ошибка: {e.reason}', show_alert=True)`. Логировать: `logger.info('Admin freeze', admin_id=db_user.id, user_id=user.id, subscription_id=sub_id)`.
   - `handle_admin_unfreeze_subscription(callback, db_user, db)` — аналогично с `unfreeze_subscription(reason='admin')`.
2. Зарегистрировать в `register_handlers(dp)` конец файла.
3. `python3 -m py_compile app/handlers/admin/subscriptions.py`
4. Коммит: «feat(admin): заморозка/разморозка подписки из admin-панели бота».

---

### Задача 12 — API-клиент кабинета: `freeze`/`unfreeze` + типы

**Результат:** `subscriptionApi.freeze()` и `subscriptionApi.unfreeze()` доступны; тип `Subscription` содержит freeze-поля; `tsc --noEmit` чист.

**Files:**
- Modify: `src/api/subscription.ts` (~824, после блока `// Daily subscription`)
- Modify: `src/types/index.ts` (~100, после `is_daily_paused`)

**Interfaces:**
- Consumes: `apiClient.post('/cabinet/subscription/freeze')`, `apiClient.post('/cabinet/subscription/unfreeze')`
- Produces:
  - `subscriptionApi.freeze(subscriptionId?: number): Promise<{success: bool; is_frozen: bool; frozen_days_banked: number | null; frozen_auto_unfreeze_at: string | null; new_end_date: string}>`
  - `subscriptionApi.unfreeze(subscriptionId?: number): Promise<{success: bool; is_frozen: bool; new_end_date: string}>`
  - `Subscription.is_frozen?: boolean`
  - `Subscription.frozen_days_banked?: number | null`
  - `Subscription.frozen_auto_unfreeze_at?: string | null`

**Шаги:**
1. В `src/types/index.ts` после строки `is_daily_paused?: boolean` (строка ~98) добавить:
   ```typescript
   // Freeze fields
   is_frozen?: boolean;
   frozen_days_banked?: number | null;
   frozen_auto_unfreeze_at?: string | null;
   ```
2. В `src/api/subscription.ts` после блока `// Daily subscription` (~824) добавить:
   ```typescript
   // ── Freeze subscription ─────────────────────────────────────────────

   freeze: async (subscriptionId?: number): Promise<{
     success: boolean;
     is_frozen: boolean;
     frozen_days_banked: number | null;
     frozen_auto_unfreeze_at: string | null;
     new_end_date: string;
   }> => {
     const response = await apiClient.post(
       '/cabinet/subscription/freeze',
       undefined,
       withSubId(subscriptionId),
     );
     return response.data;
   },

   unfreeze: async (subscriptionId?: number): Promise<{
     success: boolean;
     is_frozen: boolean;
     new_end_date: string;
   }> => {
     const response = await apiClient.post(
       '/cabinet/subscription/unfreeze',
       undefined,
       withSubId(subscriptionId),
     );
     return response.data;
   },
   ```
3. `cd /Users/mihail/Desktop/Serv/bedolaga-cabinet && npx tsc --noEmit` → 0 ошибок.
4. Коммит (кабинет): «feat(api): subscriptionApi.freeze/unfreeze + тип Subscription с freeze-полями».

---

### Задача 13 — UI `SimpleSubscription.tsx`: кнопка/CTA заморозки, модал, состояние «заморожена»

**Результат:** экран «Подписка» показывает кнопку или CTA в зависимости от наличия email; при is_frozen — блок-баннер с кнопкой разморозки; `tsc --noEmit` + тесты зелёные.

**Files:**
- Modify: `src/components/simple/SimpleSubscription.tsx`

**Interfaces:**
- Consumes: `subscription.is_frozen`, `.frozen_days_banked`, `.frozen_auto_unfreeze_at`; `subscriptionApi.freeze()`, `subscriptionApi.unfreeze()`; `useMutation` (tanstack query); `useTranslation`; `t('freeze.*')`
- Produces: блок заморозки/разморозки в JSX

**Шаги:**
1. Добавить `useState` для `showFreezeModal: boolean`.
2. Добавить `useMutation` для `freezeMutation` (вызывает `subscriptionApi.freeze()`, on success инвалидирует `['subscription']`) и `unfreezeMutation` (вызывает `subscriptionApi.unfreeze()`).
3. Определить `isFrozen = subscription?.is_frozen ?? false`.
4. Определить `canFreeze`: `!isFrozen && subscription?.status === 'active' && !subscription?.is_trial && !!FREEZE_SUBSCRIPTIONS_ENABLED_FLAG`. Флаг читать из конфига (`import { API } from '../../config/constants'` или аналогичный механизм из `AppConfig`).
5. Определить `hasVerifiedEmail` — из `userProfile`/`authState` или поля в ответе. Если нет данных о почте — считать `false` (безопасная сторона, не блокирует кабинет для пользователей с почтой, но сервер всё равно проверит).
6. Рендерить (в блоке активной подписки):
   - Если `isFrozen`:
     ```tsx
     <BentoCard>
       <p>{t('freeze.status_frozen')}</p>
       <p>{t('freeze.days_banked', { count: subscription.frozen_days_banked })}</p>
       <p>{t('freeze.auto_unfreeze_at', { date: formatLongDate(subscription.frozen_auto_unfreeze_at) })}</p>
       <Button onClick={() => unfreezeMutation.mutate()}>{t('freeze.unfreeze_button')}</Button>
     </BentoCard>
     ```
   - Если `canFreeze && !hasVerifiedEmail`:
     ```tsx
     <p>{t('freeze.email_required_cta')} <a href="/profile">{t('freeze.email_link')}</a></p>
     ```
   - Если `canFreeze && hasVerifiedEmail`:
     ```tsx
     <Button onClick={() => setShowFreezeModal(true)}>{t('freeze.freeze_button')}</Button>
     ```
7. Рендерить модал (`showFreezeModal`):
   ```tsx
   {showFreezeModal && (
     <div role="dialog">
       <h2>{t('freeze.modal_title')}</h2>
       <p>{t('freeze.modal_body', { days: subscription?.days_left, maxDays: FREEZE_MAX_DAYS })}</p>
       <Button onClick={() => { freezeMutation.mutate(); setShowFreezeModal(false); }}>{t('freeze.confirm_button')}</Button>
       <Button variant="secondary" onClick={() => setShowFreezeModal(false)}>{t('freeze.cancel_button')}</Button>
     </div>
   )}
   ```
8. `npx tsc --noEmit` → OK.
9. `npm test -- --testPathPattern=SimpleSubscription` → PASSED.
10. Коммит: «feat(кабинет): SimpleSubscription — кнопка/CTA заморозки, модал, блок состояния "заморожена"».

---

### Задача 14 — UI `SimpleDashboard.tsx`: hero-статус «Заморожена», скрыть подключение

**Результат:** при `is_frozen=true` hero показывает нейтральный статус «Заморожена» и скрывает секцию «Подключить устройство»; `tsc --noEmit` + тесты зелёные.

**Files:**
- Modify: `src/components/simple/SimpleDashboard.tsx`

**Interfaces:**
- Consumes: `subscription.is_frozen`, `.frozen_days_banked`; существующие условия hero-блока; `t('freeze.*')`
- Produces: новая ветка `isFrozen` в JSX hero

**Шаги:**
1. Извлечь `isFrozen = subscriptionResponse?.subscription?.is_frozen ?? false` и `frozenDaysBanked = subscriptionResponse?.subscription?.frozen_days_banked`.
2. В hero-блоке (где рендерится «Активна»/«Истекла» и т.д.) добавить ветку раньше «активна»:
   ```tsx
   if (isFrozen) {
     heroStatus = t('freeze.hero_status'); // «Заморожена»
     heroColor = 'neutral'; // не зелёный, не красный
     heroSubtitle = t('freeze.hero_days_banked', { count: frozenDaysBanked });
     heroHint = t('freeze.hero_hint'); // «Разморозьте в разделе Подписка»
   }
   ```
3. Скрыть кнопку/раздел «Подключить устройство»: обернуть в `{!isFrozen && <ConnectDeviceSection />}`.
4. `npx tsc --noEmit` → OK.
5. `npm test -- --testPathPattern=SimpleDashboard` → PASSED.
6. Коммит: «feat(кабинет): SimpleDashboard — статус "Заморожена", скрыть "Подключить устройство"».

---

### Задача 15 — Ключи локалей (ru / en / zh / fa)

**Результат:** все 4 JSON-файла содержат одинаковый набор ключей `freeze.*`; `npm test` (тест паритета локалей `src/locales/locales.test.ts`) → PASSED.

**Files:**
- Modify: `src/locales/ru.json`
- Modify: `src/locales/en.json`
- Modify: `src/locales/zh.json`
- Modify: `src/locales/fa.json`

**Interfaces:**
- Produces: ключи `freeze.freeze_button`, `freeze.unfreeze_button`, `freeze.modal_title`, `freeze.modal_body`, `freeze.confirm_button`, `freeze.cancel_button`, `freeze.status_frozen`, `freeze.days_banked`, `freeze.auto_unfreeze_at`, `freeze.email_required_cta`, `freeze.email_link`, `freeze.hero_status`, `freeze.hero_days_banked`, `freeze.hero_hint`

**Шаги:**
1. Добавить секцию `"freeze": { ... }` в `ru.json` с 14 ключами (русский текст по тексту спеки §13).
2. Добавить те же 14 ключей в `en.json` (английский перевод).
3. Добавить те же 14 ключей в `zh.json` (китайский — упрощённый, машинный перевод в качестве заглушки, помеченный TODO для ревью носителя).
4. Добавить те же 14 ключей в `fa.json` (фарси RTL — машинный перевод, помеченный TODO).
5. `npm test -- --testPathPattern=locales` → PASSED (тест проверяет паритет ключей).
6. Коммит: «feat(локали): ru/en/zh/fa — ключи заморозки подписки».

---

## Self-Review

### Покрытие разделов спеки задачами

| Раздел спеки | Задача |
|---|---|
| §1 Цель | — (описание) |
| §2 Механика заморозки/разморозки | Задачи 2, 4 |
| §3 Безопасность от блокировки (email precon) | Задачи 4, 9 |
| §4 Поля и миграция | Задачи 1, 2 |
| §5 Сервисный слой freeze/unfreeze | Задача 4 |
| §6 Предусловия и валидация | Задача 4 |
| §7 Автопродление | Задача 5 |
| §8 Планировщик и авто-разморозка | Задачи 6, 7 |
| §9 Конфиг | Задача 3 |
| §10 API кабинета | Задача 9 |
| §11 Бот-хендлеры | Задача 10 |
| §12 Администрирование | Задача 11 |
| §13 UI кабинета | Задачи 13, 14 |
| §14 Уведомления | Задача 8 |
| §15 Крайние случаи | Покрыты в Задаче 4 (ранний возврат, NULL frozen_at) и Задаче 6 (фильтр is_frozen) |
| §16 Тесты | Задачи 4, 5, 6, 7, 9, 13, 14 |

### Сканирование плейсхолдеров

- «аналогично задаче N» — не используется.
- «TBD»/«TODO» — только один маркер в zh/fa локалях для ревью носителем (приемлемо; не технический TBD).
- Задача 5 содержит уточнение по структуре `cart_data` — это намеренная пометка для исполнителя проверить фактический тип объекта перед вставкой; не плейсхолдер.

### Согласованность типов/имён

- `FreezeNotAllowedError` — определяется в Задаче 4, используется в Задачах 9, 10, 11.
- `reason: Literal['manual', 'auto', 'admin']` — используется в Задачах 4, 7, 10, 11 согласованно.
- `get_subscriptions_for_auto_unfreeze(db, now)` — определяется в Задаче 6, используется в Задаче 7.
- `frozen_auto_unfreeze_at` — имя поля согласовано в Задачах 1, 2, 4, 9, 12, 13.
- Cabinet-эндпоинты: Задача 9 (бэкенд `POST /cabinet/subscription/freeze|unfreeze`) и Задача 12 (фронтенд `apiClient.post('/cabinet/subscription/freeze|unfreeze')`) — совпадают.
- `MiniAppSubscriptionFreezeRequest` без тела (пустой BaseModel) — используется и для freeze, и для unfreeze (action задан URL), соответствует спеке §10.2.

### Устранённые нестыковки спеки

1. **§5.2 vs §12 `reason`**: спека §5.2 определяет `reason: Literal['manual', 'auto']`, но §12 говорит `reason='admin'`. В плане принято решение расширить до `Literal['manual', 'auto', 'admin']` — закреплено в Global Constraints.
2. **`get_subscriptions_for_auto_unfreeze` имя**: в спеке §8.1 функция называется `get_subscriptions_for_auto_unfreeze`, в старой карте кода — `get_subscriptions_for_unfreeze`. Принято имя из спеки.
