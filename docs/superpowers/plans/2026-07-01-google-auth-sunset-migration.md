# Google Auth Sunset Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать всем пользователям с привязанным Google способ входа без Google — по кнопке (в кабинете и в боте) разослать им письмо с персональной долгоживущей ссылкой «Задать пароль» — до отключения Google-входа (ограничение РФ с 2026-07-07).

**Architecture:** Новый изолированный сервис `GoogleMigrationService` (бэкенд бота) на каждого Google-пользователя выпускает персональный долгоживущий reset-токен, ставит `email_verified=True` и шлёт письмо с ссылкой `{CABINET_URL}/reset-password?token=...`. Триггеры — admin-эндпоинт кабинета и кнопка в Telegram-админке бота, оба зовут один сервис. Переиспользуются готовые: flow reset-пароля, лендинг `ResetPassword.tsx`, `email_service`.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy async / aiogram (бот) — репо `remnawave-bedolaga-telegram-bot`. React / TS / react-query / i18next — репо `bedolaga-cabinet`.

## Global Constraints

- Два репозитория: **бэкенд** `/Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot` (бот + API кабинета), **фронтенд** `/Users/mihail/Desktop/Serv/bedolaga-cabinet`.
- Коммиты: **описательные** (заголовок + тело: что и зачем). **НИКОГДА** не добавлять trailer `Co-Authored-By`.
- После правок `.py`: `python3 -m py_compile <files>`; для модулей с `register_handlers` — импорт-тест.
- Роуты кабинета монтируются под префиксом `/cabinet` (итоговый путь `/cabinet/admin/google-migration/...`).
- Аудитория рассылки: `User.google_id IS NOT NULL AND User.email IS NOT NULL` (≈278). `google_id` **не изменять** (отключение Google — отдельная фаза 2).
- Токен инвайта — **долгоживущий**: `GOOGLE_MIGRATION_TOKEN_EXPIRE_DAYS` (дефолт 30), НЕ дефолтный 1 час.
- При отправке инвайта ставить `email_verified=True` (иначе вход по email+паролю не пройдёт: `app/cabinet/routes/auth.py:79`).
- Текст письма = предоставленный владельцем + встроенная персональная кнопка «🔐 Задать пароль» (`{{set_password_url}}`).
- Reset-эндпоинт и лендинг НЕ меняем (переиспользуем как есть).

---

## File Structure

**Бэкенд (`remnawave-bedolaga-telegram-bot`):**
- Modify `app/config.py` — новые настройки.
- Modify `app/cabinet/auth/email_verification.py` — helper срока токена.
- Modify `app/database/crud/user.py` — выборка + статистика Google-юзеров.
- Create `app/services/google_migration_service.py` — сервис рассылки + текст письма.
- Create `app/cabinet/routes/admin_google_migration.py` — admin-эндпоинты.
- Modify `app/cabinet/routes/__init__.py` — регистрация роутера.
- Create `app/handlers/admin/google_migration.py` — кнопка/хендлеры бота.
- Modify `app/handlers/admin/main.py` (+ `app/keyboards/admin.py`) — пункт меню и регистрация хендлеров.
- Tests: `tests/crud/test_google_migration_crud.py`, `tests/cabinet/test_google_migration_service.py`, `tests/cabinet/test_admin_google_migration_routes.py`, `tests/handlers/test_google_migration_bot.py`.

**Фронтенд (`bedolaga-cabinet`):**
- Create `src/api/adminGoogleMigration.ts` — API-клиент.
- Create `src/pages/AdminGoogleMigration.tsx` — страница.
- Modify `src/App.tsx` — lazy-import + роут.
- Modify `src/pages/AdminPanel.tsx` — ссылка на страницу.
- Modify `src/locales/ru.json`, `src/locales/en.json` — строки.

---

## Task 1: Config + helper срока токена

**Files:**
- Modify: `app/config.py` (класс `Settings`, рядом с `CABINET_PASSWORD_RESET_EXPIRE_HOURS` ~строка 1199)
- Modify: `app/cabinet/auth/email_verification.py` (рядом с `get_password_reset_expires_at`, ~строка 61)
- Test: `tests/test_google_migration_config.py`

**Interfaces:**
- Produces: `settings.GOOGLE_MIGRATION_TOKEN_EXPIRE_DAYS: int` (default 30), `settings.GOOGLE_AUTH_ENABLED: bool` (default True, задел для фазы 2), `email_verification.get_google_migration_token_expires_at() -> datetime`.

- [ ] **Step 1: Failing test**

```python
# tests/test_google_migration_config.py
from datetime import UTC, datetime, timedelta


def test_settings_have_google_migration_defaults():
    from app.config import settings
    assert settings.GOOGLE_MIGRATION_TOKEN_EXPIRE_DAYS == 30
    assert settings.GOOGLE_AUTH_ENABLED is True


def test_token_expiry_is_far_in_future():
    from app.cabinet.auth.email_verification import get_google_migration_token_expires_at
    expires = get_google_migration_token_expires_at()
    delta = expires - datetime.now(UTC)
    assert delta > timedelta(days=29)
```

- [ ] **Step 2: Run → fail**

Run: `python3 -m pytest tests/test_google_migration_config.py -v`
Expected: FAIL (`AttributeError: ... GOOGLE_MIGRATION_TOKEN_EXPIRE_DAYS`).

- [ ] **Step 3: Implement**

В `app/config.py`, рядом с `CABINET_PASSWORD_RESET_EXPIRE_HOURS: int = 1`:

```python
    # Google auth sunset migration (RF legal requirement, 2026-07-07).
    GOOGLE_MIGRATION_TOKEN_EXPIRE_DAYS: int = 30
    GOOGLE_AUTH_ENABLED: bool = True  # Phase 2 kill-switch; not enforced yet.
```

