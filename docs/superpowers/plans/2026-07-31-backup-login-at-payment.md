# План: Резервный метод входа при оплате (фича B)

**Дата:** 2026-07-31
**Спека:** `docs/superpowers/specs/2026-07-31-backup-login-at-payment-design.md`
**Статус:** готов к реализации

---

## Goal

После успешной оплаты подписки (в боте и в кабинете) мягко предлагать пользователю привязать
резервный метод входа, если у него ≤ 1 метода. Позитивный посыл: «Привяжи вход — и сможешь
заходить на сайт и продлевать подписку в любой момент». Предложение закрываемое, не блокирует
покупку.

---

## Architecture

```
app/services/account_merge_service.py  ← нет изменений (compute_auth_methods уже есть)
app/cabinet/routes/account_linking.py  ← добавить needs_backup_login() + GET /cabinet/backup-login-suggestion

app/handlers/subscription/purchase.py   ← confirm_purchase() — инъекция after success
app/handlers/subscription/tariff_purchase.py ← confirm_tariff_purchase() — инъекция after success

src/api/auth.ts                         ← getBackupLoginSuggestion()
src/components/SuccessNotificationModal.tsx ← показывать backup-login плашку (dismissible)
src/locales/ru.json, en.json            ← новые ключи backupLogin.*
```

---

## Tech Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy async, aiogram 3, structlog.
- **Backend tests:** `.venv/bin/python3 -m pytest`, `pytest-asyncio`, SimpleNamespace-mock-паттерн (без БД).
- **Frontend:** React + TypeScript, Vite, vitest, react-i18next, Zustand, axios (`apiClient`).
- **Frontend lint:** `npx tsc --noEmit && npm run build && npx vitest run`.

---

## Global Constraints

| Ограничение | Значение |
|---|---|
| Bot tests | `.venv/bin/python3 -m pytest` |
| Env secrets | никогда не коммитить `.env` (PUBLIC repo) |
| Commit messages | русский, заголовок + тело |
| Co-Authored-By | ЗАПРЕЩЕНО |
| Frontend checks | `npx tsc --noEmit && npm run build && npx vitest run` |
| Locale keys | только `ru.json` + `en.json` (fa/zh не трогать); `fallbackLng='ru'` |
| Связанные экраны | переиспользуем ConnectedAccounts (`/profile/accounts`) и bot link-* — новую логику привязки НЕ пишем |
| Блокировать покупку | НЕЛЬЗЯ — только soft/dismissible |
| Триггер | только оплата подписки, не пополнение баланса |
| Условие показа | только при ≤ 1 методе входа |
| Fork | наши правки приоритетнее upstream; перед мержем спрашивать подтверждение |

---

## Ключевые находки из кодовой базы

### `compute_auth_methods` (источник правды)

Находится в `app/services/account_merge_service.py:130`:
```python
def compute_auth_methods(user: User) -> list[str]:
    methods: list[str] = []
    if user.telegram_id:
        methods.append('telegram')
    if user.email and user.password_hash:
        methods.append('email')
    for provider, column in OAUTH_PROVIDER_COLUMNS.items():
        if getattr(user, column, None):
            methods.append(provider)
    return methods
```

`_count_auth_methods(user)` в `account_linking.py:223` — это `len(compute_auth_methods(user))`.
Новый хелпер `needs_backup_login` размещаем рядом с `_count_auth_methods` в `account_linking.py`.

### Точки инъекции в боте (success points)

1. **`confirm_purchase`** в `app/handlers/subscription/purchase.py:2197` — классический режим.
   Успех: строки 2887 (`callback.message.edit_text(success_text, ...)`) и 2892 (fallback без ссылки).
   Оба пути сливаются к `purchase_completed = True` (строка 2900). Инъекция: **после обоих
   `edit_text`-вызовов** и перед `purchase_completed = True`, когда `db_user` и `callback.bot`
   доступны.

2. **`confirm_tariff_purchase`** в `app/handlers/subscription/tariff_purchase.py:1636` — режим тарифов.
   Успех: строки 2048–2079 (`callback.message.edit_text(TARIFF_PURCHASE_SUCCESS, ...)`).
   Инъекция: **после** этого `edit_text`, когда `db_user`, `callback.bot`, `db` доступны.

В обоих случаях `callback.message.chat.id == db_user.telegram_id`, поэтому:
```python
await callback.bot.send_message(chat_id=db_user.telegram_id, ...)
```

### URL кнопки «Привязать вход на сайте»

`settings.CABINET_URL` (строка 1393 config.py) — базовый URL кабинета.
`settings._normalized_cabinet_url()` — нормализованный (убирает дефолт `https://example.com/cabinet`).
ConnectedAccounts маршрут в кабинете: `/profile/accounts` (App.tsx:509).

Кнопка строится так:
```python
cabinet_url = settings._normalized_cabinet_url()
if cabinet_url:
    linking_url = f"{cabinet_url}/profile/accounts"
```

Если `cabinet_url` не настроен — кнопку не показываем (бот не сломан).

### Роутер кабинета для нового эндпоинта

