# Дизайн-спека: Заморозка / приостановка подписки

**Дата:** 2026-08-19  
**Статус:** Принято, готово к реализации

---

## Оглавление

1. [Цель](#1-цель)
2. [Обзор механики](#2-обзор-механики)
3. [Модель безопасности от блокировки](#3-модель-безопасности-от-блокировки)
4. [Изменения данных: поля и миграция](#4-изменения-данных-поля-и-миграция)
5. [Сервисный слой: freeze / unfreeze](#5-сервисный-слой-freeze--unfreeze)
6. [Предусловия и валидация](#6-предусловия-и-валидация)
7. [Автопродление](#7-автопродление)
8. [Планировщик и авто-разморозка](#8-планировщик-и-авто-разморозка)
9. [Конфиг](#9-конфиг)
10. [API кабинета (схемы и эндпоинты)](#10-api-кабинета-схемы-и-эндпоинты)
11. [Бот-хендлеры (Telegram)](#11-бот-хендлеры-telegram)
12. [Администрирование](#12-администрирование)
13. [UI кабинета: простой режим](#13-ui-кабинета-простой-режим)
14. [Уведомления](#14-уведомления)
15. [Крайние случаи и идемпотентность](#15-крайние-случаи-и-идемпотентность)
16. [Что покрыть тестами](#16-что-покрыть-тестами)

---

## 1. Цель

Клиент временно приостанавливает подписку (отпуск, командировка и т.п.): VPN на время заморозки отключён, дни не тратятся, при разморозке оставшиеся дни возвращаются через сдвиг `end_date`.

---

## 2. Обзор механики

### Классическая заморозка (один тип, без вариантов)

- **Заморозка** — `status = DISABLED`, `is_frozen = True`. Отдельный статус `FROZEN` не вводится; флаг `is_frozen` отличает заморозку от прочих отключений (например, ручного бана).
- **VPN** — при заморозке отключается через `disable_remnawave_user()` (`subscription_service.py:1066`); при разморозке включается через `enable_remnawave_user()` (`subscription_service.py:1108`). Оба метода идемпотентны.
- **Счётчик дней** — остановлен: `end_date` не движется, пока подписка заморожена.
- **Разморозка** — `end_date += (now − frozen_at)`. `status = ACTIVE`. Freeze-поля обнуляются.
- **Открытая заморозка** — срок заранее не задаётся. Разморозка: вручную клиентом (кабинет или бот) или автоматически при достижении `frozen_auto_unfreeze_at`.
- **Квоты и кулдауны** — не вводятся. Единственный лимит — `FREEZE_MAX_DAYS` как страховка авто-разморозки. Повторная заморозка после разморозки разрешена без ограничений.

### Взаимодействие с суточными тарифами

Суточные подписки имеют собственный механизм паузы (`is_daily_paused`, `models.py:2394`). Freeze не используется для суточных тарифов — предусловие заморозки исключает суточные (см. раздел 6). Если `is_daily_paused = True` — заморозка блокируется (статус уже нестандартный).

---

## 3. Модель безопасности от блокировки

**Проблема:** клиент замораживает подписку → VPN выключается → в регионе с жёсткой блокировкой клиент может не попасть ни в кабинет (работает через Cloudflare/браузер), ни в Telegram.

Три независимых пути разморозки, каждый работает без остальных:

| Путь | Механизм | Предусловие |
|------|----------|-------------|
| **1. Кабинет по почте** | Вход в кабинет по `email` + `password` (без Telegram) | Предусловие заморозки: `user.email is not None and user.email_verified` |
| **2. Разморозка через бот** | Команда/кнопка «Разморозить» в Telegram-боте | Telegram доступен |
| **3. Авто-разморозка** | Крон `_check_frozen_subscriptions_for_auto_unfreeze()` по достижении `frozen_auto_unfreeze_at` | Требует только работающего сервера |

**Обязательное предусловие** — привязанная и верифицированная почта (`user.email is not None and user.email_verified`, `models.py:2101-2102`). Если почта не привязана, кнопка «Заморозить» в кабинете не показывается — вместо неё CTA «Привяжите почту» со ссылкой в профиль. Это гарантирует наличие пути 1 в момент заморозки.

---

## 4. Изменения данных: поля и миграция

### 4.1 Новые поля модели `Subscription` (`app/database/models.py:2282`)

```python
# Freeze fields — добавить к классу Subscription
is_frozen = Column(Boolean, nullable=False, default=False, server_default='false')
frozen_at = Column(AwareDateTime(), nullable=True)
frozen_days_banked = Column(Integer, nullable=True)       # снимок days_left на момент заморозки
frozen_auto_unfreeze_at = Column(AwareDateTime(), nullable=True)  # = frozen_at + FREEZE_MAX_DAYS
```

**Семантика полей:**

| Поле | Тип | Описание |
|------|-----|----------|
| `is_frozen` | `bool, NOT NULL, default False` | Флаг заморозки; `True` только при `status = DISABLED` из-за freeze |
| `frozen_at` | `datetime, nullable` | Момент заморозки (UTC) |
| `frozen_days_banked` | `int, nullable` | Снимок `days_left` на момент заморозки (только для отображения в UI) |
| `frozen_auto_unfreeze_at` | `datetime, nullable` | Граница авто-разморозки = `frozen_at + timedelta(days=FREEZE_MAX_DAYS)` |

### 4.2 Alembic-миграция

**Файл:** `migrations/alembic/versions/9030_subscription_freeze.py`

Нумерация продолжает паттерн нашего форка (последняя — `9029_user_cabinet_ui_mode.py`). Файл должен содержать числовой префикс `9030` в соответствии с `_OUR_MIGRATION_PATTERN`.

```python
"""Add subscription freeze fields

Revision ID: 9030
Revises: <prev_revision_id>
Create Date: 2026-08-19
"""

def upgrade():
    op.add_column('subscriptions', sa.Column('is_frozen', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('subscriptions', sa.Column('frozen_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('subscriptions', sa.Column('frozen_days_banked', sa.Integer(), nullable=True))
    op.add_column('subscriptions', sa.Column('frozen_auto_unfreeze_at', sa.DateTime(timezone=True), nullable=True))
    # Индекс для крона авто-разморозки
    op.create_index('ix_subscriptions_frozen_auto_unfreeze', 'subscriptions',
                    ['is_frozen', 'frozen_auto_unfreeze_at'])

def downgrade():
    op.drop_index('ix_subscriptions_frozen_auto_unfreeze', table_name='subscriptions')
    op.drop_column('subscriptions', 'frozen_auto_unfreeze_at')
    op.drop_column('subscriptions', 'frozen_days_banked')
    op.drop_column('subscriptions', 'frozen_at')
    op.drop_column('subscriptions', 'is_frozen')
```

---

## 5. Сервисный слой: freeze / unfreeze

Оба метода добавляются в класс `SubscriptionService` (`app/services/subscription_service.py`).

### 5.1 `freeze_subscription(user, subscription, db)`

```python
async def freeze_subscription(
    self,
    user: User,
    subscription: Subscription,
    db: AsyncSession,
) -> None:
    """
    Заморозить подписку:
    - status = DISABLED, is_frozen = True
    - frozen_at = now, frozen_auto_unfreeze_at = now + FREEZE_MAX_DAYS
    - frozen_days_banked = subscription.days_left  (models.py:2506)
    - disable_remnawave_user()
    - notify_subscription_frozen()
    """
```

**Последовательность шагов:**

1. Валидация предусловий (раздел 6) — при ошибке `raise FreezeNotAllowedError(reason=...)`.
2. `now = datetime.now(UTC)`
3. `subscription.frozen_days_banked = subscription.days_left`  (`days_left` property, `models.py:2506`)
4. `subscription.frozen_at = now`
5. `subscription.frozen_auto_unfreeze_at = now + timedelta(days=settings.FREEZE_MAX_DAYS)`
6. `subscription.is_frozen = True`
7. `subscription.status = SubscriptionStatus.DISABLED`
8. `await db.flush()` (без commit — commit на уровне роутера/сервиса-вызывателя)
9. `await self.disable_remnawave_user(panel_user_id=subscription.remnawave_id, db=db)`
10. `await notification_delivery_service.notify_subscription_frozen(user=user, subscription=subscription)`

### 5.2 `unfreeze_subscription(user, subscription, db, reason)`

```python
async def unfreeze_subscription(
    self,
    user: User,
    subscription: Subscription,
    db: AsyncSession,
    reason: Literal['manual', 'auto'] = 'manual',
) -> None:
    """
    Разморозить подписку:
    - end_date += (now − frozen_at)
    - status = ACTIVE, is_frozen = False, freeze-поля обнуляются
    - enable_remnawave_user()
    - notify_subscription_unfrozen()
    """
```

**Последовательность шагов:**

1. Проверка `subscription.is_frozen` — если уже не заморожена, ранний возврат (идемпотентность).
2. `now = datetime.now(UTC)`
3. `frozen_duration = now - subscription.frozen_at`
4. `subscription.end_date = subscription.end_date + frozen_duration`  (прямой сдвиг, по образцу `crud/subscription.py:1107`)
5. `subscription.status = SubscriptionStatus.ACTIVE`
6. `subscription.is_frozen = False`
7. `subscription.frozen_at = None`
8. `subscription.frozen_days_banked = None`
9. `subscription.frozen_auto_unfreeze_at = None`
10. `await db.flush()`
11. `await self.enable_remnawave_user(panel_user_id=subscription.remnawave_id, db=db)`
12. `await notification_delivery_service.notify_subscription_unfrozen(user=user, subscription=subscription, reason=reason)`

---

## 6. Предусловия и валидация

Все проверки выполняются в начале `freeze_subscription()` (или в отдельном `_validate_freeze_preconditions()`). При нарушении бросается `FreezeNotAllowedError` с кодом причины, который транслируется в понятное пользователю сообщение.

| № | Предусловие | Код ошибки |
|---|------------|------------|
| 1 | `settings.FREEZE_SUBSCRIPTIONS_ENABLED is True` | `freeze_disabled` |
| 2 | `subscription.status` в `{ACTIVE}` (не TRIAL, не EXPIRED, не DISABLED, не в грейсе) | `invalid_status` |
| 3 | `subscription.is_trial is False` (статус `TRIAL` уже исключён выше; явная проверка флага) | `trial_not_allowed` |
| 4 | `subscription.days_left >= settings.FREEZE_MIN_DAYS_REMAINING` (`days_left` property, `models.py:2506`) | `too_few_days` |
| 5 | `subscription.is_frozen is False` (ещё не заморожена) | `already_frozen` |
| 6 | `subscription.is_daily_paused is False` (`models.py:2394`) — суточная на паузе | `daily_paused` |
| 7 | `subscription.grace_candidate_at is None` — не в grace-window | `in_grace` |
| 8 | `user.email is not None and user.email_verified` (`models.py:2101-2102`) | `email_not_verified` |

Предусловия для `unfreeze_subscription()`: только `subscription.is_frozen is True`; если нет — ранний возврат без ошибки (идемпотентность).

---

## 7. Автопродление

**Файл:** `app/services/subscription_auto_purchase_service.py`

В метод `_auto_extend_subscription()` (строка 494) добавляется ранний возврат:

```python
# В начале _auto_extend_subscription(), до любой логики:
if subscription.is_frozen:
    logger.debug('Автопродление пропущено: подписка заморожена', subscription_id=subscription.id)
    return
```

Аналогичная проверка добавляется в `try_auto_extend_expired_after_topup()` (строка 2470) — чтобы пополнение баланса во время заморозки не запускало автопродление.

Когда `end_date` во время заморозки номинально «наступает», подписка не должна продлеваться — заморозка обеспечивает это через ранний возврат выше. После разморозки `end_date` сдвигается вперёд, и автопродление снова начинает отсчёт от нового `end_date`.

---

## 8. Планировщик и авто-разморозка

### 8.1 Новый метод `_check_frozen_subscriptions_for_auto_unfreeze()`

**Файл:** `app/services/monitoring_service.py`  
**Класс:** `MonitoringService` (строка 200)  
**Вызов:** из `_monitoring_cycle()` — после `_check_expired_subscriptions()` (строка 641)

```python
async def _check_frozen_subscriptions_for_auto_unfreeze(self, db: AsyncSession) -> None:
    """
    Найти подписки с is_frozen=True и frozen_auto_unfreeze_at <= now.
    Для каждой вызвать unfreeze_subscription(reason='auto').
    """
    now = datetime.now(UTC)
    # SELECT * FROM subscriptions WHERE is_frozen = TRUE AND frozen_auto_unfreeze_at <= now
    subscriptions = await get_subscriptions_for_auto_unfreeze(db, now)
    for subscription in subscriptions:
        user = subscription.user
        try:
            await subscription_service.unfreeze_subscription(
                user=user, subscription=subscription, db=db, reason='auto'
            )
            await db.commit()
            logger.info('Авто-разморозка выполнена', subscription_id=subscription.id)
        except Exception as e:
            await db.rollback()
            logger.error('Ошибка авто-разморозки', subscription_id=subscription.id, error=str(e))
```

**Вспомогательный CRUD** `get_subscriptions_for_auto_unfreeze(db, now)` — добавить в `app/database/crud/subscription.py` по образцу `get_expired_subscriptions()`. Использует индекс `ix_subscriptions_frozen_auto_unfreeze`.

### 8.2 Исключение замороженных из свипа истёкших

В `_check_expired_subscriptions()` (`monitoring_service.py:641`) запрос `get_expired_subscriptions()` должен возвращать только строки с `is_frozen = False`. Добавить фильтр в CRUD-функцию:

```python
# В get_expired_subscriptions() — добавить к WHERE:
Subscription.is_frozen == False
```

Это исключает ситуацию, когда `end_date` замороженной подписки номинально истекает, а крон переводит её в `EXPIRED`, затирая заморозку.

### 8.3 Частота проверки

Авто-разморозка работает в рамках существующего `MonitoringService._monitoring_cycle()` с интервалом ~60 минут (`MONITORING_INTERVAL`, `main.py:642`). Отдельная задача не нужна: точность ±1 час приемлема.

---

## 9. Конфиг

**Файл:** `app/config.py`, класс `Settings` (строка 84)

```python
# Заморозка подписки
FREEZE_SUBSCRIPTIONS_ENABLED: bool = False      # Фича по умолчанию выключена
FREEZE_MAX_DAYS: int = 60                        # Максимальная длительность заморозки (авто-разморозка)
FREEZE_MIN_DAYS_REMAINING: int = 3              # Минимум дней до конца подписки для активации заморозки
```

Переменные переопределяются через ENV. `FREEZE_SUBSCRIPTIONS_ENABLED=False` по умолчанию позволяет развернуть код без активации фичи.

---

## 10. API кабинета (схемы и эндпоинты)

### 10.1 Новые эндпоинты

**Файл:** `app/webapi/routes/miniapp.py`  
**Паттерн:** по образцу `toggle_daily_subscription_pause_endpoint` (строка 7407)

```
POST /cabinet/subscription/freeze
POST /cabinet/subscription/unfreeze
```

Оба эндпоинта:
- Требуют аутентификации (существующий auth middleware кабинета).
- Находят активную подписку пользователя.
- Вызывают `subscription_service.freeze_subscription()` / `unfreeze_subscription()`.
- При `FreezeNotAllowedError` возвращают `HTTP 422` с полем `error_code`.

### 10.2 Схемы запросов и ответов

**Файл:** `app/webapi/schemas/miniapp.py` — добавить:

```python
class MiniAppSubscriptionFreezeRequest(BaseModel):
    pass  # тело пустое, действие однозначно определено URL

class MiniAppSubscriptionFreezeResponse(BaseModel):
    success: bool
    is_frozen: bool
    frozen_days_banked: int | None
    frozen_auto_unfreeze_at: datetime | None
    new_end_date: datetime  # для информации

class MiniAppSubscriptionUnfreezeResponse(BaseModel):
    success: bool
    is_frozen: bool
    new_end_date: datetime
```

### 10.3 Расширение схемы подписки

**Файл:** `app/cabinet/schemas/subscription.py`, класс `SubscriptionData` (строка 64)

Добавить freeze-поля рядом с существующими daily-полями (строки 63-67):

```python
# Freeze fields
is_frozen: bool = False
frozen_days_banked: int | None = None
frozen_auto_unfreeze_at: datetime | None = None
```

Эти поля включаются в ответ при каждом запросе страницы подписки, чтобы фронтенд рисовал UI состояния заморозки без дополнительного запроса.

---

## 11. Бот-хендлеры (Telegram)

Бот — независимый путь разморозки (часть модели безопасности, раздел 3).

### 11.1 Заморозка

**Точка входа:** кнопка «Заморозить подписку» в меню управления подпиской.  
**Файл:** добавить хендлер в `app/handlers/subscription/` (по образцу существующих хендлеров управления подпиской).

Сценарий:
1. Бот показывает confirm-диалог: «Пока подписка заморожена, VPN не работает. Дни сохраняются. Авто-разморозка через N дней. Разморозить можно здесь или через кабинет по почте. Подтвердить?»
2. Пользователь подтверждает → вызов `subscription_service.freeze_subscription()`.
3. Ответ: «Подписка заморожена. Сохранено дней: N. Авто-разморозка: {дата}.»

Ошибка `email_not_verified`: «Для заморозки необходимо привязать почту в кабинете. [Привязать →]»

### 11.2 Разморозка

**Точка входа:** кнопка «Разморозить» в меню управления подпиской (показывается только если `subscription.is_frozen`).

Сценарий:
1. Немедленная разморозка без confirm-диалога (действие безопасное — включает VPN).
2. Ответ: «Подписка разморожена. Подписка продлена до: {new_end_date}.»

### 11.3 Отображение статуса

В разделе «Моя подписка» бота: если `subscription.is_frozen` — показывать статус «Заморожена» вместо стандартного, и кнопку «Разморозить» вместо «Заморозить».

---

## 12. Администрирование

Администратор должен иметь возможность заморозить/разморозить подписку любого пользователя вручную (для поддержки).

**Точки входа:**
- Существующий admin-раздел управления подпиской пользователя.
- Кнопки «Заморозить» / «Разморозить» рядом с блоком статуса подписки.
- При действии: вызов тех же `freeze_subscription()` / `unfreeze_subscription()` с `reason='admin'`.
- Предусловия для admin-заморозки: те же, что для пользовательской, кроме проверки `email_not_verified` (admin может замораживать без email).
- Журналировать действие: `logger.info('Admin freeze/unfreeze', admin_id=..., user_id=..., subscription_id=...)`.

---

## 13. UI кабинета: простой режим

**Репозиторий:** `/Users/mihail/Desktop/Serv/bedolaga-cabinet`

### 13.1 Экран «Подписка» (`SimpleSubscription.tsx`)

#### Состояние «Подписка активна»

Кнопка «Заморозить подписку» — показывается если `is_frozen == false && FREEZE_SUBSCRIPTIONS_ENABLED`.

- **Если email не привязан** (`is_frozen == false` и нет верифицированной почты): вместо кнопки — CTA «Привяжите почту, чтобы заморозить подписку» со ссылкой в профиль (`/profile`).
- **Если email привязан**: кнопка «Заморозить подписку» → открывает модал.

**Модал заморозки (информационный, с confirm):**
- Заголовок: «Заморозить подписку?»
- Текст: «Пока подписка заморожена, VPN не работает. Оставшиеся дни ({N}) сохранятся и вернутся при разморозке. Разморозить можно здесь или через Telegram-бот. Авто-разморозка через {FREEZE_MAX_DAYS} дней.»
- Кнопки: «Заморозить» (POST `/cabinet/subscription/freeze`) и «Отмена».

#### Состояние «Подписка заморожена» (`is_frozen == true`)

Блок-баннер в верхней части:
- «Подписка заморожена»
- «Сохранено дней: {frozen_days_banked}»
- «Авто-разморозка: {frozen_auto_unfreeze_at, форматированная дата}»
- Кнопка «Разморозить» (POST `/cabinet/subscription/unfreeze`).

### 13.2 Экран «Главная» (`SimpleDashboard.tsx`)

#### Если подписка заморожена

- **Hero-блок:** статус «Заморожена» вместо «Активна». Цвет — нейтральный/синий (не зелёный, не красный). Текст: «Дней сохранено: {frozen_days_banked}». Hero кликабелен — ведёт на экран «Подписка».
- **Скрыть:** кнопку / раздел «Подключить устройство» (VPN всё равно не работает, конфиги бесполезны).
- **Подсказка под hero:** «Разморозьте подписку в разделе Подписка, чтобы возобновить VPN».

---

## 14. Уведомления

**Файл:** `app/services/notification_delivery_service.py`  
**Класс:** `NotificationDeliveryService` (строка 101)  
**Паттерн:** по образцу `notify_subscription_expired()` (строка 502)

### 14.1 `notify_subscription_frozen()`

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

**Содержание уведомления:**
- «Ваша подписка заморожена.»
- «Сохранено дней: {frozen_days_banked}.»
- «Авто-разморозка: {auto_unfreeze_at}.»
- «Чтобы разморозить раньше: используйте Telegram-бот или войдите в кабинет по email.»

Добавить `SUBSCRIPTION_FROZEN` в `NotificationType` enum.

### 14.2 `notify_subscription_unfrozen()`

```python
async def notify_subscription_unfrozen(
    self,
    user: User,
    subscription: Subscription,
    reason: Literal['manual', 'auto'] = 'manual',
    bot: Bot | None = None,
    telegram_message: str | None = None,
    telegram_markup: Any | None = None,
) -> bool:
    context = {
        'reason': reason,
        'new_end_date': format_email_datetime(subscription.end_date),
    }
    return await self.send_notification(
        user=user,
        notification_type=NotificationType.SUBSCRIPTION_UNFROZEN,
        context=context,
        bot=bot,
        telegram_message=telegram_message,
        telegram_markup=telegram_markup,
    )
```

**Содержание уведомления:**
- Если `reason == 'auto'`: «Ваша подписка автоматически разморожена (истёк максимальный срок заморозки).»
- Если `reason == 'manual'`: «Ваша подписка разморожена.»
- В обоих случаях: «VPN снова активен. Подписка действует до: {new_end_date}.»

Добавить `SUBSCRIPTION_UNFROZEN` в `NotificationType` enum.

---

## 15. Крайние случаи и идемпотентность

| Ситуация | Поведение |
|----------|-----------|
| `freeze_subscription()` при уже замороженной | Бросает `FreezeNotAllowedError(already_frozen)` |
| `unfreeze_subscription()` при уже активной | Ранний возврат, ничего не делает (идемпотентно) |
| `disable_remnawave_user()` возвращает ошибку | Логируем, не откатываем DB — VPN может уже не работать; крон авто-разморозки всё равно включит обратно |
| `enable_remnawave_user()` возвращает ошибку при разморозке | Логируем, статус в DB уже ACTIVE — RetryLogic: следующий мониторинг-цикл обнаружит активную подписку с disabled панелью и починит |
| `frozen_at` вдруг NULL при разморозке | Логируем ошибку, сдвиг `end_date` не делаем (не знаем на сколько), просто активируем |
| Подписка заморожена и `end_date` наступает | `get_expired_subscriptions()` фильтрует `is_frozen = False` → крон истечения игнорирует |
| Автопродление при заморозке | Ранний возврат в `_auto_extend_subscription()` → не срабатывает |
| Пополнение баланса при заморозке | `try_auto_extend_expired_after_topup()` проверяет `is_frozen` → не срабатывает |
| Суточная подписка + попытка заморозки | Предусловие 6 (`is_daily_paused`) блокирует; суточная не заморажива |
| Admin-разморозка замороженной через бота подписки | Те же `unfreeze_subscription()`, идемпотентны |
| Миграция: существующие строки | Новые поля: `is_frozen = false`, остальные NULL → консистентно |

---

## 16. Что покрыть тестами

### Юнит-тесты `SubscriptionService`

- `freeze_subscription()` — успешный путь: проверить изменение полей модели (`is_frozen`, `frozen_at`, `status`, `frozen_days_banked`, `frozen_auto_unfreeze_at`).
- `freeze_subscription()` — каждое предусловие из раздела 6: отдельный тест с соответствующим кодом ошибки.
- `unfreeze_subscription(reason='manual')` — сдвиг `end_date` на корректный интервал.
- `unfreeze_subscription(reason='auto')` — проверить обнуление freeze-полей.
- `unfreeze_subscription()` при незамороженной подписке — идемпотентный возврат без ошибки.
- `_auto_extend_subscription()` при `is_frozen=True` — ранний возврат (автопродление не выполнено).

### Интеграционные тесты

- API `POST /cabinet/subscription/freeze` — 200 при корректных данных, 422 при нарушении предусловия.
- API `POST /cabinet/subscription/unfreeze` — 200 и корректный `new_end_date`.
- `_check_frozen_subscriptions_for_auto_unfreeze()` — подписка с `frozen_auto_unfreeze_at` в прошлом разморажива, с датой в будущем — нет.
- `_check_expired_subscriptions()` — замороженная подписка с `end_date < now` не переходит в EXPIRED.

### Тесты миграции

- `upgrade()` / `downgrade()` без ошибок на тестовой БД.
- Существующие строки после `upgrade()` имеют `is_frozen = false`.