В `app/cabinet/auth/email_verification.py`, после `get_password_reset_expires_at`:

```python
def get_google_migration_token_expires_at() -> datetime:
    """Long-lived expiry for the Google-sunset set-password invite token."""
    days = max(1, settings.GOOGLE_MIGRATION_TOKEN_EXPIRE_DAYS)
    return datetime.now(UTC) + timedelta(days=days)
```

(Проверить, что `timedelta`, `UTC`, `datetime`, `settings` уже импортированы в файле — они используются выше; если нет — добавить.)

- [ ] **Step 4: Run → pass**

Run: `python3 -m pytest tests/test_google_migration_config.py -v` → PASS
Then: `python3 -m py_compile app/config.py app/cabinet/auth/email_verification.py`

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/cabinet/auth/email_verification.py tests/test_google_migration_config.py
git commit -m "feat(google-migration): настройки и долгоживущий срок токена инвайта

Добавлены GOOGLE_MIGRATION_TOKEN_EXPIRE_DAYS (30) и задел GOOGLE_AUTH_ENABLED
для будущего отключения Google-входа, плюс helper срока жизни токена
set-password инвайта (в отличие от дефолтного 1 часа для обычного сброса)."
```

---

## Task 2: CRUD — выборка и статистика Google-юзеров

**Files:**
- Modify: `app/database/crud/user.py` (рядом с `OAUTH_PROVIDER_COLUMNS` / oauth-функциями, ~строка 1564+)
- Test: `tests/crud/test_google_migration_crud.py`

**Interfaces:**
- Consumes: модель `User` (`google_id`, `email`, `auth_type`, `password_hash`).
- Produces:
  - `get_google_linked_users(db) -> list[User]` — все с `google_id IS NOT NULL AND email IS NOT NULL`.
  - `get_google_migration_stats(db) -> dict` — ключи `total`, `google_only`, `with_password` (int).

- [ ] **Step 1: Failing test**

```python
# tests/crud/test_google_migration_crud.py
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database.models import Base, User
from app.database.crud.user import get_google_linked_users, get_google_migration_stats