Новый эндпоинт добавляем в `app/cabinet/routes/account_linking.py`, в существующий `router`
(prefix=`/auth/account`). Итоговый путь: `GET /cabinet/auth/account/backup-login-suggestion`.
Зависимость `get_current_cabinet_user` уже импортирована в этом файле.
Регистрация через `account_linking_router` в `__init__.py:98` — ничего менять не нужно.

### Frontend: где показывать плашку

`SuccessNotificationModal` (`src/components/SuccessNotificationModal.tsx`) — глобальный портал,
показывается при `isOpen=true` через Zustand-стор `useSuccessNotification`. Он рендерится в
`AppShell.tsx:190` и показывается после ЛЮБОЙ подписки (`subscription_activated`,
`subscription_renewed`, `subscription_purchased`). Это единственный «экран успешной оплаты
подписки» в кабинете (WebSocket триггерит `show()` при получении события).

**Добавляем backup-login плашку внутрь `SuccessNotificationModal`**: после кнопок действий,
до кнопки «Закрыть», при `isSubscription === true` — делаем отдельный `useQuery` для
`getBackupLoginSuggestion()` и показываем dismissible-баннер если `needs_backup=true`.

ConnectedAccounts route: `/profile/accounts` (App.tsx:509).

---

## Tasks

---

### T1 — Хелпер `needs_backup_login` + unit-тест

**Файлы:**
- `app/cabinet/routes/account_linking.py` (добавить функцию после строки 225)
- `tests/cabinet/test_backup_login_helper.py` (новый)

**Interfaces:**

Консумирует:
```python
from app.services.account_merge_service import compute_auth_methods
from app.database.models import User
```

Производит:
```python
def needs_backup_login(user: User) -> bool:
    """True если у пользователя ≤ 1 метода входа."""
    return _count_auth_methods(user) <= 1
```

**Шаги:**

**T1.1 — Написать провальный тест**

Создать `tests/cabinet/test_backup_login_helper.py`:

```python
"""Тесты хелпера needs_backup_login."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# нельзя импортировать до добавления функции — тест должен падать
from app.cabinet.routes.account_linking import needs_backup_login


def _user(telegram_id=None, email=None, password_hash=None, yandex_id=None) -> SimpleNamespace:
    return SimpleNamespace(
        telegram_id=telegram_id,
        email=email,
        password_hash=password_hash,
        yandex_id=yandex_id,
        google_id=None,
        discord_id=None,
        vk_id=None,
    )


def test_needs_backup_login_single_telegram():
    """Только Telegram → True."""
    user = _user(telegram_id=123456)
    assert needs_backup_login(user) is True


def test_needs_backup_login_two_methods():
    """Telegram + email → False."""
    user = _user(telegram_id=123456, email='a@b.com', password_hash='hash')
    assert needs_backup_login(user) is False


def test_needs_backup_login_oauth_plus_telegram():
    """Telegram + Yandex OAuth → False."""
    user = _user(telegram_id=123456, yandex_id='ya_123')
    assert needs_backup_login(user) is False


def test_needs_backup_login_email_only():
    """Только email → True."""
    user = _user(email='a@b.com', password_hash='hash')
    assert needs_backup_login(user) is True


def test_needs_backup_login_zero_methods():
    """Ноль методов → True (edge case, аккаунт-зомби)."""
    user = _user()
    assert needs_backup_login(user) is True
```

Запустить и убедиться в ImportError/AttributeError:
```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
.venv/bin/python3 -m pytest tests/cabinet/test_backup_login_helper.py -v 2>&1 | head -20
```
Ожидаемый результат: `ImportError: cannot import name 'needs_backup_login'`.

**T1.2 — Реализовать хелпер**

В `app/cabinet/routes/account_linking.py`, после строки 225 (после `_count_auth_methods`):

```python
def needs_backup_login(user: User) -> bool:
    """True если у пользователя ≤ 1 метода входа — нужно предложить резервный."""
    return _count_auth_methods(user) <= 1
```

**T1.3 — Запустить тесты, добавить py_compile**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
python3 -m py_compile app/cabinet/routes/account_linking.py && echo OK
.venv/bin/python3 -m pytest tests/cabinet/test_backup_login_helper.py -v
```
Ожидаемый результат: `5 passed`.

**T1.4 — Коммит**

```
feat(auth): хелпер needs_backup_login для проверки числа методов входа

Добавлен needs_backup_login(user) -> bool в account_linking.py рядом
с _count_auth_methods. Возвращает True при ≤ 1 методе входа — единая
точка правды для T2 (эндпоинт) и T3 (бот).
```

---

### T2 — Эндпоинт `GET /cabinet/auth/account/backup-login-suggestion`

**Файлы:**
- `app/cabinet/routes/account_linking.py` (добавить схему + эндпоинт)
- `tests/cabinet/test_backup_login_endpoint.py` (новый)

**Interfaces:**

Консумирует:
```python
from app.cabinet.routes.account_linking import (
    needs_backup_login,
    router,           # APIRouter(prefix='/auth/account', ...)
)
from app.cabinet.dependencies import get_current_cabinet_user
from app.database.models import User
from fastapi import Depends
from pydantic import BaseModel
```

Производит:
```
GET /cabinet/auth/account/backup-login-suggestion
Authorization: Bearer <jwt>
→ 200 { "needs_backup": bool }
→ 401 если нет токена
```

**Шаги:**

**T2.1 — Написать провальные тесты**

Создать `tests/cabinet/test_backup_login_endpoint.py`:

```python
"""Тесты эндпоинта GET /cabinet/auth/account/backup-login-suggestion."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _user_single():
    """Пользователь с одним методом входа (Telegram)."""
    return SimpleNamespace(
        telegram_id=999, email=None, password_hash=None,
        yandex_id=None, google_id=None, discord_id=None, vk_id=None,
    )


def _user_two():
    """Пользователь с двумя методами (Telegram + email)."""
    return SimpleNamespace(
        telegram_id=999, email='u@example.com', password_hash='hash',
        yandex_id=None, google_id=None, discord_id=None, vk_id=None,
    )


@pytest.mark.asyncio
async def test_backup_suggestion_needs_backup_true():
    """Один метод → needs_backup=True."""
    from app.cabinet.routes.account_linking import get_backup_login_suggestion

    result = await get_backup_login_suggestion(user=_user_single())
    assert result.needs_backup is True


@pytest.mark.asyncio
async def test_backup_suggestion_needs_backup_false():
    """Два метода → needs_backup=False."""
    from app.cabinet.routes.account_linking import get_backup_login_suggestion

    result = await get_backup_login_suggestion(user=_user_two())
    assert result.needs_backup is False


@pytest.mark.asyncio
async def test_backup_suggestion_response_shape():
    """Ответ содержит поле needs_backup типа bool."""
    from app.cabinet.routes.account_linking import get_backup_login_suggestion

    result = await get_backup_login_suggestion(user=_user_single())
    assert hasattr(result, 'needs_backup')
    assert isinstance(result.needs_backup, bool)
```

Запустить — ожидается `ImportError` на `get_backup_login_suggestion`:
```bash
.venv/bin/python3 -m pytest tests/cabinet/test_backup_login_endpoint.py -v 2>&1 | head -20
```

**T2.2 — Добавить схему и эндпоинт**

В `app/cabinet/routes/account_linking.py`:

1. Добавить Pydantic-схему (рядом с другими схемами, например после `UnlinkResponse`):

```python
class BackupLoginSuggestionResponse(BaseModel):
    needs_backup: bool
```

2. Добавить эндпоинт в `router` (после `get_linked_providers`):

```python
@router.get('/backup-login-suggestion', response_model=BackupLoginSuggestionResponse)
async def get_backup_login_suggestion(
    user: User = Depends(get_current_cabinet_user),
) -> BackupLoginSuggestionResponse:
    """Возвращает, нужно ли предложить пользователю резервный метод входа.

    needs_backup=True если у пользователя ≤ 1 метода входа.
    Эндпоинт предназначен для кабинета и вызывается после успешной оплаты подписки.
    """
    return BackupLoginSuggestionResponse(needs_backup=needs_backup_login(user))
```

**T2.3 — Компиляция и тесты**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
python3 -m py_compile app/cabinet/routes/account_linking.py && echo OK
.venv/bin/python3 -m pytest tests/cabinet/test_backup_login_helper.py tests/cabinet/test_backup_login_endpoint.py -v
```
Ожидаемый результат: все тесты `passed`.

**T2.4 — Коммит**

```
feat(cabinet): GET /cabinet/auth/account/backup-login-suggestion

Новый эндпоинт под JWT-авторизацией возвращает {needs_backup: bool}.
Использует needs_backup_login(user) из T1. Нужен для фронтенда кабинета,
чтобы показывать плашку после оплаты подписки.
```

---

### T3 — Бот: сообщение о резервном входе после оплаты подписки

**Файлы:**
- `app/handlers/subscription/purchase.py` (инъекция в `confirm_purchase`)
- `app/handlers/subscription/tariff_purchase.py` (инъекция в `confirm_tariff_purchase`)
- `tests/handlers/test_backup_login_bot_message.py` (новый)

**Interfaces:**

Консумирует:
```python
from app.cabinet.routes.account_linking import needs_backup_login
from app.config import settings
# settings._normalized_cabinet_url() -> str | None
# callback.bot.send_message(chat_id, text, reply_markup, parse_mode)
```

Сообщение (текст — хардкод, т.к. бот не использует i18next):
```
🔐 Привяжи вход по почте или Яндексу — сможешь заходить на сайт и продлевать
подписку в любой момент (и не потеряешь аккаунт при смене Telegram).
```

Кнопка:
```
InlineKeyboardButton(text='🔗 Привязать вход на сайте', url=f'{cabinet_url}/profile/accounts')
```

Если `_normalized_cabinet_url()` вернул `None` → кнопку не добавляем, сообщение не отправляем
(не засорять бота неполезной плашкой без ссылки).

Вспомогательная функция (помещается в **обоих** файлах, поэтому выносится в отдельный модуль):

**Новый файл:** `app/handlers/subscription/backup_login_nudge.py`

```python
"""Мягкое предложение привязать резервный метод входа после оплаты подписки."""

import structlog
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.cabinet.routes.account_linking import needs_backup_login
from app.config import settings
from app.database.models import User

logger = structlog.get_logger(__name__)

_NUDGE_TEXT = (
    '🔐 Привяжи вход по почте или Яндексу — сможешь заходить на сайт '
    'и продлевать подписку в любой момент '
    '(и не потеряешь аккаунт при смене Telegram).'
)
_NUDGE_BUTTON_TEXT = '🔗 Привязать вход на сайте'


async def send_backup_login_nudge(bot: Bot, user: User) -> None:
    """Отправить мягкое предложение привязать резервный метод входа.

    Best-effort: любая ошибка логируется и игнорируется.
    Не отправляем, если:
    - у пользователя ≥ 2 методов входа
    - нет telegram_id (не можем отправить ЛС)
    - CABINET_URL не настроен
    """
    if not needs_backup_login(user):
        return
    if not user.telegram_id:
        return
    cabinet_url = settings._normalized_cabinet_url()
    if not cabinet_url:
        return

    linking_url = f'{cabinet_url}/profile/accounts'
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=_NUDGE_BUTTON_TEXT, url=linking_url)]
        ]
    )

    try:
        await bot.send_message(
            chat_id=user.telegram_id,
            text=_NUDGE_TEXT,
            reply_markup=keyboard,
        )
        logger.info(
            'Отправлено предложение резервного входа',
            user_id=user.id,
            telegram_id=user.telegram_id,
        )
    except Exception as exc:
        logger.warning(
            'Не удалось отправить предложение резервного входа (non-fatal)',
            user_id=user.id,
            error=str(exc),
        )
```

**Шаги:**

**T3.1 — Написать провальные тесты**

Создать `tests/handlers/test_backup_login_bot_message.py`:

```python
"""Тесты: бот отправляет nudge после оплаты подписки."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _user_single(telegram_id=111222333):
    return SimpleNamespace(
        id=1, telegram_id=telegram_id,
        email=None, password_hash=None,
        yandex_id=None, google_id=None, discord_id=None, vk_id=None,
    )