@pytest.fixture
async def session():
    engine = create_async_engine('sqlite+aiosqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _add(s, **kw):
    defaults = dict(referral_code=f"r{kw.get('email','x')}", balance_kopeks=0, promo_group_id=None)
    defaults.update(kw)
    u = User(**defaults)
    s.add(u)
    await s.commit()
    return u


@pytest.mark.asyncio
async def test_get_google_linked_users_filters(session):
    await _add(session, email='a@gmail.com', google_id='111', auth_type='google')
    await _add(session, email='b@gmail.com', google_id='222', auth_type='telegram', telegram_id=5)
    await _add(session, email=None, google_id='333', auth_type='google')  # no email -> excluded
    await _add(session, email='c@ya.ru', google_id=None, auth_type='email')  # no google -> excluded

    users = await get_google_linked_users(session)
    emails = {u.email for u in users}
    assert emails == {'a@gmail.com', 'b@gmail.com'}


@pytest.mark.asyncio
async def test_stats_counts(session):
    await _add(session, email='a@gmail.com', google_id='111', auth_type='google')
    await _add(session, email='b@gmail.com', google_id='222', auth_type='telegram', telegram_id=5)
    await _add(session, email='c@gmail.com', google_id='333', auth_type='google', password_hash='x')

    stats = await get_google_migration_stats(session)
    assert stats == {'total': 3, 'google_only': 2, 'with_password': 1}
```

- [ ] **Step 2: Run → fail**

Run: `python3 -m pytest tests/crud/test_google_migration_crud.py -v`
Expected: FAIL (`ImportError` / функции не существуют).

- [ ] **Step 3: Implement**

В `app/database/crud/user.py` (убедиться, что `func` из sqlalchemy импортирован — он используется в файле; `select` тоже):

```python
async def get_google_linked_users(db: AsyncSession) -> list[User]:
    """All users with a linked Google account and an email (migration audience)."""
    result = await db.execute(
        select(User).where(User.google_id.isnot(None), User.email.isnot(None))
    )
    return list(result.scalars().all())


async def get_google_migration_stats(db: AsyncSession) -> dict[str, int]:
    """Counts for the Google-sunset migration admin dashboard."""
    base = (User.google_id.isnot(None), User.email.isnot(None))
    total = await db.scalar(select(func.count()).select_from(User).where(*base))
    google_only = await db.scalar(
        select(func.count()).select_from(User).where(*base, User.auth_type == 'google')
    )
    with_password = await db.scalar(
        select(func.count()).select_from(User).where(*base, User.password_hash.isnot(None))
    )
    return {
        'total': int(total or 0),
        'google_only': int(google_only or 0),
        'with_password': int(with_password or 0),
    }
```

- [ ] **Step 4: Run → pass**

Run: `python3 -m pytest tests/crud/test_google_migration_crud.py -v` → PASS
Then: `python3 -m py_compile app/database/crud/user.py`

- [ ] **Step 5: Commit**

```bash
git add app/database/crud/user.py tests/crud/test_google_migration_crud.py
git commit -m "feat(google-migration): CRUD выборки и статистики Google-юзеров

get_google_linked_users — аудитория рассылки (google_id + email),
get_google_migration_stats — счётчики total/google_only/with_password
для админ-дашборда миграции."
```

---

## Task 3: GoogleMigrationService + текст письма

**Files:**
- Create: `app/services/google_migration_service.py`
- Test: `tests/cabinet/test_google_migration_service.py`

**Interfaces:**
- Consumes: `get_google_linked_users` (Task 2), `generate_password_reset_token` + `get_google_migration_token_expires_at` (Task 1), `email_service.send_email`, `AsyncSessionLocal`, `settings.CABINET_URL`.
- Produces:
  - `build_invite_email(set_password_url: str, username: str) -> tuple[str, str]` — `(subject, html)`.
  - синглтон `google_migration_service` с методами `async start() -> bool` (False если уже идёт), `get_status() -> dict` (ключи `running,total,sent,failed,started_at,finished_at`).

- [ ] **Step 1: Failing test**

```python
# tests/cabinet/test_google_migration_service.py
import pytest

from app.services import google_migration_service as gm


def test_build_invite_email_contains_link_and_username():
    subject, html = gm.build_invite_email('https://cab.example/reset-password?token=abc', 'Иван')
    assert subject
    assert 'https://cab.example/reset-password?token=abc' in html
    assert 'Иван' in html
    # placeholders fully substituted
    assert '{{set_password_url}}' not in html
    assert '{{username}}' not in html


@pytest.mark.asyncio
async def test_start_is_single_flight(monkeypatch):
    service = gm.GoogleMigrationService()
    service._status.running = True  # simulate in-progress run
    started = await service.start()
    assert started is False
```

- [ ] **Step 2: Run → fail**

Run: `python3 -m pytest tests/cabinet/test_google_migration_service.py -v`
Expected: FAIL (module/functions missing).

- [ ] **Step 3: Implement**

```python
# app/services/google_migration_service.py
"""One-off migration: email Google-linked users a personal, long-lived
set-password link before Google login is disabled (RF law, 2026-07-07)."""

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import structlog

from app.cabinet.auth.email_verification import (
    generate_password_reset_token,
    get_google_migration_token_expires_at,
)
from app.cabinet.services.email_service import email_service
from app.config import settings
from app.database.crud.user import get_google_linked_users
from app.database.database import AsyncSessionLocal

logger = structlog.get_logger(__name__)

EMAIL_RATE_LIMIT = 8  # emails per second (matches EmailBroadcastService)
_SEND_INTERVAL = 1.0 / EMAIL_RATE_LIMIT

DEFAULT_SUBJECT = '⚠️ Важно: вход через Google скоро отключится — задайте пароль'

_DEFAULT_HTML = """\
<div style="font-family:Arial,sans-serif;font-size:15px;line-height:1.6;color:#222;max-width:560px;margin:0 auto">
  <p>⚠️ <b>Друзья, важное — не пропустите!</b></p>
  <p>С 7 июля вход через <b>Google ID</b> и <b>Apple ID</b> больше не будет работать — такие теперь ограничения 😔</p>
  <p>Чтобы не остаться без доступа к своему кабинету — привяжите другой способ входа уже сейчас 👇</p>
  <p>✅ Telegram&nbsp;&nbsp;✅ Яндекс ID&nbsp;&nbsp;✅ Обычная почта</p>
  <p>🔐 А можно просто задать пароль для своей почты — и спокойно входить по логину и паролю.</p>
  <p style="text-align:center;margin:28px 0">
    <a href="{{set_password_url}}" style="background:#4f46e5;color:#fff;text-decoration:none;padding:14px 28px;border-radius:10px;font-weight:bold;display:inline-block">🔐 Задать пароль</a>
  </p>
  <p>👉 Также всё можно сделать в личном кабинете → раздел «Профиль». Займёт меньше минуты!</p>
  <p>❗️ Если вы входите только через Google или Apple — не тяните! Без привязки после 7 июля войти не получится 🙏</p>
  <p>Позаботьтесь о доступе заранее — и всё будет работать без сбоев! 💪</p>
  <p>— EasyTunel ❤️</p>
</div>"""


def build_invite_email(set_password_url: str, username: str) -> tuple[str, str]:
    """Render (subject, html) for the set-password invite."""
    html = _DEFAULT_HTML.replace('{{set_password_url}}', set_password_url).replace('{{username}}', username or '')
    return DEFAULT_SUBJECT, html


@dataclass
class _Status:
    running: bool = False
    total: int = 0
    sent: int = 0
    failed: int = 0
    started_at: str | None = None
    finished_at: str | None = None


class GoogleMigrationService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._status = _Status()
        self._task: asyncio.Task | None = None

    async def start(self) -> bool:
        """Kick off the background send. Returns False if already running."""
        async with self._lock:
            if self._status.running:
                return False
            self._status = _Status(running=True, started_at=datetime.now(UTC).isoformat())
            self._task = asyncio.create_task(self._run(), name='google-migration')
            return True

    def get_status(self) -> dict:
        return asdict(self._status)

    async def _run(self) -> None:
        try:
            async with AsyncSessionLocal() as session:
                users = await get_google_linked_users(session)
                user_ids = [u.id for u in users]
            self._status.total = len(user_ids)
            for user_id in user_ids:
                ok = await self._process_user(user_id)
                if ok:
                    self._status.sent += 1
                else:
                    self._status.failed += 1
                await asyncio.sleep(_SEND_INTERVAL)
        except Exception as exc:  # never leave status stuck as running
            logger.exception('Google migration run crashed', exc=exc)
        finally:
            self._status.running = False
            self._status.finished_at = datetime.now(UTC).isoformat()

    async def _process_user(self, user_id: int) -> bool:
        from app.database.models import User

        try:
            async with AsyncSessionLocal() as session:
                user = await session.get(User, user_id)
                if user is None or not user.email:
                    return False
                token = generate_password_reset_token()
                user.password_reset_token = token
                user.password_reset_expires = get_google_migration_token_expires_at()
                user.email_verified = True
                user.email_verified_at = datetime.now(UTC)
                await session.commit()
                email = user.email
                username = user.first_name or ''
            set_password_url = f'{settings.CABINET_URL}/reset-password?token={token}'
            subject, html = build_invite_email(set_password_url, username)
            return bool(await asyncio.to_thread(email_service.send_email, email, subject, html))
        except Exception as exc:
            logger.warning('Failed to send Google-migration invite', user_id=user_id, exc=exc)
            return False


google_migration_service = GoogleMigrationService()
```

- [ ] **Step 4: Run → pass**

Run: `python3 -m pytest tests/cabinet/test_google_migration_service.py -v` → PASS
Then: `python3 -m py_compile app/services/google_migration_service.py`

- [ ] **Step 5: Commit**

```bash
git add app/services/google_migration_service.py tests/cabinet/test_google_migration_service.py
git commit -m "feat(google-migration): сервис рассылки инвайтов на задание пароля

GoogleMigrationService выпускает на каждого Google-юзера персональный
долгоживущий reset-токен, ставит email_verified=True и шлёт письмо с
кнопкой-magic-link на лендинг задания пароля. Троттлинг 8 писем/сек,
single-flight, статус прогресса. google_id не трогается."
```

---

## Task 4: Admin-эндпоинты кабинета (status/send)

**Files:**
- Create: `app/cabinet/routes/admin_google_migration.py`
- Modify: `app/cabinet/routes/__init__.py` (импорт + `router.include_router(...)`)
- Test: `tests/cabinet/test_admin_google_migration_routes.py`

**Interfaces:**
- Consumes: `get_google_migration_stats` (Task 2), `google_migration_service` (Task 3), `require_permission`, `get_cabinet_db`.
- Produces endpoints (под префиксом `/cabinet`):
  - `GET /cabinet/admin/google-migration/status` → `{ stats: {total,google_only,with_password}, run: {running,total,sent,failed,started_at,finished_at} }`
  - `POST /cabinet/admin/google-migration/send` → `{ started: bool }`

- [ ] **Step 1: Failing test**

```python
# tests/cabinet/test_admin_google_migration_routes.py
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_routes_registered():
    from app.cabinet.routes import router
    paths = {r.path: r.methods for r in router.routes}
    assert '/cabinet/admin/google-migration/status' in paths
    assert 'GET' in paths['/cabinet/admin/google-migration/status']
    assert 'POST' in paths['/cabinet/admin/google-migration/send']


@pytest.mark.asyncio
async def test_send_starts_service(monkeypatch):
    from app.cabinet.routes import admin_google_migration as mod
    monkeypatch.setattr(mod.google_migration_service, 'start', AsyncMock(return_value=True))
    resp = await mod.send_invites(admin=SimpleNamespace(id=1), db=AsyncMock())
    assert resp == {'started': True}


@pytest.mark.asyncio
async def test_status_returns_stats(monkeypatch):
    from app.cabinet.routes import admin_google_migration as mod
    monkeypatch.setattr(mod, 'get_google_migration_stats', AsyncMock(return_value={'total': 3, 'google_only': 2, 'with_password': 1}))
    monkeypatch.setattr(mod.google_migration_service, 'get_status', lambda: {'running': False, 'total': 0, 'sent': 0, 'failed': 0, 'started_at': None, 'finished_at': None})
    resp = await mod.get_migration_status(admin=SimpleNamespace(id=1), db=AsyncMock())
    assert resp['stats']['total'] == 3
    assert resp['run']['running'] is False
```

- [ ] **Step 2: Run → fail**

Run: `python3 -m pytest tests/cabinet/test_admin_google_migration_routes.py -v`
Expected: FAIL (module missing / routes not registered).

- [ ] **Step 3: Implement**

```python
# app/cabinet/routes/admin_google_migration.py
"""Admin endpoints to trigger the Google-sunset set-password invite campaign."""

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.user import get_google_migration_stats
from app.database.models import User
from app.services.google_migration_service import google_migration_service

from ..dependencies import get_cabinet_db, require_permission

logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/google-migration', tags=['admin-google-migration'])


@router.get('/status')
async def get_migration_status(
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    stats = await get_google_migration_stats(db)
    return {'stats': stats, 'run': google_migration_service.get_status()}


@router.post('/send')
async def send_invites(
    admin: User = Depends(require_permission('broadcasts:send')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    started = await google_migration_service.start()
    logger.info('Google migration invites triggered', admin_id=getattr(admin, 'id', None), started=started)
    return {'started': started}
```

В `app/cabinet/routes/__init__.py` (рядом с другими admin-роутерами): добавить импорт
`from .admin_google_migration import router as admin_google_migration_router`
и строку `router.include_router(admin_google_migration_router)` рядом с остальными `include_router` (например возле `admin_broadcasts_router`).

> Примечание: путь `/cabinet/...` формируется потому, что верхнеуровневый `router` монтируется с префиксом `/cabinet` (см. существующие роуты вроде `/cabinet/admin/overpay/certificate`). Внутри модуля указываем только `/admin/google-migration`.

- [ ] **Step 4: Run → pass**

Run: `python3 -m pytest tests/cabinet/test_admin_google_migration_routes.py -v` → PASS
Then: `python3 -m py_compile app/cabinet/routes/admin_google_migration.py app/cabinet/routes/__init__.py`

- [ ] **Step 5: Commit**

```bash
git add app/cabinet/routes/admin_google_migration.py app/cabinet/routes/__init__.py tests/cabinet/test_admin_google_migration_routes.py
git commit -m "feat(google-migration): admin-эндпоинты кабинета status/send

GET /cabinet/admin/google-migration/status — счётчики + статус рассылки,
POST .../send — запуск рассылки инвайтов. Под RBAC broadcasts:read/send."
```

---

## Task 5: Кнопка в Telegram-админке бота

**Files:**
- Create: `app/handlers/admin/google_migration.py`
- Modify: `app/handlers/admin/main.py` (вызов `register_handlers` нового модуля — по образцу других admin-модулей)
- Modify: `app/keyboards/admin.py` — добавить кнопку в подходящее admin-меню (например меню рассылок/сообщений) с `callback_data='admin_google_migration'`
- Test: `tests/handlers/test_google_migration_bot.py`

**Interfaces:**
- Consumes: `google_migration_service` (Task 3), `get_google_migration_stats` (Task 2), сессия БД (по образцу существующих admin-хендлеров), проверка админа (по образцу соседних модулей).
- Produces: `register_handlers(dp)` регистрирует два callback-хендлера: `admin_google_migration` (меню + счётчики + кнопка подтверждения), `admin_google_migration_send` (запуск).

- [ ] **Step 1: Failing test**

```python
# tests/handlers/test_google_migration_bot.py
from unittest.mock import AsyncMock

import pytest

from app.handlers.admin import google_migration as gm


@pytest.mark.asyncio
async def test_send_handler_starts_service(monkeypatch):
    monkeypatch.setattr(gm.google_migration_service, 'start', AsyncMock(return_value=True))
    callback = AsyncMock()
    callback.message = AsyncMock()
    await gm.handle_send_invites(callback)
    gm.google_migration_service.start.assert_awaited_once()
    callback.answer.assert_awaited()


def test_register_handlers_smoke():
    from aiogram import Dispatcher
    dp = Dispatcher()
    gm.register_handlers(dp)  # must not raise
```

- [ ] **Step 2: Run → fail**

Run: `python3 -m pytest tests/handlers/test_google_migration_bot.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

Изучить соседний простой admin-модуль (например `app/handlers/admin/nodes_restart.py` или `maintenance.py`) для точного паттерна: как получают сессию БД, как проверяют админа, как отвечают на callback. Затем:

```python
# app/handlers/admin/google_migration.py
"""Telegram admin button: trigger the Google-sunset set-password invite campaign."""

import structlog
from aiogram import Dispatcher, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.database.crud.user import get_google_migration_stats
from app.database.database import AsyncSessionLocal
from app.services.google_migration_service import google_migration_service

logger = structlog.get_logger(__name__)


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='✅ Отправить всем', callback_data='admin_google_migration_send')],
        [InlineKeyboardButton(text='◀️ Назад', callback_data='admin_messages')],
    ])


async def show_menu(callback: CallbackQuery) -> None:
    async with AsyncSessionLocal() as session:
        stats = await get_google_migration_stats(session)
    status = google_migration_service.get_status()
    text = (
        '📧 <b>Миграция Google-пользователей</b>\n\n'
        f'Всего с Google: <b>{stats["total"]}</b>\n'
        f'Только через Google: <b>{stats["google_only"]}</b>\n'
        f'Уже задали пароль: <b>{stats["with_password"]}</b>\n\n'
    )
    if status['running']:
        text += f'⏳ Идёт рассылка: {status["sent"]}/{status["total"]} (ошибок {status["failed"]})'
    elif status['finished_at']:
        text += f'✅ Последняя рассылка: отправлено {status["sent"]}, ошибок {status["failed"]}'
    text += '\n\nНажмите кнопку — всем этим пользователям уйдёт письмо с долгоживущей ссылкой на задание пароля.'
    await callback.message.edit_text(text, reply_markup=_confirm_keyboard())
    await callback.answer()


async def handle_send_invites(callback: CallbackQuery) -> None:
    started = await google_migration_service.start()
    if started:
        await callback.answer('Рассылка запущена ✅', show_alert=True)
    else:
        await callback.answer('Рассылка уже идёт ⏳', show_alert=True)


def register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(show_menu, F.data == 'admin_google_migration')
    dp.callback_query.register(handle_send_invites, F.data == 'admin_google_migration_send')
```

В `app/keyboards/admin.py` — добавить кнопку `InlineKeyboardButton(text='📧 Миграция Google', callback_data='admin_google_migration')` в admin-меню рассылок/сообщений (то, что открывается по `admin_messages`).

В `app/handlers/admin/main.py` — вызвать `register_handlers` нового модуля там, где регистрируются остальные admin-модули (найти по `from . import` / вызовам `register_handlers`).

- [ ] **Step 4: Run → pass + import-test**

Run: `python3 -m pytest tests/handlers/test_google_migration_bot.py -v` → PASS
Then:
```bash
python3 -m py_compile app/handlers/admin/google_migration.py app/handlers/admin/main.py app/keyboards/admin.py
python3 -c "from app.handlers.admin import google_migration; from aiogram import Dispatcher; google_migration.register_handlers(Dispatcher()); print('register OK')"
```

- [ ] **Step 5: Commit**

```bash
git add app/handlers/admin/google_migration.py app/handlers/admin/main.py app/keyboards/admin.py tests/handlers/test_google_migration_bot.py
git commit -m "feat(google-migration): кнопка запуска рассылки в Telegram-админке

Пункт меню '📧 Миграция Google' показывает счётчики и по подтверждению
запускает тот же GoogleMigrationService, что и кабинет."
```

---

## Task 6: Фронтенд API-клиент (кабинет)

**Files:**
- Create: `bedolaga-cabinet/src/api/adminGoogleMigration.ts`
- Test: (фронтенд-юнит-тестов в этом репо для api-клиентов нет — проверка через `npm run build` в Task 7)

**Interfaces:**
- Consumes: `apiClient` (`./client`).
- Produces:
  - `type GoogleMigrationStatus = { stats: {total:number; google_only:number; with_password:number}; run: {running:boolean; total:number; sent:number; failed:number; started_at:string|null; finished_at:string|null} }`
  - `adminGoogleMigrationApi.getStatus(): Promise<GoogleMigrationStatus>`
  - `adminGoogleMigrationApi.sendInvites(): Promise<{started:boolean}>`

- [ ] **Step 1: Implement**

```ts
// bedolaga-cabinet/src/api/adminGoogleMigration.ts
import apiClient from './client';

export interface GoogleMigrationStats {
  total: number;
  google_only: number;
  with_password: number;
}

export interface GoogleMigrationRun {
  running: boolean;
  total: number;
  sent: number;
  failed: number;
  started_at: string | null;
  finished_at: string | null;
}

export interface GoogleMigrationStatus {
  stats: GoogleMigrationStats;
  run: GoogleMigrationRun;
}

export const adminGoogleMigrationApi = {
  getStatus: async (): Promise<GoogleMigrationStatus> => {
    const { data } = await apiClient.get<GoogleMigrationStatus>('/admin/google-migration/status');
    return data;
  },
  sendInvites: async (): Promise<{ started: boolean }> => {
    const { data } = await apiClient.post<{ started: boolean }>('/admin/google-migration/send');
    return data;
  },
};
```

> Проверить в `src/api/client.ts`, включает ли baseURL уже префикс `/cabinet`. Если да — путь остаётся `/admin/...`; если нет — добавить `/cabinet` в начало (сравнить с путями в `src/api/adminBroadcasts.ts`).

- [ ] **Step 2: Commit**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
git add src/api/adminGoogleMigration.ts
git commit -m "feat(google-migration): API-клиент миграции Google в кабинете

getStatus/sendInvites для admin-эндпоинтов /admin/google-migration."
```

---

## Task 7: Фронтенд страница + роут + меню + локали

**Files:**
- Create: `bedolaga-cabinet/src/pages/AdminGoogleMigration.tsx`
- Modify: `bedolaga-cabinet/src/App.tsx` (lazy-import + `<Route>`)
- Modify: `bedolaga-cabinet/src/pages/AdminPanel.tsx` (ссылка-карточка на страницу)
- Modify: `bedolaga-cabinet/src/locales/ru.json`, `src/locales/en.json`
- Test: `npm run build` (сборка проходит)

**Interfaces:**
- Consumes: `adminGoogleMigrationApi` (Task 6), `react-query`, `useTranslation`.

- [ ] **Step 1: Implement страницу**

```tsx
// bedolaga-cabinet/src/pages/AdminGoogleMigration.tsx
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { adminGoogleMigrationApi } from '../api/adminGoogleMigration';

export default function AdminGoogleMigration() {
  const { t } = useTranslation();
  const qc = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ['google-migration-status'],
    queryFn: adminGoogleMigrationApi.getStatus,
    refetchInterval: (q) => (q.state.data?.run.running ? 2000 : false),
  });

  const send = useMutation({
    mutationFn: adminGoogleMigrationApi.sendInvites,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['google-migration-status'] }),
  });

  const stats = data?.stats;
  const run = data?.run;

  return (
    <div className="mx-auto max-w-2xl p-4">
      <h1 className="mb-4 text-2xl font-bold">{t('googleMigration.title')}</h1>
      <p className="mb-4 text-dark-500">{t('googleMigration.description')}</p>

      {isLoading ? (
        <div>{t('common.loading')}</div>
      ) : (
        <div className="space-y-4">
          <div className="grid grid-cols-3 gap-3">
            <Stat label={t('googleMigration.total')} value={stats?.total ?? 0} />
            <Stat label={t('googleMigration.googleOnly')} value={stats?.google_only ?? 0} />
            <Stat label={t('googleMigration.withPassword')} value={stats?.with_password ?? 0} />
          </div>

          {run?.running && (
            <div className="rounded-lg bg-dark-800 p-3 text-sm">
              {t('googleMigration.inProgress', { sent: run.sent, total: run.total, failed: run.failed })}
            </div>
          )}
          {!run?.running && run?.finished_at && (
            <div className="rounded-lg bg-dark-800 p-3 text-sm">
              {t('googleMigration.lastRun', { sent: run.sent, failed: run.failed })}
            </div>
          )}

          <button
            className="btn-primary"
            disabled={send.isPending || run?.running}
            onClick={() => {
              if (window.confirm(t('googleMigration.confirm', { count: stats?.total ?? 0 }))) send.mutate();
            }}
          >
            {run?.running ? t('googleMigration.sending') : t('googleMigration.sendButton')}
          </button>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg bg-dark-800 p-4 text-center">
      <div className="text-3xl font-bold text-dark-100">{value}</div>
      <div className="mt-1 text-xs text-dark-500">{label}</div>
    </div>
  );
}
```

> Классы (`btn-primary`, `bg-dark-800`, `text-dark-*`) — сверить с существующими admin-страницами (например `AdminBroadcasts`/`AdminPanel`) и заменить на актуальные, если отличаются.

- [ ] **Step 2: Роут в `src/App.tsx`**

Рядом с другими admin lazy-import (около строки 80):
```ts
const AdminGoogleMigration = lazyWithRetry(() => import('./pages/AdminGoogleMigration'));
```
В admin-секции роутов (рядом с маршрутом AdminBroadcasts) добавить `<Route>` по образцу соседних admin-роутов (тот же guard/layout), путь `admin/google-migration`.

- [ ] **Step 3: Ссылка в `src/pages/AdminPanel.tsx`**

Добавить карточку/пункт, ведущий на `/admin/google-migration` (по образцу существующих карточек AdminPanel), подпись `t('googleMigration.title')`.

- [ ] **Step 4: Локали**

В `src/locales/ru.json` добавить блок:
```json
"googleMigration": {
  "title": "Миграция Google",
  "description": "Рассылка пользователям с входом через Google: письмо с долгоживущей ссылкой на задание пароля перед отключением Google-входа.",
  "total": "Всего с Google",
  "googleOnly": "Только Google",
  "withPassword": "Уже с паролем",
  "sendButton": "Отправить инвайты всем",
  "sending": "Идёт рассылка…",
  "inProgress": "Идёт рассылка: {{sent}}/{{total}} (ошибок {{failed}})",
  "lastRun": "Последняя рассылка: отправлено {{sent}}, ошибок {{failed}}",
  "confirm": "Отправить письмо с ссылкой на задание пароля всем ({{count}})?"
}
```
В `src/locales/en.json` — англоязычный эквивалент с теми же ключами.

- [ ] **Step 5: Сборка**

Run:
```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet && npm run build
```
Expected: успешная сборка без TS-ошибок.

- [ ] **Step 6: Commit**

```bash
git add src/pages/AdminGoogleMigration.tsx src/App.tsx src/pages/AdminPanel.tsx src/locales/ru.json src/locales/en.json
git commit -m "feat(google-migration): страница миграции Google в админке кабинета

Счётчики (всего с Google / только Google / уже с паролем), кнопка запуска
рассылки инвайтов на задание пароля с прогрессом, роут и пункт меню, локали."
```

---

## Task 8: Список «не мигрировавших» Google-юзеров (backend)

> Контекст: bounce-письма (452 переполнен / 550 нет ящика / 554 policy) приходят **асинхронно** на `noreply@easytunel.space` уже после того, как релей принял письмо — в коде рассылки их не поймать. Поэтому «кого не достали» определяем **по поведению**: `google_id` есть, а пароль не задан. Аналог существующего списка заблокировавших бота (`blocked_users_service`).

**Files:**
- Modify: `app/database/crud/user.py`
- Modify: `app/cabinet/routes/admin_google_migration.py` (+ эндпоинт)
- Test: `tests/crud/test_google_migration_crud.py` (дополнить), `tests/cabinet/test_admin_google_migration_routes.py` (дополнить)

**Interfaces:**
- Produces:
  - `get_google_at_risk_users(db) -> list[dict]` — элементы `{id:int, email:str, auth_type:str, has_telegram:bool, blocked_bot:bool}` для юзеров с `google_id IS NOT NULL AND email IS NOT NULL AND password_hash IS NULL`.
  - `GET /cabinet/admin/google-migration/at-risk` → `{ count:int, users:list[...] }`.

- [ ] **Step 1: Failing test (CRUD)**

Дополнить `tests/crud/test_google_migration_crud.py`:

```python
@pytest.mark.asyncio
async def test_at_risk_users(session):
    from app.database.crud.user import get_google_at_risk_users
    # migrated (has password) -> excluded
    await _add(session, email='ok@gmail.com', google_id='1', auth_type='google', password_hash='x')
    # at risk, blocked the bot, no telegram
    await _add(session, email='bad@gmail.com', google_id='2', auth_type='google', status='blocked')
    # at risk but has telegram
    await _add(session, email='tg@gmail.com', google_id='3', auth_type='telegram', telegram_id=7)

    rows = await get_google_at_risk_users(session)
    by_email = {r['email']: r for r in rows}
    assert 'ok@gmail.com' not in by_email
    assert by_email['bad@gmail.com']['blocked_bot'] is True
    assert by_email['bad@gmail.com']['has_telegram'] is False
    assert by_email['tg@gmail.com']['has_telegram'] is True
```

- [ ] **Step 2: Run → fail**

Run: `python3 -m pytest tests/crud/test_google_migration_crud.py::test_at_risk_users -v` → FAIL (ImportError).

- [ ] **Step 3: Implement CRUD**

В `app/database/crud/user.py`:

```python
async def get_google_at_risk_users(db: AsyncSession) -> list[dict]:
    """Google-linked users who have NOT set a password yet (didn't migrate).

    Flags whether they also blocked the bot / lack Telegram — the hard cases
    unreachable by BOTH email and Telegram.
    """
    result = await db.execute(
        select(User).where(
            User.google_id.isnot(None),
            User.email.isnot(None),
            User.password_hash.is_(None),
        )
    )
    return [
        {
            'id': u.id,
            'email': u.email,
            'auth_type': u.auth_type,
            'has_telegram': u.telegram_id is not None,
            'blocked_bot': u.status == 'blocked',
        }
        for u in result.scalars().all()
    ]
```

- [ ] **Step 4: Endpoint + test**

В `app/cabinet/routes/admin_google_migration.py` добавить импорт `get_google_at_risk_users` и эндпоинт:

```python
@router.get('/at-risk')
async def get_at_risk_users(
    admin: User = Depends(require_permission('broadcasts:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    users = await get_google_at_risk_users(db)
    return {'count': len(users), 'users': users}
```

Дополнить `tests/cabinet/test_admin_google_migration_routes.py`:

```python
def test_at_risk_route_registered():
    from app.cabinet.routes import router
    paths = {r.path: r.methods for r in router.routes}
    assert 'GET' in paths['/cabinet/admin/google-migration/at-risk']


@pytest.mark.asyncio
async def test_at_risk_returns_list(monkeypatch):
    from app.cabinet.routes import admin_google_migration as mod
    monkeypatch.setattr(mod, 'get_google_at_risk_users', AsyncMock(return_value=[{'id': 1, 'email': 'a@b.c', 'auth_type': 'google', 'has_telegram': False, 'blocked_bot': True}]))
    resp = await mod.get_at_risk_users(admin=SimpleNamespace(id=1), db=AsyncMock())
    assert resp['count'] == 1
    assert resp['users'][0]['blocked_bot'] is True
```

- [ ] **Step 5: Run → pass**

Run: `python3 -m pytest tests/crud/test_google_migration_crud.py tests/cabinet/test_admin_google_migration_routes.py -v` → PASS
Then: `python3 -m py_compile app/database/crud/user.py app/cabinet/routes/admin_google_migration.py`

- [ ] **Step 6: Commit**

```bash
git add app/database/crud/user.py app/cabinet/routes/admin_google_migration.py tests/crud/test_google_migration_crud.py tests/cabinet/test_admin_google_migration_routes.py
git commit -m "feat(google-migration): список не мигрировавших Google-юзеров

get_google_at_risk_users + GET /at-risk: google_id есть, пароль не задан.
Отметки has_telegram/blocked_bot — видно, кого не достать ни почтой,
ни ботом (аналог списка заблокировавших бота). Bounce'ы асинхронны и в
момент отправки не ловятся, поэтому учёт по поведению."
```

---

## Task 9: Список «не мигрировавших» в UI (кабинет + бот)

**Files:**
- Modify: `bedolaga-cabinet/src/api/adminGoogleMigration.ts` (+ `getAtRisk`)
- Modify: `bedolaga-cabinet/src/pages/AdminGoogleMigration.tsx` (секция списка)
- Modify: `bedolaga-cabinet/src/locales/ru.json`, `src/locales/en.json`
- Modify: `app/handlers/admin/google_migration.py` (строка счётчика at-risk в меню бота)

**Interfaces:**
- Consumes: `GET /admin/google-migration/at-risk` (Task 8).

- [ ] **Step 1: API-метод**

В `src/api/adminGoogleMigration.ts` добавить:

```ts
export interface GoogleAtRiskUser {
  id: number;
  email: string;
  auth_type: string;
  has_telegram: boolean;
  blocked_bot: boolean;
}

// в объект adminGoogleMigrationApi:
  getAtRisk: async (): Promise<{ count: number; users: GoogleAtRiskUser[] }> => {
    const { data } = await apiClient.get<{ count: number; users: GoogleAtRiskUser[] }>('/admin/google-migration/at-risk');
    return data;
  },
```

- [ ] **Step 2: Секция списка в `AdminGoogleMigration.tsx`**

Добавить запрос и таблицу под блоком статистики:

```tsx
  const atRisk = useQuery({ queryKey: ['google-at-risk'], queryFn: adminGoogleMigrationApi.getAtRisk });
```

```tsx
      <h2 className="mt-8 mb-2 text-lg font-semibold">
        {t('googleMigration.atRiskTitle')} ({atRisk.data?.count ?? 0})
      </h2>
      <div className="max-h-96 overflow-auto rounded-lg border border-dark-700">
        {(atRisk.data?.users ?? []).map((u) => (
          <div key={u.id} className="flex items-center justify-between border-b border-dark-800 px-3 py-2 text-sm">
            <span>{u.email}</span>
            <span className="flex gap-2">
              {!u.has_telegram && <span className="rounded bg-amber-900 px-2 py-0.5 text-xs">{t('googleMigration.noTelegram')}</span>}
              {u.blocked_bot && <span className="rounded bg-red-900 px-2 py-0.5 text-xs">{t('googleMigration.blockedBot')}</span>}
            </span>
          </div>
        ))}
      </div>
```

- [ ] **Step 3: Локали**

Добавить в `googleMigration` (ru.json):
```json
"atRiskTitle": "Не задали пароль",
"noTelegram": "нет Telegram",
"blockedBot": "заблокировал бота"
```
И английские эквиваленты в en.json.

- [ ] **Step 4: Счётчик в боте**

В `app/handlers/admin/google_migration.py::show_menu` добавить после блока со stats строку с числом «не задали пароль» = `stats['total'] - stats['with_password']`:

```python
    at_risk = stats['total'] - stats['with_password']
    text += f'❗️ Не задали пароль: <b>{at_risk}</b>\n'
```
(вставить до строки с призывом нажать кнопку).

- [ ] **Step 5: Сборка + компиляция**

Run:
```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet && npm run build
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot && python3 -m py_compile app/handlers/admin/google_migration.py
```
Expected: сборка ок, компиляция ок.

- [ ] **Step 6: Commit (два репо)**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
git add src/api/adminGoogleMigration.ts src/pages/AdminGoogleMigration.tsx src/locales/ru.json src/locales/en.json
git commit -m "feat(google-migration): список не мигрировавших в кабинете

Таблица google-юзеров без пароля с бейджами 'нет Telegram' / 'заблокировал
бота' — видно, кого не достать ни одним каналом."

cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
git add app/handlers/admin/google_migration.py
git commit -m "feat(google-migration): счётчик не задавших пароль в меню бота"
```

---

## Self-Review (выполнено при написании плана)

**Покрытие спеки:** CRUD выборки+статистики (Task 2) ✓; сервис с персональным долгим токеном + email_verified=True + текст письма (Task 1,3) ✓; admin-триггер кабинета (Task 4) ✓; кнопка в боте (Task 5, добавлено по требованию) ✓; фронт-страница/статус (Task 6,7) ✓; список «не мигрировавших» по поведению + отметки нет-TG/заблокировал-бота (Task 8,9, добавлено по требованию — вместо ловли асинхронных bounce'ов) ✓; лендинг/reset переиспользуются без изменений ✓; `google_id` не трогается ✓; Yandex/VK не затрагиваются ✓. Фаза 2 (отключение Google) — вне плана, есть задел `GOOGLE_AUTH_ENABLED`.

**Не делаем сейчас (осознанно):** парсинг bounce-писем по IMAP (отдельная подсистема, фаза 2) — вместо этого поведенческий список; временные bounce'ы (452/554) могут доставиться при повторном запуске рассылки, постоянные (550) видны как «не задали пароль».

**Плейсхолдеры:** конкретный код во всех шагах; отмечены только точки сверки классов/путей с существующим кодом (не TODO-заглушки).

**Согласованность типов:** `get_google_migration_stats` возвращает `{total,google_only,with_password}` — эти же ключи в роуте (Task 4), боте (Task 5), фронте (Task 6,7). `google_migration_service.start()/get_status()` — единый контракт для кабинета и бота. `set_password_url` формируется как `{CABINET_URL}/reset-password?token=...` — совпадает с существующим reset-flow.