def _user_two(telegram_id=111222333):
    return SimpleNamespace(
        id=1, telegram_id=telegram_id,
        email='u@example.com', password_hash='hash',
        yandex_id=None, google_id=None, discord_id=None, vk_id=None,
    )


@pytest.mark.asyncio
async def test_nudge_sent_when_single_method():
    """Один метод входа → send_message вызывается."""
    from app.handlers.subscription.backup_login_nudge import send_backup_login_nudge

    bot = AsyncMock()
    bot.send_message = AsyncMock()

    with patch(
        'app.handlers.subscription.backup_login_nudge.settings._normalized_cabinet_url',
        return_value='https://example.com/cabinet',
    ):
        await send_backup_login_nudge(bot, _user_single())

    bot.send_message.assert_awaited_once()
    call_kwargs = bot.send_message.call_args
    assert call_kwargs.kwargs['chat_id'] == 111222333
    assert '/profile/accounts' in call_kwargs.kwargs['reply_markup'].inline_keyboard[0][0].url


@pytest.mark.asyncio
async def test_nudge_not_sent_when_two_methods():
    """Два метода → send_message НЕ вызывается."""
    from app.handlers.subscription.backup_login_nudge import send_backup_login_nudge

    bot = AsyncMock()
    bot.send_message = AsyncMock()

    with patch(
        'app.handlers.subscription.backup_login_nudge.settings._normalized_cabinet_url',
        return_value='https://example.com/cabinet',
    ):
        await send_backup_login_nudge(bot, _user_two())

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_nudge_not_sent_when_no_cabinet_url():
    """CABINET_URL не настроен → send_message НЕ вызывается."""
    from app.handlers.subscription.backup_login_nudge import send_backup_login_nudge

    bot = AsyncMock()
    bot.send_message = AsyncMock()

    with patch(
        'app.handlers.subscription.backup_login_nudge.settings._normalized_cabinet_url',
        return_value=None,
    ):
        await send_backup_login_nudge(bot, _user_single())

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_nudge_not_sent_when_no_telegram_id():
    """Нет telegram_id → send_message НЕ вызывается."""
    from app.handlers.subscription.backup_login_nudge import send_backup_login_nudge

    bot = AsyncMock()
    bot.send_message = AsyncMock()
    user_no_tg = _user_single(telegram_id=None)

    with patch(
        'app.handlers.subscription.backup_login_nudge.settings._normalized_cabinet_url',
        return_value='https://example.com/cabinet',
    ):
        await send_backup_login_nudge(bot, user_no_tg)

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_nudge_failure_does_not_raise():
    """Ошибка send_message не пробрасывается наружу (best-effort)."""
    from app.handlers.subscription.backup_login_nudge import send_backup_login_nudge

    bot = AsyncMock()
    bot.send_message = AsyncMock(side_effect=Exception('Telegram API error'))

    with patch(
        'app.handlers.subscription.backup_login_nudge.settings._normalized_cabinet_url',
        return_value='https://example.com/cabinet',
    ):
        # Не должно выбрасывать исключение
        await send_backup_login_nudge(bot, _user_single())

    bot.send_message.assert_awaited_once()
```

Запустить — ожидается `ImportError`:
```bash
.venv/bin/python3 -m pytest tests/handlers/test_backup_login_bot_message.py -v 2>&1 | head -20
```

**T3.2 — Создать `backup_login_nudge.py`**

Создать `app/handlers/subscription/backup_login_nudge.py` с кодом из раздела Interfaces выше.

Убедиться, что `tests/handlers/` существует:
```bash
ls /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot/tests/handlers/ 2>/dev/null || mkdir -p /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot/tests/handlers && touch /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot/tests/handlers/__init__.py
```

**T3.3 — Запустить тесты `backup_login_nudge`**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
python3 -m py_compile app/handlers/subscription/backup_login_nudge.py && echo OK
.venv/bin/python3 -m pytest tests/handlers/test_backup_login_bot_message.py -v
```
Ожидаемый результат: `5 passed`.

**T3.4 — Инъекция в `confirm_purchase`**

В `app/handlers/subscription/purchase.py`:

1. Добавить импорт вверху файла (рядом с другими handlers-импортами):

```python
from .backup_login_nudge import send_backup_login_nudge
```

2. Найти строку `purchase_completed = True` (строка 2900) внутри `confirm_purchase`.
   Вставить вызов ПЕРЕД ней, в обоих ветках (после `edit_text` с `success_text` и после
   `edit_text` с `SUBSCRIPTION_LINK_GENERATING_NOTICE`):

```python
        # best-effort: предлагаем резервный метод входа если у юзера ≤ 1 метода
        await db.refresh(db_user)
        await send_backup_login_nudge(callback.bot, db_user)

        purchase_completed = True
```

Примечание: `db.refresh(db_user)` уже вызывается на строке 2725, но к моменту success-ветки
пользователь актуален — дополнительный refresh перед nudge добавляем для надёжности.

**T3.5 — Инъекция в `confirm_tariff_purchase`**

В `app/handlers/subscription/tariff_purchase.py`:

1. Добавить импорт вверху файла:

```python
from .backup_login_nudge import send_backup_login_nudge
```

2. После блока `callback.message.edit_text(TARIFF_PURCHASE_SUCCESS, ...)` (строки 2048–2079),
   сразу за закрывающей скобкой блока `edit_text`, добавить:

```python
    # best-effort: предлагаем резервный метод входа если у юзера ≤ 1 метода
    await send_backup_login_nudge(callback.bot, db_user)
```

**T3.6 — py_compile оба файла**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
python3 -m py_compile app/handlers/subscription/purchase.py && echo purchase OK
python3 -m py_compile app/handlers/subscription/tariff_purchase.py && echo tariff OK
```

**T3.7 — Запустить все тесты**

```bash
.venv/bin/python3 -m pytest tests/cabinet/test_backup_login_helper.py \
  tests/cabinet/test_backup_login_endpoint.py \
  tests/handlers/test_backup_login_bot_message.py -v
```
Ожидаемый результат: `13 passed`.

**T3.8 — Коммит**

```
feat(bot): nudge-сообщение о резервном входе после оплаты подписки

Новый модуль backup_login_nudge.py — send_backup_login_nudge(bot, user).
Отправляет отдельное сообщение с кнопкой «Привязать вход на сайте»
(→ cabinet /profile/accounts) только если у юзера ≤ 1 метода входа.
Best-effort: ошибка отправки не ломает покупку. Инъекция в
confirm_purchase и confirm_tariff_purchase.
```

---

### T4 — Frontend: API + dismissible плашка в SuccessNotificationModal

**Файлы:**
- `src/api/auth.ts` (добавить `getBackupLoginSuggestion`)
- `src/components/SuccessNotificationModal.tsx` (добавить плашку)
- `src/locales/ru.json` (новые ключи `backupLogin.*`)
- `src/locales/en.json` (новые ключи `backupLogin.*`)
- `src/components/SuccessNotificationModal.test.tsx` (новый — unit-тест)

**Interfaces:**

**API-функция** (`src/api/auth.ts`):
```typescript
export interface BackupLoginSuggestionResponse {
  needs_backup: boolean;
}

export const getBackupLoginSuggestion = async (): Promise<BackupLoginSuggestionResponse> => {
  const response = await apiClient.get<BackupLoginSuggestionResponse>(
    '/cabinet/auth/account/backup-login-suggestion',
  );
  return response.data;
};
```

**Locale keys** (добавить в `auth` namespace):

`ru.json`:
```json
"backupLogin": {
  "title": "Привяжи резервный способ входа",
  "description": "Сможешь заходить на сайт и продлевать подписку в любой момент — даже если сменишь Telegram.",
  "linkButton": "Привязать",
  "dismissButton": "Позже"
}
```

`en.json`:
```json
"backupLogin": {
  "title": "Add a backup login method",
  "description": "You'll be able to access the site and renew your subscription anytime — even if you change your Telegram.",
  "linkButton": "Link account",
  "dismissButton": "Later"
}
```

**Компонент-плашка** (добавить в `SuccessNotificationModal.tsx`):

Логика:
- Рендерится только при `isSubscription === true`
- `useQuery` для `getBackupLoginSuggestion()` с `enabled: isOpen && isSubscription`
- `staleTime: 0` — каждый раз перезапрашивать при открытии
- При ошибке запроса — скрывать плашку (`onError` или `isError → false`)
- Локальный `useState<boolean>` `backupLoginDismissed` — сбрасывается при закрытии модала
- Показываем: `!backupLoginDismissed && data?.needs_backup === true`
- Кнопка «Привязать»: `navigate('/profile/accounts')` + `hide()` + `haptic.impact('light')`
- Кнопка «Позже»: `setBackupLoginDismissed(true)`

**Шаги:**

**T4.1 — Добавить locale keys**

В `src/locales/ru.json` внутри секции `"auth"`:

```json
"backupLogin": {
  "title": "Привяжи резервный способ входа",
  "description": "Сможешь заходить на сайт и продлевать подписку в любой момент — даже если сменишь Telegram.",
  "linkButton": "Привязать",
  "dismissButton": "Позже"
}
```

В `src/locales/en.json` внутри секции `"auth"`:

```json
"backupLogin": {
  "title": "Add a backup login method",
  "description": "You'll be able to access the site and renew your subscription anytime — even if you change your Telegram.",
  "linkButton": "Link account",
  "dismissButton": "Later"
}
```

Убедиться что в `fa.json` и `zh.json` изменений нет.

Запустить locale-тест:
```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx vitest run src/locales/locales.test.ts
```
Ожидаемый результат: `passed`.

**T4.2 — Добавить API-функцию**

В `src/api/auth.ts` добавить:

```typescript
export interface BackupLoginSuggestionResponse {
  needs_backup: boolean;
}

export const getBackupLoginSuggestion = async (): Promise<BackupLoginSuggestionResponse> => {
  const response = await apiClient.get<BackupLoginSuggestionResponse>(
    '/cabinet/auth/account/backup-login-suggestion',
  );
  return response.data;
};
```

**T4.3 — Написать провальный тест**

Создать `src/components/SuccessNotificationModal.test.tsx`:

```tsx
/**
 * Тесты backup-login плашки в SuccessNotificationModal.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router';
import { I18nextProvider } from 'react-i18next';
import i18n from '../i18n';
import SuccessNotificationModal from './SuccessNotificationModal';
import { useSuccessNotification } from '../store/successNotification';
import { getBackupLoginSuggestion } from '../api/auth';

vi.mock('../api/auth', () => ({
  getBackupLoginSuggestion: vi.fn(),
}));

// Мокаем хуки платформы
vi.mock('../hooks/useTelegramSDK', () => ({
  useTelegramSDK: () => ({
    safeAreaInset: { bottom: 0 },
    contentSafeAreaInset: { bottom: 0 },
    isTelegramWebApp: false,
  }),
}));

vi.mock('@/platform', () => ({
  useHaptic: () => ({
    notification: vi.fn(),
    impact: vi.fn(),
  }),
}));

vi.mock('../hooks/useFocusTrap', () => ({
  useFocusTrap: () => ({ current: null }),
}));

vi.mock('../hooks/useCurrency', () => ({
  useCurrency: () => ({
    formatAmount: (v: number) => v.toFixed(2),
    currencySymbol: '₽',
  }),
}));

function Wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <I18nextProvider i18n={i18n}>{children}</I18nextProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('SuccessNotificationModal — backup-login плашка', () => {
  it('показывает плашку при needs_backup=true после оплаты подписки', async () => {
    (getBackupLoginSuggestion as ReturnType<typeof vi.fn>).mockResolvedValue({
      needs_backup: true,
    });

    useSuccessNotification.setState({
      isOpen: true,
      data: { type: 'subscription_purchased' },
      closeOthersSignal: 0,
    });

    render(
      <Wrapper>
        <SuccessNotificationModal />
      </Wrapper>,
    );

    await waitFor(() => {
      expect(screen.getByText(/Привяжи|Add a backup/i)).toBeTruthy();
    });
  });

  it('НЕ показывает плашку при needs_backup=false', async () => {
    (getBackupLoginSuggestion as ReturnType<typeof vi.fn>).mockResolvedValue({
      needs_backup: false,
    });

    useSuccessNotification.setState({
      isOpen: true,
      data: { type: 'subscription_purchased' },
      closeOthersSignal: 0,
    });

    render(
      <Wrapper>
        <SuccessNotificationModal />
      </Wrapper>,
    );

    await waitFor(() => {
      // кнопка «Привязать» не должна появиться
      expect(screen.queryByText(/Привязать|Link account/i)).toBeNull();
    });
  });

  it('закрывает плашку при клике «Позже»', async () => {
    (getBackupLoginSuggestion as ReturnType<typeof vi.fn>).mockResolvedValue({
      needs_backup: true,
    });

    useSuccessNotification.setState({
      isOpen: true,
      data: { type: 'subscription_renewed' },
      closeOthersSignal: 0,
    });

    render(
      <Wrapper>
        <SuccessNotificationModal />
      </Wrapper>,
    );

    await waitFor(() => screen.getByText(/Позже|Later/i));
    fireEvent.click(screen.getByText(/Позже|Later/i));

    await waitFor(() => {
      expect(screen.queryByText(/Привяжи|Add a backup/i)).toBeNull();
    });
  });

  it('НЕ показывает плашку при пополнении баланса (balance_topup)', async () => {
    (getBackupLoginSuggestion as ReturnType<typeof vi.fn>).mockResolvedValue({
      needs_backup: true,
    });

    useSuccessNotification.setState({
      isOpen: true,
      data: { type: 'balance_topup' },
      closeOthersSignal: 0,
    });

    render(
      <Wrapper>
        <SuccessNotificationModal />
      </Wrapper>,
    );

    // Небольшая пауза чтобы query успел завершиться
    await new Promise((r) => setTimeout(r, 50));

    expect(screen.queryByText(/Привяжи|Add a backup/i)).toBeNull();
  });

  it('скрывает плашку при ошибке API (не ломает модал)', async () => {
    (getBackupLoginSuggestion as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('network error'),
    );

    useSuccessNotification.setState({
      isOpen: true,
      data: { type: 'subscription_purchased' },
      closeOthersSignal: 0,
    });

    render(
      <Wrapper>
        <SuccessNotificationModal />
      </Wrapper>,
    );

    await new Promise((r) => setTimeout(r, 100));

    // Плашка не показывается, модал не падает
    expect(screen.queryByText(/Привяжи|Add a backup/i)).toBeNull();
    // Основной заголовок модала по-прежнему виден
    expect(screen.getByRole('dialog')).toBeTruthy();
  });
});
```

Запустить — ожидается провал (компонент не содержит плашку):
```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx vitest run src/components/SuccessNotificationModal.test.tsx 2>&1 | tail -20
```

**T4.4 — Добавить плашку в `SuccessNotificationModal.tsx`**

1. Добавить импорты в начало файла:

```tsx
import { getBackupLoginSuggestion } from '../api/auth';
```

2. В теле компонента `SuccessNotificationModal`, добавить состояние и query ПОСЛЕ строки
   `const haptic = useHaptic();`:

```tsx
  const [backupLoginDismissed, setBackupLoginDismissed] = useState(false);

  // Сброс dismiss при закрытии модала
  useEffect(() => {
    if (!isOpen) {
      setBackupLoginDismissed(false);
    }
  }, [isOpen]);

  const { data: backupData } = useQuery({
    queryKey: ['backup-login-suggestion'],
    queryFn: getBackupLoginSuggestion,
    enabled: isOpen && isSubscription,
    staleTime: 0,
    retry: false,
  });

  const showBackupLoginBanner =
    !backupLoginDismissed &&
    backupData?.needs_backup === true &&
    isSubscription;

  const handleGoToConnectedAccounts = useCallback(() => {
    hide();
    haptic.impact('light');
    navigate('/profile/accounts');
  }, [hide, haptic, navigate]);
```

3. Добавить переменную `isSubscription` (она уже есть в компоненте с строки 113 — убедиться
   что используем её в `enabled`-поле query).

4. В JSX, внутри `<div className="space-y-4 p-6">`, перед блоком кнопок `<div className="space-y-2 pt-2">`,
   добавить backup-login баннер:

```tsx
          {/* Backup login suggestion */}
          {showBackupLoginBanner && (
            <div className="rounded-xl border border-accent-500/20 bg-accent-500/5 p-4">
              <p className="mb-1 font-semibold text-accent-300">
                {t('auth.backupLogin.title', 'Привяжи резервный способ входа')}
              </p>
              <p className="mb-3 text-sm text-dark-400">
                {t(
                  'auth.backupLogin.description',
                  'Сможешь заходить на сайт и продлевать подписку в любой момент.',
                )}
              </p>
              <div className="flex gap-2">
                <button
                  onClick={handleGoToConnectedAccounts}
                  className="flex-1 rounded-xl bg-accent-500 py-2.5 text-sm font-bold text-on-accent transition-colors hover:bg-accent-400"
                >
                  {t('auth.backupLogin.linkButton', 'Привязать')}
                </button>
                <button
                  onClick={() => setBackupLoginDismissed(true)}
                  className="flex-1 rounded-xl bg-dark-800 py-2.5 text-sm font-semibold text-dark-400 transition-colors hover:bg-dark-700 hover:text-dark-200"
                >
                  {t('auth.backupLogin.dismissButton', 'Позже')}
                </button>
              </div>
            </div>
          )}
```

Убедиться что `isSubscription` определена ДО нового `useQuery`.

**T4.5 — Проверка TypeScript и сборка**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx tsc --noEmit 2>&1 | head -30
npm run build 2>&1 | tail -10
```
Ожидаемый результат: без ошибок.

**T4.6 — Запустить vitest**

```bash
npx vitest run 2>&1 | tail -20
```
Ожидаемый результат: все тесты `passed`, в т.ч. новый `SuccessNotificationModal.test.tsx` (5 тестов).

**T4.7 — Коммит (frontend)**

В репозитории `bedolaga-cabinet`:

```
feat(cabinet): плашка резервного входа после оплаты подписки

В SuccessNotificationModal: при isSubscription && needs_backup=true
показываем dismissible-баннер «Привяжи резервный способ входа» с
кнопкой «Привязать» (→ /profile/accounts) и «Позже». Новый API
getBackupLoginSuggestion() в auth.ts. Новые ключи auth.backupLogin.*
в ru.json и en.json.
```

---

## Порядок выполнения

```
T1 → T2 → T3 → T4
```

T1 и T2 не зависят от frontend. T3 зависит от T1 (импортирует `needs_backup_login`).
T4 зависит от T2 (вызывает эндпоинт).

---

## Self-Review

### Покрытие требований спеки

| Требование | Выполнено |
|---|---|
| `needs_backup_login(user)` = `_count_auth_methods(user) <= 1` | T1 |
| DRY: хелпер рядом с `_count_auth_methods` | T1 (account_linking.py) |
| `GET /cabinet/backup-login-suggestion` → `{needs_backup: bool}` | T2 |
| Бот: сообщение после покупки подписки (классический режим) | T3 → confirm_purchase |
| Бот: сообщение после покупки тарифа (tariff режим) | T3 → confirm_tariff_purchase |
| Бот: best-effort, сбой не ломает покупку | T3 (try/except в send_backup_login_nudge) |
| Бот: не отправлять при ≥ 2 методах | T3 (needs_backup_login check) |
| Бот: кнопка → ConnectedAccounts `/profile/accounts` | T3 |
| Бот: не отправлять без CABINET_URL | T3 |
| Frontend: `getBackupLoginSuggestion()` | T4 |
| Frontend: dismissible плашка при `needs_backup=true` | T4 |
| Frontend: «Привязать» → `/profile/accounts` | T4 |
| Frontend: «Позже» закрывает плашку | T4 |
| Frontend: не показывать при `balance_topup` | T4 (enabled только при isSubscription) |
| Frontend: ошибка API → не показывать (не блокировать) | T4 |
| Locale keys только ru.json + en.json | T4 |
| tsc + build + vitest проходят | T4.5 + T4.6 |
| py_compile после каждой правки .py | каждый шаг |

### Сканирование плейсхолдеров

- `<TODO>`, `<YOUR_URL>`, `<REPLACE>` — не найдено.
- Все пути абсолютные, все имена символов взяты из реального кода.

### Консистентность типов и имён

- Хелпер: `needs_backup_login` (snake_case, Python) / `needsBackup` в JSON (camelCase, JSON convention).
- Pydantic-схема: `BackupLoginSuggestionResponse` (consistent с `LinkCallbackResponse`, `UnlinkResponse`).
- TypeScript interface: `BackupLoginSuggestionResponse.needs_backup: boolean` (snake_case от backend).
- Locale namespace: `auth.backupLogin.*` (существующий namespace `auth`, новый под-ключ).
- Эндпоинт-путь: `/cabinet/auth/account/backup-login-suggestion` (в `router` prefix `/auth/account`).

### Разрешённые неоднозначности спеки

1. **Точка инъекции в боте (confirm_purchase):** Спека говорит «после сообщения об успехе».
   В `confirm_purchase` два финальных `edit_text`: с ссылкой (строка 2887) и без ссылки (строка 2892).
   Инъекция вынесена в единое место — перед `purchase_completed = True` (строка 2900), т.е.
   после обоих ветвей. Это гарантирует что nudge отправляется при любом успешном сценарии.

2. **Confirm_tariff_purchase (tariff mode):** Вызов nudge добавляется после `edit_text` с
   `TARIFF_PURCHASE_SUCCESS` (строки 2048–2079 tariff_purchase.py). `confirm_daily_tariff_purchase`
   (суточный тариф) — аналогичная инъекция по той же логике (после его success-edit_text),
   но это выходит за рамки T3.8; при необходимости — отдельный коммит.

3. **Frontend: место плашки.** Спека говорит «экран успешной оплаты подписки». В кабинете
   единый экран успеха — `SuccessNotificationModal` (глобальный портал), который показывается
   при всех типах `subscription_*` через WebSocket-нотификации. Это правильное место — оно
   не зависит от конкретного пути оплаты (классика / тариф / Telegram Stars).

4. **Cabinet URL для кнопки бота:** `settings._normalized_cabinet_url()` — приватный метод,
   но уже используется в `app/cabinet/utils/links.py:20`, `app/cabinet/auth/oauth_providers.py:518`
   и других местах. Использование в `backup_login_nudge.py` консистентно с проектом.
