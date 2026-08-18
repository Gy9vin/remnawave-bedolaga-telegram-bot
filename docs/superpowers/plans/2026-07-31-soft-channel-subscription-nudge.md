# Soft Channel Subscription Nudge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement soft mandatory channel subscription (gate screen stays, VPN never disabled) plus a dismissible cabinet/bot notification card that drives active users to read each new main-channel post exactly once.

**Architecture:** Feature A only (no payment-backup). New `is_main` flag on `RequiredChannel` identifies exactly one "main" channel whose `channel_post` Telegram events are captured by the bot to populate `last_post_*` fields. `CHANNEL_SOFT_MODE=True` short-circuits the deactivation path in both `channel_checker.py` and `channel_member.py`. The bot nudge card shows after the gate passes; the cabinet nudge is a non-blocking modal populated by a new `GET /cabinet/channel-nudge` endpoint. Per-user de-dup is tracked via `User.last_seen_channel_post_id`.

**Tech Stack:** Python 3.13 / aiogram 3 / FastAPI / SQLAlchemy async / Alembic (numeric IDs in `migrations/alembic/versions/`); React 18 / TypeScript / Vite / Vitest / i18next (ru+en only); pytest (`\`.venv/bin/python3 -m pytest\`).

## Global Constraints

- Bot tests run with: `.venv/bin/python3 -m pytest`
- Frontend passes: `npx tsc --noEmit` + `npm run build` + `npx vitest run` (in `/Users/mihail/Desktop/Serv/bedolaga-cabinet`)
- **NEVER commit `.env`** — PUBLIC repo
- Commit messages in **RUSSIAN** — заголовок + тело (что и зачем)
- **NO `Co-Authored-By`** trailer in any commit
- Soft mode default **ON** (`CHANNEL_SOFT_MODE: bool = True`)
- **Exactly one** `is_main` channel enforced at DB/API level (setting one clears others)
- Post notification shows **once per post** via `last_seen_channel_post_id`
- Fork: never restructure upstream files; our changes take priority
- Locale files: only `src/locales/ru.json` and `src/locales/en.json`; never touch `fa` / `zh`
- Alembic migration IDs: our-side numeric `9\d{3}` pattern; current head is `9024`; next is `9025`
- Settings patching in tests: `monkeypatch.setattr(settings, 'KEY', value)` (not frozen pydantic)
- Cabinet test pattern: import the route module directly, call handler functions with `AsyncMock()` db and `SimpleNamespace()` user

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `app/database/models.py` | Modify | Add `RequiredChannel.is_main`, `last_post_*` fields; `User.last_seen_channel_post_id` |
| `migrations/alembic/versions/9025_channel_soft_nudge.py` | Create | Alembic migration for new columns |
| `app/config.py` | Modify | Add `CHANNEL_SOFT_MODE: bool = True` |
| `app/middlewares/channel_checker.py` | Modify | Short-circuit `_deactivate_subscription_on_unsubscribe` in soft mode |
| `app/handlers/channel_member.py` | Modify | Short-circuit leave-deactivation in soft mode |
| `app/services/channel_subscription_service.py` | Modify | Short-circuit `should_disable_subscription`; add `is_main` to cache dict; add `get_main_channel()` |
| `app/handlers/channel_post.py` | Create | `channel_post` handler: update `last_post_*` for the main channel |
| `app/handlers/main_menu.py` (or whichever calls the gate) | Identify+Modify | Show post nudge card after gate passes |
| `app/database/crud/required_channel.py` | Modify | Add `set_main_channel()`, extend `_UPDATABLE_FIELDS` with `is_main` + `last_post_*` |
| `app/cabinet/routes/channel_nudge.py` | Create | `GET /cabinet/channel-nudge` + `POST /cabinet/channel-nudge/seen` |
| `app/cabinet/routes/__init__.py` | Modify | Include `channel_nudge_router` |
| `app/cabinet/schemas/channel.py` | Modify | Add `is_main` / `last_post_*` to `ChannelResponse`; add `ChannelNudgeResponse` |
| `app/cabinet/routes/admin_channels.py` | Modify | Add `POST /{id}/set-main` endpoint |
| `app/database/crud/required_channel.py` | Modify (continued) | `set_main_channel()` — clear others, set one |
| `tests/services/test_channel_soft_mode.py` | Create | Unit tests: soft mode + post card logic |
| `tests/cabinet/test_channel_nudge_routes.py` | Create | Unit tests: nudge endpoints |
| `tests/cabinet/test_admin_channel_is_main.py` | Create | Unit tests: is_main enforcement |
| `src/api/adminChannels.ts` | Modify | Add `is_main`, `last_post_*` to `RequiredChannel`; add `setMain()` |
| `src/api/channelNudge.ts` | Create | `getChannelNudge()`, `markChannelPostSeen()` |
| `src/components/ChannelNudgeModal.tsx` | Create | Dismissible popup with post + subscribe blocks |
| `src/components/layout/AppShell/AppShell.tsx` | Modify | Mount `<ChannelNudgeModal />` |
| `src/pages/AdminChannelSubscriptions.tsx` | Modify | `is_main` toggle in channel card; show latest post read-only |
| `src/locales/ru.json` | Modify | Add `channelNudge.*` keys |
| `src/locales/en.json` | Modify | Add `channelNudge.*` keys |
| `src/components/ChannelNudgeModal.test.tsx` | Create | Vitest tests for popup behaviour |

---

### Task 1: Model + Alembic Migration

**Files:**
- Modify: `app/database/models.py` (class `RequiredChannel` ~line 4308, class `User` ~line 2069)
- Create: `migrations/alembic/versions/9025_channel_soft_nudge.py`
- Test: `tests/test_channel_nudge_model_columns.py`

**Interfaces:**
- Produces:
  - `RequiredChannel.is_main: Column(Boolean, nullable=False, server_default='false')`
  - `RequiredChannel.last_post_message_id: Column(Integer, nullable=True)`
  - `RequiredChannel.last_post_link: Column(String(500), nullable=True)`
  - `RequiredChannel.last_post_title: Column(String(200), nullable=True)`
  - `RequiredChannel.last_post_at: Column(AwareDateTime(), nullable=True)`
  - `User.last_seen_channel_post_id: Column(Integer, nullable=True)`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_channel_nudge_model_columns.py
"""Smoke test: new columns exist on the ORM models before migration runs."""
import pytest


def test_required_channel_has_is_main_column():
    from app.database.models import RequiredChannel
    assert hasattr(RequiredChannel, 'is_main'), 'RequiredChannel.is_main missing'


def test_required_channel_has_last_post_columns():
    from app.database.models import RequiredChannel
    for col in ('last_post_message_id', 'last_post_link', 'last_post_title', 'last_post_at'):
        assert hasattr(RequiredChannel, col), f'RequiredChannel.{col} missing'


def test_user_has_last_seen_channel_post_id():
    from app.database.models import User
    assert hasattr(User, 'last_seen_channel_post_id'), 'User.last_seen_channel_post_id missing'
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
.venv/bin/python3 -m pytest tests/test_channel_nudge_model_columns.py -v
```

Expected: `FAILED — AssertionError: RequiredChannel.is_main missing`

- [ ] **Step 3: Add columns to `app/database/models.py`**

In `class RequiredChannel(Base):` (line 4308), after `disable_paid_on_leave` (line 4320), add:

```python
    is_main = Column(Boolean, nullable=False, server_default='false')
    last_post_message_id = Column(Integer, nullable=True)
    last_post_link = Column(String(500), nullable=True)
    last_post_title = Column(String(200), nullable=True)
    last_post_at = Column(AwareDateTime(), nullable=True)
```

In `class User(Base):` (line 2069), after `last_pinned_message_id` (~line 2181), add:

```python
    last_seen_channel_post_id = Column(Integer, nullable=True)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python3 -m pytest tests/test_channel_nudge_model_columns.py -v
```

Expected: `3 passed`

- [ ] **Step 5: Create Alembic migration**

Check current head first:
```bash
.venv/bin/python3 -m alembic heads
```
Expected: `9024 (head)`

Create `migrations/alembic/versions/9025_channel_soft_nudge.py`:

```python
"""add is_main + last_post fields to required_channels; add last_seen_channel_post_id to users

Adds soft-mode nudge columns:
- required_channels.is_main (bool, default false) — marks the single main channel
- required_channels.last_post_message_id, last_post_link, last_post_title, last_post_at
  — cached latest channel post for the nudge card
- users.last_seen_channel_post_id — per-user de-dup: last post id seen in nudge

Revision ID: 9025
Revises: 9024
Create Date: 2026-07-31

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '9025'
down_revision: Union[str, Sequence[str], None] = '9024'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'required_channels',
        sa.Column('is_main', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.add_column(
        'required_channels',
        sa.Column('last_post_message_id', sa.Integer(), nullable=True),
    )
    op.add_column(
        'required_channels',
        sa.Column('last_post_link', sa.String(length=500), nullable=True),
    )
    op.add_column(
        'required_channels',
        sa.Column('last_post_title', sa.String(length=200), nullable=True),
    )
    op.add_column(
        'required_channels',
        sa.Column('last_post_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('last_seen_channel_post_id', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('users', 'last_seen_channel_post_id')
    op.drop_column('required_channels', 'last_post_at')
    op.drop_column('required_channels', 'last_post_title')
    op.drop_column('required_channels', 'last_post_link')
    op.drop_column('required_channels', 'last_post_message_id')
    op.drop_column('required_channels', 'is_main')
```

- [ ] **Step 6: Verify migration check passes**

```bash
.venv/bin/python3 -m alembic check 2>&1 | head -5
```

Expected output contains: `ERROR [alembic.util.messaging] Target database is not up to date` (expected — we're not running against a live DB in dev; the key check is that alembic can _parse_ the migration without errors, so run):

```bash
.venv/bin/python3 -m alembic heads
```

Expected: `9025 (head)`

- [ ] **Step 7: py_compile check**

```bash
.venv/bin/python3 -m py_compile app/database/models.py migrations/alembic/versions/9025_channel_soft_nudge.py && echo "OK"
```

Expected: `OK`

- [ ] **Step 8: Commit**

```bash
git add app/database/models.py migrations/alembic/versions/9025_channel_soft_nudge.py tests/test_channel_nudge_model_columns.py
git commit -m "$(cat <<'EOF'
feat(model): добавить is_main и last_post_* в RequiredChannel, last_seen_channel_post_id в User

Миграция 9025: новые колонки для мягкого нуджа подписки на канал.
is_main помечает единственный главный канал (бот мониторит его посты),
last_post_* кешируют свежий пост для карточки нуджа,
last_seen_channel_post_id на пользователе исключает повтор показа одного поста.
EOF
)"
```

---

### Task 2: CHANNEL_SOFT_MODE Config + Short-Circuit Deactivation

**Files:**
- Modify: `app/config.py` (after line 170, near other `CHANNEL_*` settings)
- Modify: `app/middlewares/channel_checker.py` (`_deactivate_subscription_on_unsubscribe`, line 451)
- Modify: `app/handlers/channel_member.py` (`on_user_left_channel`, line 127)
- Modify: `app/services/channel_subscription_service.py` (`should_disable_subscription`, line 87)
- Test: `tests/services/test_channel_soft_mode.py`

**Interfaces:**
- Consumes: `settings.CHANNEL_SOFT_MODE` (bool)
- Produces:
  - `settings.CHANNEL_SOFT_MODE: bool = True` (accessible as `from app.config import settings; settings.CHANNEL_SOFT_MODE`)
  - `ChannelSubscriptionService.should_disable_subscription(channel, is_trial)` returns `False` when `settings.CHANNEL_SOFT_MODE is True`
  - `_deactivate_subscription_on_unsubscribe` returns immediately when soft mode is on
  - `on_user_left_channel` skips deactivation when soft mode is on

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_channel_soft_mode.py
"""Tests for CHANNEL_SOFT_MODE soft-mode flag.

In soft mode:
- should_disable_subscription always returns False (no VPN kill)
- _deactivate_subscription_on_unsubscribe is a no-op
- channel leave event does not deactivate subscriptions
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.channel_subscription_service import ChannelSubscriptionService


def test_should_disable_returns_false_in_soft_mode(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'CHANNEL_SOFT_MODE', True)
    svc = ChannelSubscriptionService()
    # Even with per-channel flags requesting deactivation, soft mode wins
    channel_trial = {'disable_trial_on_leave': True, 'disable_paid_on_leave': True}
    assert ChannelSubscriptionService.should_disable_subscription(channel_trial, is_trial=True) is False
    assert ChannelSubscriptionService.should_disable_subscription(channel_trial, is_trial=False) is False


def test_should_disable_respects_per_channel_when_soft_mode_off(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'CHANNEL_SOFT_MODE', False)
    monkeypatch.setattr(settings, 'CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE', True)
    channel = {'disable_trial_on_leave': True, 'disable_paid_on_leave': False}
    assert ChannelSubscriptionService.should_disable_subscription(channel, is_trial=True) is True
    assert ChannelSubscriptionService.should_disable_subscription(channel, is_trial=False) is False


@pytest.mark.asyncio
async def test_deactivate_is_noop_in_soft_mode(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'CHANNEL_SOFT_MODE', True)

    from app.middlewares.channel_checker import ChannelCheckerMiddleware
    middleware = ChannelCheckerMiddleware()
    bot = AsyncMock()
    channels = [{'channel_id': '-100111', 'is_subscribed': False, 'disable_paid_on_leave': True}]

    # No DB calls should be made in soft mode
    with patch('app.middlewares.channel_checker.AsyncSessionLocal') as mock_session:
        await middleware._deactivate_subscription_on_unsubscribe(12345, bot, channels)
        mock_session.assert_not_called()


@pytest.mark.asyncio
async def test_channel_leave_does_not_deactivate_in_soft_mode(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'CHANNEL_SOFT_MODE', True)

    from app.handlers import channel_member as mod
    import types

    # Fake ChatMemberUpdated event
    user_ns = types.SimpleNamespace(id=99999)
    event = types.SimpleNamespace(
        old_chat_member=types.SimpleNamespace(user=user_ns),
        chat=types.SimpleNamespace(id=-100111),
    )
    bot = AsyncMock()

    with (
        patch.object(mod.channel_subscription_service, 'get_required_channel_ids', AsyncMock(return_value={'-100111'})),
        patch.object(mod.channel_subscription_service, 'on_user_left', AsyncMock()),
        patch('app.handlers.channel_member.AsyncSessionLocal') as mock_session,
    ):
        await mod.on_user_left_channel(event, bot)
        mock_session.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python3 -m pytest tests/services/test_channel_soft_mode.py -v
```

Expected: `4 FAILED` — `AttributeError: 'Settings' object has no attribute 'CHANNEL_SOFT_MODE'`

- [ ] **Step 3: Add `CHANNEL_SOFT_MODE` to config**

In `app/config.py`, after line 171 (`CHANNEL_REQUIRED_FOR_ALL: bool = False`), insert:

```python
    CHANNEL_SOFT_MODE: bool = True  # Мягкий режим: гейт-экран показывается, VPN не отключается
```

- [ ] **Step 4: Patch `should_disable_subscription` in service**

In `app/services/channel_subscription_service.py`, replace `should_disable_subscription` (line 87–101):

```python
    @staticmethod
    def should_disable_subscription(channel: dict, is_trial: bool) -> bool:
        """Check if a channel's settings require subscription deactivation.

        In CHANNEL_SOFT_MODE=True: always returns False (VPN never disabled).
        """
        from app.config import settings

        if settings.CHANNEL_SOFT_MODE:
            return False

        if is_trial:
            if not settings.CHANNEL_DISABLE_TRIAL_ON_UNSUBSCRIBE:
                return False
            return channel.get('disable_trial_on_leave', True)
        return channel.get('disable_paid_on_leave', False)
```

- [ ] **Step 5: Short-circuit `_deactivate_subscription_on_unsubscribe` in middleware**

In `app/middlewares/channel_checker.py`, at the top of `_deactivate_subscription_on_unsubscribe` (line 451), insert after the function signature line:

```python
    async def _deactivate_subscription_on_unsubscribe(
        self,
        telegram_id: int,
        bot: Bot,
        channels: list[dict],
    ) -> None:
        """Deactivate subscription when user unsubscribes from required channels."""
        if settings.CHANNEL_SOFT_MODE:
            return  # Мягкий режим: никогда не отключаем VPN
        async with AsyncSessionLocal() as db:
            # ... (rest of existing code unchanged)
```

- [ ] **Step 6: Short-circuit leave-deactivation in `channel_member.py`**

In `app/handlers/channel_member.py`, in `on_user_left_channel` (line 127), after `await channel_subscription_service.on_user_left(user.id, channel_id)` (~line 136) and before `if not settings.CHANNEL_IS_REQUIRED_SUB: return`, insert:

```python
    if settings.CHANNEL_SOFT_MODE:
        return  # Мягкий режим: не отключаем VPN при выходе из канала
```

The block in `on_user_left_channel` after the `on_user_left` call should now read:

```python
    await channel_subscription_service.on_user_left(user.id, channel_id)

    if settings.CHANNEL_SOFT_MODE:
        return  # Мягкий режим: не отключаем VPN при выходе из канала

    if not settings.CHANNEL_IS_REQUIRED_SUB:
        return
```

- [ ] **Step 7: py_compile check**

```bash
.venv/bin/python3 -m py_compile app/config.py app/services/channel_subscription_service.py app/middlewares/channel_checker.py app/handlers/channel_member.py && echo "OK"
```

Expected: `OK`

- [ ] **Step 8: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/services/test_channel_soft_mode.py -v
```

Expected: `4 passed`

- [ ] **Step 9: Run existing channel tests to check no regressions**

```bash
.venv/bin/python3 -m pytest tests/services/test_channel_check_uncertain.py -v
```

Expected: `7 passed`

- [ ] **Step 10: Commit**

```bash
git add app/config.py app/services/channel_subscription_service.py app/middlewares/channel_checker.py app/handlers/channel_member.py tests/services/test_channel_soft_mode.py
git commit -m "$(cat <<'EOF'
feat(soft-mode): CHANNEL_SOFT_MODE=True — гейт остаётся, VPN никогда не отключается

Добавлен флаг CHANNEL_SOFT_MODE (по умолчанию включён). При включённом режиме:
- should_disable_subscription всегда возвращает False
- _deactivate_subscription_on_unsubscribe в channel_checker — нет-оп
- on_user_left_channel в channel_member — ранний выход без деактивации
Гейт-экран «подпишись» остаётся, но доступ к VPN не отзывается.
EOF
)"
```

---

### Task 3: `channel_post` Handler — Capture Latest Main-Channel Post

**Files:**
- Create: `app/handlers/channel_post.py`
- Modify: `app/database/crud/required_channel.py` (add `get_main_channel()`, `update_channel_post()`, extend `_UPDATABLE_FIELDS`)
- Modify: `app/services/channel_subscription_service.py` (add `is_main` to cache dict; add `get_main_channel()`)
- Modify: `app/handlers/channel_member.py` (`register_handlers` to include channel_post router)
- Test: `tests/services/test_channel_post_handler.py`

**Interfaces:**
- Consumes: `RequiredChannel.is_main`, `RequiredChannel.channel_id`
- Produces:
  - `get_main_channel(db: AsyncSession) -> RequiredChannel | None`
  - `update_channel_post(db, channel_db_id: int, message_id: int, link: str, title: str, at: datetime) -> None`
  - `channel_subscription_service.get_main_channel() -> dict | None` (from cache, keys: `id`, `channel_id`, `is_main`, etc.)
  - `app/handlers/channel_post.py` router registered via `register_handlers` in `channel_member.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/test_channel_post_handler.py
"""Tests for channel_post handler: captures new main-channel posts."""
from __future__ import annotations

import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_channel_post_updates_last_post_for_main_channel():
    """A channel_post event for the main channel updates last_post_* fields."""
    from app.handlers.channel_post import on_channel_post

    main_channel_id = '-100999888'
    main_channel_db_id = 7

    # Fake main channel dict (as returned from cache)
    main_channel_dict = {
        'id': main_channel_db_id,
        'channel_id': main_channel_id,
        'is_main': True,
        'title': 'Главный канал',
        'channel_link': 'https://t.me/mainchan',
    }

    # Fake aiogram Message with text
    message = types.SimpleNamespace(
        message_id=42,
        chat=types.SimpleNamespace(id=int(main_channel_id), username='mainchan'),
        text='Привет! Это свежий пост.',
        caption=None,
        date=datetime(2026, 7, 31, 10, 0, 0, tzinfo=UTC),
    )

    with (
        patch(
            'app.handlers.channel_post.channel_subscription_service.get_main_channel',
            AsyncMock(return_value=main_channel_dict),
        ),
        patch('app.handlers.channel_post.update_channel_post', AsyncMock()) as mock_update,
        patch('app.handlers.channel_post.AsyncSessionLocal') as mock_session,
    ):
        mock_db = AsyncMock()
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=None)

        await on_channel_post(message)

    mock_update.assert_awaited_once()
    call_kwargs = mock_update.call_args
    assert call_kwargs.kwargs['message_id'] == 42
    assert 'mainchan' in call_kwargs.kwargs['link']
    assert '42' in call_kwargs.kwargs['link']
    assert call_kwargs.kwargs['title'] == 'Привет! Это свежий пост.'[:120]


@pytest.mark.asyncio
async def test_channel_post_ignores_non_main_channel():
    """A channel_post event for a non-main channel is silently ignored."""
    from app.handlers.channel_post import on_channel_post

    message = types.SimpleNamespace(
        message_id=99,
        chat=types.SimpleNamespace(id=-100111222, username='otherchan'),
        text='Пост из другого канала',
        caption=None,
        date=datetime(2026, 7, 31, tzinfo=UTC),
    )

    with (
        patch(
            'app.handlers.channel_post.channel_subscription_service.get_main_channel',
            AsyncMock(return_value={'id': 7, 'channel_id': '-100999888', 'is_main': True}),
        ),
        patch('app.handlers.channel_post.update_channel_post', AsyncMock()) as mock_update,
    ):
        await on_channel_post(message)

    mock_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_post_graceful_when_no_main_channel():
    """No main channel configured → no crash, update not called."""
    from app.handlers.channel_post import on_channel_post

    message = types.SimpleNamespace(
        message_id=5,
        chat=types.SimpleNamespace(id=-100000001, username='chan'),
        text='Пост',
        caption=None,
        date=datetime(2026, 7, 31, tzinfo=UTC),
    )

    with (
        patch(
            'app.handlers.channel_post.channel_subscription_service.get_main_channel',
            AsyncMock(return_value=None),
        ),
        patch('app.handlers.channel_post.update_channel_post', AsyncMock()) as mock_update,
    ):
        await on_channel_post(message)

    mock_update.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python3 -m pytest tests/services/test_channel_post_handler.py -v
```

Expected: `3 FAILED` — `ModuleNotFoundError: No module named 'app.handlers.channel_post'`

- [ ] **Step 3: Add CRUD helpers to `required_channel.py`**

In `app/database/crud/required_channel.py`, extend `_UPDATABLE_FIELDS`:

```python
_UPDATABLE_FIELDS = frozenset(
    {
        'channel_id',
        'channel_link',
        'title',
        'is_active',
        'is_main',
        'sort_order',
        'disable_trial_on_leave',
        'disable_paid_on_leave',
        'last_post_message_id',
        'last_post_link',
        'last_post_title',
        'last_post_at',
    }
)
```

Then add these two functions after `toggle_channel`:

```python
async def get_main_channel(db: AsyncSession) -> RequiredChannel | None:
    """Return the single active channel marked is_main=True, or None."""
    result = await db.execute(
        select(RequiredChannel)
        .where(RequiredChannel.is_main.is_(True), RequiredChannel.is_active.is_(True))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def set_main_channel(db: AsyncSession, channel_db_id: int) -> RequiredChannel | None:
    """Mark channel_db_id as is_main=True and clear is_main on all others.

    Enforces the business rule: exactly one main channel.
    Returns the newly-main channel or None if not found.
    """
    # Clear all
    all_channels_result = await db.execute(select(RequiredChannel))
    for ch in all_channels_result.scalars().all():
        ch.is_main = False
        ch.updated_at = datetime.now(UTC)
    # Set the target
    channel = await get_channel_by_id(db, channel_db_id)
    if not channel:
        return None
    channel.is_main = True
    channel.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(channel)
    return channel


async def update_channel_post(
    db: AsyncSession,
    channel_db_id: int,
    message_id: int,
    link: str,
    title: str,
    at: 'datetime',
) -> None:
    """Update the cached latest-post fields on a RequiredChannel."""
    channel = await get_channel_by_id(db, channel_db_id)
    if not channel:
        logger.warning('update_channel_post: channel not found', channel_db_id=channel_db_id)
        return
    channel.last_post_message_id = message_id
    channel.last_post_link = link
    channel.last_post_title = title
    channel.last_post_at = at
    channel.updated_at = datetime.now(UTC)
    await db.commit()
```

Add missing import at the top of the crud file (already has `from datetime import UTC, datetime`):
```python
from datetime import UTC, datetime  # (already present — no change needed)
```

- [ ] **Step 4: Add `get_main_channel()` to `ChannelSubscriptionService`**

In `app/services/channel_subscription_service.py`, add this method after `get_first_channel_id` (~line 241):

```python
    async def get_main_channel(self) -> dict | None:
        """Return the cached main channel dict (is_main=True), or None.

        Cache miss falls back to DB. Returns dict with same keys as get_required_channels()
        plus 'is_main'. Returns None if no main channel is configured.
        """
        channels = await self.get_required_channels()
        for ch in channels:
            if ch.get('is_main'):
                return ch
        return None
```

Also update `get_required_channels` to include `is_main` and `last_post_*` in the cached dict:

```python
    async def get_required_channels(self) -> list[dict]:
        """Get the list of active required channels (cached)."""
        cached = await ChannelSubCache.get_required_channels()
        if cached is not None:
            return cached

        async with AsyncSessionLocal() as db:
            channels = await get_active_channels(db)
            result = [
                {
                    'id': ch.id,
                    'channel_id': ch.channel_id,
                    'channel_link': ch.channel_link,
                    'title': ch.title,
                    'sort_order': ch.sort_order,
                    'is_main': ch.is_main,
                    'disable_trial_on_leave': ch.disable_trial_on_leave,
                    'disable_paid_on_leave': ch.disable_paid_on_leave,
                    'last_post_message_id': ch.last_post_message_id,
                    'last_post_link': ch.last_post_link,
                    'last_post_title': ch.last_post_title,
                    'last_post_at': ch.last_post_at,
                }
                for ch in channels
            ]
            await ChannelSubCache.set_required_channels(result)
            return result
```

- [ ] **Step 5: Create `app/handlers/channel_post.py`**

```python
"""Handler for channel_post updates in the main required channel.

The bot must be a channel admin to receive these events.
If it is not, events simply won't arrive — the handler degrades gracefully.
"""
from __future__ import annotations

from datetime import UTC, datetime

import structlog
from aiogram import Router
from aiogram.filters import IS_CHANNEL
from aiogram.types import Message

from app.database.crud.required_channel import update_channel_post
from app.database.database import AsyncSessionLocal
from app.services.channel_subscription_service import channel_subscription_service


logger = structlog.get_logger(__name__)

router = Router(name='channel_post')

_MAX_TITLE_LEN = 120


def _build_post_link(message: Message) -> str:
    """Build a t.me link to a channel message.

    Uses @username if available, otherwise falls back to numeric channel_id.
    """
    username = getattr(message.chat, 'username', None)
    if username:
        return f'https://t.me/{username}/{message.message_id}'
    # Numeric channel ID is always negative; strip the leading -100 for t.me links
    raw_id = str(message.chat.id).lstrip('-')
    if raw_id.startswith('100'):
        raw_id = raw_id[3:]
    return f'https://t.me/c/{raw_id}/{message.message_id}'


def _extract_title(message: Message) -> str:
    """Extract up to 120 chars of post text/caption, fallback to 'Новый пост'."""
    text = message.text or message.caption or ''
    text = text.strip()
    if not text:
        return 'Новый пост'
    return text[:_MAX_TITLE_LEN]


@router.channel_post()
async def on_channel_post(message: Message) -> None:
    """Receive a new channel post; update last_post_* on the main channel."""
    try:
        main_channel = await channel_subscription_service.get_main_channel()
        if not main_channel:
            return  # No main channel configured — nothing to track

        # Only process posts from the main channel
        if str(message.chat.id) != main_channel['channel_id']:
            return

        link = _build_post_link(message)
        title = _extract_title(message)
        at = message.date if message.date and message.date.tzinfo else datetime.now(UTC)

        async with AsyncSessionLocal() as db:
            await update_channel_post(
                db,
                channel_db_id=main_channel['id'],
                message_id=message.message_id,
                link=link,
                title=title,
                at=at,
            )

        # Invalidate cache so the new post appears in /channel-nudge immediately
        await channel_subscription_service.invalidate_channels_cache()

        logger.info(
            'Updated main channel last post',
            channel_id=main_channel['channel_id'],
            message_id=message.message_id,
        )
    except Exception as e:
        logger.error('Error handling channel_post', error=e)
```

- [ ] **Step 6: Register `channel_post` router**

In `app/handlers/channel_member.py`, update `register_handlers`:

```python
def register_handlers(dp_router: Router) -> None:
    """Register channel member event handlers on the dispatcher/router."""
    dp_router.include_router(router)
    from app.handlers.channel_post import router as channel_post_router
    dp_router.include_router(channel_post_router)
```

- [ ] **Step 7: py_compile check**

```bash
.venv/bin/python3 -m py_compile app/handlers/channel_post.py app/database/crud/required_channel.py app/services/channel_subscription_service.py app/handlers/channel_member.py && echo "OK"
```

Expected: `OK`

- [ ] **Step 8: Run tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/services/test_channel_post_handler.py -v
```

Expected: `3 passed`

- [ ] **Step 9: Run full test suite (no regressions)**

```bash
.venv/bin/python3 -m pytest tests/services/test_channel_check_uncertain.py tests/services/test_channel_soft_mode.py tests/test_channel_nudge_model_columns.py -v
```

Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add app/handlers/channel_post.py app/database/crud/required_channel.py app/services/channel_subscription_service.py app/handlers/channel_member.py tests/services/test_channel_post_handler.py
git commit -m "$(cat <<'EOF'
feat(channel-post): хендлер channel_post для главного канала, обновляет last_post_*

Бот слушает channel_post события главного канала (is_main=True) и кеширует
last_post_message_id / link / title / at для карточки нуджа. Если бот не
является администратором канала — события не приходят, фича деградирует без
ошибок. Добавлены CRUD-функции get_main_channel, set_main_channel,
update_channel_post. Сервис теперь включает is_main и last_post_* в кеш.
EOF
)"
```

---

### Task 4: Bot Post-Nudge Card After Gate Passes

**Files:**
- Identify and Modify: the handler/function called after the gate is cleared (find where `_reactivate_subscription_on_subscribe` or the gate pass-through sends the user to the main menu). Based on codebase patterns the gate pass-through is in `ChannelCheckerMiddleware.__call__` at the `return await handler(event, data)` after `_reactivate_subscription_on_subscribe`. The nudge card is best injected inside the handler itself after the gate clears — specifically in the `sub_channel_check` callback branch that clears the gate (line 170–175 in `channel_checker.py`).
- Modify: `app/middlewares/channel_checker.py`
- Modify: `app/database/crud/user.py` (add `update_user_last_seen_post(db, user_id, post_id)`)
- Test: `tests/services/test_channel_nudge_card.py`

**Interfaces:**
- Consumes: `channel_subscription_service.get_main_channel() -> dict | None`, `User.last_seen_channel_post_id`
- Produces: bot message to user with inline URL button, `update_user_last_seen_post(db, user_id, post_id)` call

- [ ] **Step 1: Find the correct injection point**

Read `app/middlewares/channel_checker.py` lines 154–176 (the `sub_channel_check` callback branch). The pass-through at line 175 is `return await handler(event, data)`. The nudge must fire **just before** that return, inside the same `async with AsyncSessionLocal()` context or just after it.

The nudge fires when:
1. `event` is a `CallbackQuery` with `event.data == 'sub_channel_check'`
2. User is now subscribed to all required channels
3. Main channel has `last_post_message_id` set
4. `user.last_seen_channel_post_id != main_channel['last_post_message_id']`

- [ ] **Step 2: Write the failing tests**

```python
# tests/services/test_channel_nudge_card.py
"""Tests for the bot nudge card shown after the channel gate clears."""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


@pytest.mark.asyncio
async def test_nudge_card_sent_when_new_post_unseen(monkeypatch):
    """After gate passes and user hasn't seen latest post, the nudge card is sent."""
    from app.config import settings
    monkeypatch.setattr(settings, 'CHANNEL_SOFT_MODE', True)

    from app.middlewares.channel_checker import _send_channel_post_nudge

    bot = AsyncMock()
    user_id = 55555
    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'title': 'Тест-канал',
        'last_post_message_id': 42,
        'last_post_link': 'https://t.me/testchan/42',
        'last_post_title': 'Привет!',
    }

    db_user = types.SimpleNamespace(id=10, last_seen_channel_post_id=None)

    with patch('app.middlewares.channel_checker.update_user_last_seen_post', AsyncMock()) as mock_update:
        await _send_channel_post_nudge(bot, user_id, db_user, main_channel, db=AsyncMock())

    # Bot should have sent a message with an inline URL button
    bot.send_message.assert_awaited_once()
    call_args = bot.send_message.call_args
    assert call_args.args[0] == user_id
    assert '🆕' in call_args.args[1]
    assert 'Привет!' in call_args.args[1]
    # InlineKeyboardMarkup should contain URL button
    reply_markup = call_args.kwargs.get('reply_markup') or call_args.args[2] if len(call_args.args) > 2 else None
    assert reply_markup is not None

    # last_seen should be updated
    mock_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_nudge_card_not_sent_when_already_seen(monkeypatch):
    """If user already saw this post, don't send the card again."""
    from app.middlewares.channel_checker import _send_channel_post_nudge

    bot = AsyncMock()
    db_user = types.SimpleNamespace(id=10, last_seen_channel_post_id=42)
    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'last_post_message_id': 42,
        'last_post_link': 'https://t.me/testchan/42',
        'last_post_title': 'Привет!',
    }

    await _send_channel_post_nudge(bot, 55555, db_user, main_channel, db=AsyncMock())
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_nudge_card_not_sent_when_no_post():
    """If main channel has no post yet, nudge is skipped."""
    from app.middlewares.channel_checker import _send_channel_post_nudge

    bot = AsyncMock()
    db_user = types.SimpleNamespace(id=10, last_seen_channel_post_id=None)
    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'last_post_message_id': None,
        'last_post_link': None,
        'last_post_title': None,
    }

    await _send_channel_post_nudge(bot, 55555, db_user, main_channel, db=AsyncMock())
    bot.send_message.assert_not_awaited()
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
.venv/bin/python3 -m pytest tests/services/test_channel_nudge_card.py -v
```

Expected: `3 FAILED` — `ImportError: cannot import name '_send_channel_post_nudge'`

- [ ] **Step 4: Add `update_user_last_seen_post` to user CRUD**

In `app/database/crud/user.py`, find and add after other user update functions:

```python
async def update_user_last_seen_post(db: AsyncSession, user_id: int, post_id: int) -> None:
    """Set user.last_seen_channel_post_id to mark a channel post as seen."""
    from app.database.models import User as UserModel
    from sqlalchemy import update as sa_update

    await db.execute(
        sa_update(UserModel)
        .where(UserModel.id == user_id)
        .values(last_seen_channel_post_id=post_id)
    )
    await db.commit()
```

- [ ] **Step 5: Add `_send_channel_post_nudge` helper to `channel_checker.py`**

Add this function near the bottom of `channel_checker.py`, above `_normalize_channel_link`:

```python
async def _send_channel_post_nudge(
    bot: Bot,
    telegram_id: int,
    db_user: Any,
    main_channel: dict,
    db: Any,
) -> None:
    """Send the '🆕 Fresh in channel' nudge card if user hasn't seen the latest post.

    Updates user.last_seen_channel_post_id after sending.
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from app.database.crud.user import update_user_last_seen_post

    post_id = main_channel.get('last_post_message_id')
    post_link = main_channel.get('last_post_link')
    post_title = main_channel.get('last_post_title') or 'Новый пост'

    if not post_id or not post_link:
        return  # No post tracked yet

    if getattr(db_user, 'last_seen_channel_post_id', None) == post_id:
        return  # Already seen

    try:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text='Открыть пост', url=post_link)]]
        )
        await bot.send_message(
            telegram_id,
            f'🆕 Свежее в канале: {post_title}',
            reply_markup=keyboard,
        )
        await update_user_last_seen_post(db, db_user.id, post_id)
    except Exception as e:
        logger.warning('Failed to send channel post nudge', telegram_id=telegram_id, error=e)
```

Also add the import at the top of `channel_checker.py` if not already present:
```python
from typing import Any
```

- [ ] **Step 6: Wire nudge into the gate pass-through**

In `app/middlewares/channel_checker.py`, in the `sub_channel_check` branch, replace the pass-through block (around line 170–176) with:

```python
            if not unsubscribed_fresh:
                # Now subscribed to all channels
                if self._any_channel_has_disable_flag(all_channels_fresh):
                    await self._reactivate_subscription_on_subscribe(telegram_id, bot)

                # Try to send post nudge card (soft mode feature)
                try:
                    main_channel = await channel_subscription_service.get_main_channel()
                    if main_channel:
                        async with AsyncSessionLocal() as db_nudge:
                            from app.database.crud.user import get_user_by_telegram_id as _get_user
                            db_user = await _get_user(db_nudge, telegram_id)
                            if db_user:
                                await _send_channel_post_nudge(
                                    bot, telegram_id, db_user, main_channel, db_nudge
                                )
                except Exception as _nudge_err:
                    logger.warning('channel post nudge failed', error=_nudge_err)

                return await handler(event, data)
```

- [ ] **Step 7: py_compile check**

```bash
.venv/bin/python3 -m py_compile app/middlewares/channel_checker.py app/database/crud/user.py && echo "OK"
```

Expected: `OK`

- [ ] **Step 8: Run tests**

```bash
.venv/bin/python3 -m pytest tests/services/test_channel_nudge_card.py -v
```

Expected: `3 passed`

- [ ] **Step 9: Full channel test pass**

```bash
.venv/bin/python3 -m pytest tests/services/test_channel_soft_mode.py tests/services/test_channel_nudge_card.py tests/services/test_channel_post_handler.py tests/services/test_channel_check_uncertain.py -v
```

Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add app/middlewares/channel_checker.py app/database/crud/user.py tests/services/test_channel_nudge_card.py
git commit -m "$(cat <<'EOF'
feat(nudge-card): карточка свежего поста в боте после прохождения гейта подписки

После того как пользователь нажал «Проверить» и прошёл гейт, бот отправляет
карточку «🆕 Свежее в канале» с кнопкой-ссылкой, если last_post_message_id
отличается от last_seen_channel_post_id пользователя. После отправки
сохраняем id просмотренного поста, чтобы не показывать повторно.
EOF
)"
```

---

### Task 5: Cabinet API Nudge Endpoints

**Files:**
- Create: `app/cabinet/routes/channel_nudge.py`
- Modify: `app/cabinet/routes/__init__.py`
- Modify: `app/cabinet/schemas/channel.py` (add `ChannelNudgeResponse`, `is_main` to `ChannelResponse`)
- Test: `tests/cabinet/test_channel_nudge_routes.py`

**Interfaces:**
- Consumes:
  - `get_current_cabinet_user` dep from `app/cabinet/dependencies.py`
  - `get_cabinet_db` dep from `app/cabinet/dependencies.py`
  - `channel_subscription_service.check_user_subscriptions(telegram_id)`
  - `channel_subscription_service.get_main_channel() -> dict | None`
  - `update_user_last_seen_post(db, user_id, post_id)` from `app/database/crud/user.py`
- Produces:
  - `GET /cabinet/channel-nudge` → `ChannelNudgeResponse`
  - `POST /cabinet/channel-nudge/seen` → `{"ok": true}`
  - `ChannelNudgeResponse` schema with fields `needs_subscribe`, `channel`, `latest_post`, `show_post`

- [ ] **Step 1: Write the failing tests**

```python
# tests/cabinet/test_channel_nudge_routes.py
"""Tests for /cabinet/channel-nudge endpoints."""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest


def test_channel_nudge_routes_registered():
    from app.cabinet.routes import router
    paths = {r.path: r.methods for r in router.routes if hasattr(r, 'methods')}
    assert '/cabinet/channel-nudge' in paths
    assert 'GET' in paths['/cabinet/channel-nudge']
    assert '/cabinet/channel-nudge/seen' in paths
    assert 'POST' in paths['/cabinet/channel-nudge/seen']


@pytest.mark.asyncio
async def test_nudge_subscribed_user_no_post():
    """Telegram user subscribed to main channel, no latest post → needs_subscribe=False, show_post=False."""
    from app.cabinet.routes import channel_nudge as mod

    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'title': 'Тест',
        'channel_link': 'https://t.me/testchan',
        'last_post_message_id': None,
        'last_post_link': None,
        'last_post_title': None,
    }
    db_user = types.SimpleNamespace(
        id=10,
        telegram_id=12345,
        last_seen_channel_post_id=None,
    )
    db = AsyncMock()

    with (
        patch.object(mod.channel_subscription_service, 'get_main_channel', AsyncMock(return_value=main_channel)),
        patch.object(
            mod.channel_subscription_service,
            'check_user_subscriptions',
            AsyncMock(return_value={'-100111': True}),
        ),
    ):
        resp = await mod.get_channel_nudge(current_user=db_user, db=db)

    assert resp.needs_subscribe is False
    assert resp.show_post is False
    assert resp.latest_post is None


@pytest.mark.asyncio
async def test_nudge_email_only_user_always_needs_subscribe():
    """Email-only user (no telegram_id) → needs_subscribe=True, no API call."""
    from app.cabinet.routes import channel_nudge as mod

    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'title': 'Тест',
        'channel_link': 'https://t.me/testchan',
        'last_post_message_id': 77,
        'last_post_link': 'https://t.me/testchan/77',
        'last_post_title': 'Hello',
    }
    db_user = types.SimpleNamespace(
        id=20,
        telegram_id=None,  # email-only
        last_seen_channel_post_id=None,
    )
    db = AsyncMock()

    with patch.object(mod.channel_subscription_service, 'get_main_channel', AsyncMock(return_value=main_channel)):
        resp = await mod.get_channel_nudge(current_user=db_user, db=db)

    assert resp.needs_subscribe is True
    assert resp.show_post is True
    assert resp.latest_post is not None
    assert resp.latest_post['id'] == 77


@pytest.mark.asyncio
async def test_nudge_show_post_false_when_already_seen():
    """User has seen the latest post → show_post=False even if needs_subscribe."""
    from app.cabinet.routes import channel_nudge as mod

    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'title': 'Тест',
        'channel_link': 'https://t.me/testchan',
        'last_post_message_id': 77,
        'last_post_link': 'https://t.me/testchan/77',
        'last_post_title': 'Hello',
    }
    db_user = types.SimpleNamespace(
        id=20,
        telegram_id=None,
        last_seen_channel_post_id=77,  # already seen
    )
    db = AsyncMock()

    with patch.object(mod.channel_subscription_service, 'get_main_channel', AsyncMock(return_value=main_channel)):
        resp = await mod.get_channel_nudge(current_user=db_user, db=db)

    assert resp.show_post is False


@pytest.mark.asyncio
async def test_nudge_seen_endpoint_updates_user():
    """POST /seen updates last_seen_channel_post_id."""
    from app.cabinet.routes import channel_nudge as mod

    db_user = types.SimpleNamespace(id=30, telegram_id=99999, last_seen_channel_post_id=None)
    db = AsyncMock()

    with patch('app.cabinet.routes.channel_nudge.update_user_last_seen_post', AsyncMock()) as mock_update:
        resp = await mod.mark_channel_nudge_seen(
            body=mod.MarkSeenRequest(post_id=42),
            current_user=db_user,
            db=db,
        )

    mock_update.assert_awaited_once_with(db, db_user.id, 42)
    assert resp == {'ok': True}


@pytest.mark.asyncio
async def test_nudge_no_500_on_telegram_error():
    """If Telegram membership check raises, endpoint returns needs_subscribe=True, no 500."""
    from app.cabinet.routes import channel_nudge as mod

    main_channel = {
        'id': 1,
        'channel_id': '-100111',
        'is_main': True,
        'title': 'Тест',
        'channel_link': 'https://t.me/testchan',
        'last_post_message_id': None,
        'last_post_link': None,
        'last_post_title': None,
    }
    db_user = types.SimpleNamespace(id=10, telegram_id=12345, last_seen_channel_post_id=None)
    db = AsyncMock()

    with (
        patch.object(mod.channel_subscription_service, 'get_main_channel', AsyncMock(return_value=main_channel)),
        patch.object(
            mod.channel_subscription_service,
            'check_user_subscriptions',
            AsyncMock(side_effect=Exception('Telegram API down')),
        ),
    ):
        resp = await mod.get_channel_nudge(current_user=db_user, db=db)

    assert resp.needs_subscribe is True  # degraded gracefully
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/python3 -m pytest tests/cabinet/test_channel_nudge_routes.py -v
```

Expected: `6 FAILED` — route/module not found

- [ ] **Step 3: Add `ChannelNudgeResponse` schema to `app/cabinet/schemas/channel.py`**

Add at the bottom of `app/cabinet/schemas/channel.py`:

```python
class ChannelPostInfo(BaseModel):
    id: int
    link: str
    title: str | None


class ChannelBasicInfo(BaseModel):
    title: str | None
    link: str | None


class ChannelNudgeResponse(BaseModel):
    needs_subscribe: bool
    channel: ChannelBasicInfo | None
    latest_post: ChannelPostInfo | None
    show_post: bool
```

Also add `is_main` and `last_post_*` to `ChannelResponse`:

```python
class ChannelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: str
    channel_link: str | None
    title: str | None
    is_active: bool
    is_main: bool
    sort_order: int
    disable_trial_on_leave: bool
    disable_paid_on_leave: bool
    last_post_message_id: int | None = None
    last_post_link: str | None = None
    last_post_title: str | None = None
    last_post_at: datetime | None = None
```

Add `from datetime import datetime` to the top of `channel.py` if not present.

- [ ] **Step 4: Create `app/cabinet/routes/channel_nudge.py`**

```python
"""Cabinet API: channel subscription nudge for the main channel.

GET  /cabinet/channel-nudge  → ChannelNudgeResponse
POST /cabinet/channel-nudge/seen → {"ok": true}
"""
from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.cabinet.schemas.channel import ChannelBasicInfo, ChannelNudgeResponse, ChannelPostInfo
from app.database.crud.user import update_user_last_seen_post
from app.database.models import User
from app.services.channel_subscription_service import channel_subscription_service


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/cabinet/channel-nudge', tags=['Cabinet Channel Nudge'])


class MarkSeenRequest(BaseModel):
    post_id: int


@router.get('', response_model=ChannelNudgeResponse)
async def get_channel_nudge(
    current_user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> ChannelNudgeResponse:
    """Return whether the user needs to subscribe and/or see a fresh post.

    Never raises 500: Telegram/panel errors degrade to needs_subscribe=True.
    """
    main_channel = await channel_subscription_service.get_main_channel()

    if not main_channel:
        return ChannelNudgeResponse(
            needs_subscribe=False,
            channel=None,
            latest_post=None,
            show_post=False,
        )

    # Build latest_post block
    post_id = main_channel.get('last_post_message_id')
    latest_post: ChannelPostInfo | None = None
    if post_id and main_channel.get('last_post_link'):
        latest_post = ChannelPostInfo(
            id=post_id,
            link=main_channel['last_post_link'],
            title=main_channel.get('last_post_title'),
        )

    channel_info = ChannelBasicInfo(
        title=main_channel.get('title'),
        link=main_channel.get('channel_link'),
    )

    # Determine needs_subscribe
    needs_subscribe: bool = True
    if current_user.telegram_id:
        try:
            subs = await channel_subscription_service.check_user_subscriptions(current_user.telegram_id)
            needs_subscribe = not subs.get(main_channel['channel_id'], False)
        except Exception as e:
            # Degrade gracefully: show nudge, no 500
            logger.warning('channel_nudge: membership check failed', error=e)
            needs_subscribe = True

    # show_post: true if there is a post the user hasn't seen yet
    show_post = (
        latest_post is not None
        and latest_post.id != current_user.last_seen_channel_post_id
    )

    return ChannelNudgeResponse(
        needs_subscribe=needs_subscribe,
        channel=channel_info,
        latest_post=latest_post,
        show_post=show_post,
    )


@router.post('/seen', response_model=dict)
async def mark_channel_nudge_seen(
    body: MarkSeenRequest,
    current_user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    """Mark a channel post as seen for the current user."""
    await update_user_last_seen_post(db, current_user.id, body.post_id)
    return {'ok': True}
```

- [ ] **Step 5: Register router in `app/cabinet/routes/__init__.py`**

After the other import lines (e.g., after the `admin_google_migration_router` import), add:

```python
from .channel_nudge import router as channel_nudge_router
```

And after `router.include_router(websocket_router)` near the bottom, add:

```python
router.include_router(channel_nudge_router)
```

- [ ] **Step 6: py_compile check**

```bash
.venv/bin/python3 -m py_compile app/cabinet/routes/channel_nudge.py app/cabinet/schemas/channel.py app/cabinet/routes/__init__.py && echo "OK"
```

Expected: `OK`

- [ ] **Step 7: Run tests**

```bash
.venv/bin/python3 -m pytest tests/cabinet/test_channel_nudge_routes.py -v
```

Expected: `6 passed`

- [ ] **Step 8: Regression check**

```bash
.venv/bin/python3 -m pytest tests/cabinet/ -v --tb=short 2>&1 | tail -20
```

Expected: no new failures.

- [ ] **Step 9: Commit**

```bash
git add app/cabinet/routes/channel_nudge.py app/cabinet/routes/__init__.py app/cabinet/schemas/channel.py tests/cabinet/test_channel_nudge_routes.py
git commit -m "$(cat <<'EOF'
feat(cabinet-api): эндпоинты /cabinet/channel-nudge для нуджа подписки/поста

GET возвращает needs_subscribe, channel, latest_post, show_post.
POST /seen записывает last_seen_channel_post_id. Ошибки Telegram деградируют
до needs_subscribe=true без 500. Email-only пользователи всегда needs_subscribe=true.
EOF
)"
```

---

### Task 6: Frontend API + Dismissible Nudge Popup

**Files:**
- Create: `src/api/channelNudge.ts`
- Create: `src/components/ChannelNudgeModal.tsx`
- Modify: `src/components/layout/AppShell/AppShell.tsx`
- Modify: `src/locales/ru.json` (add `channelNudge` namespace)
- Modify: `src/locales/en.json` (add `channelNudge` namespace)
- Create: `src/components/ChannelNudgeModal.test.tsx`

**Interfaces:**
- Produces:
  - `getChannelNudge(): Promise<ChannelNudgeData>` from `src/api/channelNudge.ts`
  - `markChannelPostSeen(postId: number): Promise<void>` from `src/api/channelNudge.ts`
  - `<ChannelNudgeModal />` — dismissible popup, no props needed (self-contained query)

- [ ] **Step 1: Add locale keys**

In `src/locales/ru.json`, in the top-level object, add:

```json
"channelNudge": {
  "newPost": "🆕 Свежее в канале",
  "openPost": "Открыть пост",
  "subscribeTitle": "Подпишитесь на наш канал",
  "subscribeBody": "Обязательно подпишитесь на наш канал, чтобы быть в курсе новостей!",
  "subscribeButton": "Подписаться",
  "close": "Закрыть"
}
```

In `src/locales/en.json`, add:

```json
"channelNudge": {
  "newPost": "🆕 New in channel",
  "openPost": "Open post",
  "subscribeTitle": "Subscribe to our channel",
  "subscribeBody": "Subscribe to our channel to stay up to date with the latest news!",
  "subscribeButton": "Subscribe",
  "close": "Close"
}
```

- [ ] **Step 2: Write failing test**

```tsx
// src/components/ChannelNudgeModal.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import React from 'react';

// Mock the API module before importing the component
vi.mock('../api/channelNudge', () => ({
  getChannelNudge: vi.fn(),
  markChannelPostSeen: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

import ChannelNudgeModal from './ChannelNudgeModal';
import { getChannelNudge, markChannelPostSeen } from '../api/channelNudge';

const mockGetChannelNudge = getChannelNudge as ReturnType<typeof vi.fn>;
const mockMarkSeen = markChannelPostSeen as ReturnType<typeof vi.fn>;

describe('ChannelNudgeModal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows post nudge when show_post=true', async () => {
    mockGetChannelNudge.mockResolvedValue({
      needs_subscribe: false,
      channel: { title: 'Тест', link: 'https://t.me/testchan' },
      latest_post: { id: 42, link: 'https://t.me/testchan/42', title: 'Hello world' },
      show_post: true,
    });

    render(<ChannelNudgeModal />);

    await waitFor(() => {
      expect(screen.getByText(/channelNudge.newPost/i)).toBeInTheDocument();
    });
    expect(screen.getByText('Hello world')).toBeInTheDocument();
  });

  it('shows subscribe block only when needs_subscribe=true', async () => {
    mockGetChannelNudge.mockResolvedValue({
      needs_subscribe: true,
      channel: { title: 'Тест', link: 'https://t.me/testchan' },
      latest_post: null,
      show_post: false,
    });

    render(<ChannelNudgeModal />);

    await waitFor(() => {
      expect(screen.getByText(/channelNudge.subscribeTitle/i)).toBeInTheDocument();
    });
  });

  it('does not render when show_post=false and needs_subscribe=false', async () => {
    mockGetChannelNudge.mockResolvedValue({
      needs_subscribe: false,
      channel: null,
      latest_post: null,
      show_post: false,
    });

    const { container } = render(<ChannelNudgeModal />);

    await waitFor(() => {
      // wait for the query to resolve
      expect(mockGetChannelNudge).toHaveBeenCalled();
    });

    // Modal content should not be in DOM
    expect(container.querySelector('[data-testid="channel-nudge-modal"]')).toBeNull();
  });

  it('calls markChannelPostSeen on close when post is present', async () => {
    mockGetChannelNudge.mockResolvedValue({
      needs_subscribe: false,
      channel: null,
      latest_post: { id: 77, link: 'https://t.me/testchan/77', title: 'A post' },
      show_post: true,
    });

    render(<ChannelNudgeModal />);

    const closeBtn = await screen.findByRole('button', { name: /channelNudge.close/i });
    fireEvent.click(closeBtn);

    await waitFor(() => {
      expect(mockMarkSeen).toHaveBeenCalledWith(77);
    });
  });
});
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx vitest run src/components/ChannelNudgeModal.test.tsx 2>&1 | tail -20
```

Expected: `FAIL — Cannot find module '../api/channelNudge'`

- [ ] **Step 4: Create `src/api/channelNudge.ts`**

```typescript
import apiClient from './client';

export interface ChannelPostInfo {
  id: number;
  link: string;
  title: string | null;
}

export interface ChannelBasicInfo {
  title: string | null;
  link: string | null;
}

export interface ChannelNudgeData {
  needs_subscribe: boolean;
  channel: ChannelBasicInfo | null;
  latest_post: ChannelPostInfo | null;
  show_post: boolean;
}

export async function getChannelNudge(): Promise<ChannelNudgeData> {
  const { data } = await apiClient.get<ChannelNudgeData>('/cabinet/channel-nudge');
  return data;
}

export async function markChannelPostSeen(postId: number): Promise<void> {
  await apiClient.post('/cabinet/channel-nudge/seen', { post_id: postId });
}
```

- [ ] **Step 5: Create `src/components/ChannelNudgeModal.tsx`**

```tsx
/**
 * Non-blocking dismissible popup shown on cabinet load when:
 * - show_post=true: user hasn't seen the latest main-channel post
 * - needs_subscribe=true: user is not confirmed as subscribed to main channel
 *
 * On show/close, calls markChannelPostSeen so the popup doesn't reappear
 * until the next post.
 */
import { useCallback, useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { getChannelNudge, markChannelPostSeen, type ChannelNudgeData } from '../api/channelNudge';

export default function ChannelNudgeModal() {
  const { t } = useTranslation();
  const [dismissed, setDismissed] = useState(false);

  const { data } = useQuery<ChannelNudgeData>({
    queryKey: ['channel-nudge'],
    queryFn: getChannelNudge,
    staleTime: 60_000,
    retry: false,
  });

  const shouldShow =
    !dismissed && data != null && (data.show_post || data.needs_subscribe);

  // Mark seen as soon as the popup becomes visible
  useEffect(() => {
    if (shouldShow && data?.latest_post?.id != null) {
      markChannelPostSeen(data.latest_post.id).catch(() => {});
    }
  }, [shouldShow, data?.latest_post?.id]);

  const handleClose = useCallback(() => {
    setDismissed(true);
    if (data?.latest_post?.id != null) {
      markChannelPostSeen(data.latest_post.id).catch(() => {});
    }
  }, [data?.latest_post?.id]);

  if (!shouldShow) return null;

  const modal = (
    <div
      className="fixed inset-0 z-[200] flex items-end justify-center sm:items-center"
      data-testid="channel-nudge-modal"
    >
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/40"
        onClick={handleClose}
        aria-hidden="true"
      />

      {/* Panel */}
      <div className="relative z-10 mx-4 mb-6 w-full max-w-sm rounded-2xl border border-dark-700 bg-dark-900 p-5 shadow-xl sm:mb-0">
        {/* Post block (shown to all when show_post=true) */}
        {data.show_post && data.latest_post && (
          <div className="mb-4">
            <p className="text-sm font-semibold text-dark-100">
              {t('channelNudge.newPost')}
            </p>
            {data.latest_post.title && (
              <p className="mt-1 text-sm text-dark-300 line-clamp-2">
                {data.latest_post.title}
              </p>
            )}
            <a
              href={data.latest_post.link}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-3 inline-block rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white hover:bg-primary-500"
              onClick={handleClose}
            >
              {t('channelNudge.openPost')}
            </a>
          </div>
        )}

        {/* Subscribe block (shown only when needs_subscribe=true) */}
        {data.needs_subscribe && data.channel && (
          <div className={data.show_post ? 'border-t border-dark-700 pt-4' : ''}>
            <p className="text-sm font-semibold text-dark-100">
              {t('channelNudge.subscribeTitle')}
            </p>
            <p className="mt-1 text-sm text-dark-400">
              {t('channelNudge.subscribeBody')}
            </p>
            {data.channel.link && (
              <a
                href={data.channel.link}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-3 inline-block rounded-lg border border-dark-600 px-4 py-2 text-sm font-medium text-dark-200 hover:bg-dark-800"
                onClick={handleClose}
              >
                {t('channelNudge.subscribeButton')}
              </a>
            )}
          </div>
        )}

        {/* Close button */}
        <button
          onClick={handleClose}
          aria-label={t('channelNudge.close')}
          className="absolute right-3 top-3 rounded-lg p-1.5 text-dark-400 hover:bg-dark-700 hover:text-dark-200"
        >
          <svg className="h-4 w-4" viewBox="0 0 16 16" fill="currentColor">
            <path d="M12.854 3.146a.5.5 0 0 1 0 .708L8.707 8l4.147 4.146a.5.5 0 0 1-.708.708L8 8.707l-4.146 4.147a.5.5 0 0 1-.708-.708L7.293 8 3.146 3.854a.5.5 0 0 1 .708-.708L8 7.293l4.146-4.147a.5.5 0 0 1 .708 0z" />
          </svg>
        </button>
      </div>
    </div>
  );

  return createPortal(modal, document.body);
}
```

- [ ] **Step 6: Mount `<ChannelNudgeModal />` in `AppShell.tsx`**

In `src/components/layout/AppShell/AppShell.tsx`, add import at the top (near the other global component imports):

```tsx
import ChannelNudgeModal from '@/components/ChannelNudgeModal';
```

In the JSX return, add `<ChannelNudgeModal />` alongside the other global modals (near line 189 where `<SuccessNotificationModal />` lives):

```tsx
      <WebSocketNotifications />
      <CampaignBonusNotifier />
      <SuccessNotificationModal />
      <ChannelNudgeModal />
      <PromptDialogHost />
```

- [ ] **Step 7: Run vitest**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx vitest run src/components/ChannelNudgeModal.test.tsx 2>&1 | tail -30
```

Expected: `4 passed`

- [ ] **Step 8: TypeScript + build check**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx tsc --noEmit 2>&1 | head -20
npm run build 2>&1 | tail -10
```

Expected: no errors.

- [ ] **Step 9: Full vitest**

```bash
npx vitest run 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 10: Commit**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
git add src/api/channelNudge.ts src/components/ChannelNudgeModal.tsx src/components/ChannelNudgeModal.test.tsx src/components/layout/AppShell/AppShell.tsx src/locales/ru.json src/locales/en.json
git commit -m "$(cat <<'EOF'
feat(cabinet-ui): попап нуджа подписки и свежего поста канала

Компонент ChannelNudgeModal монтируется в AppShell рядом с другими глобальными
модалями. Запрашивает /cabinet/channel-nudge при загрузке и показывает карточку
поста (всем) и блок подписки (только неподписанным). При показе/закрытии
вызывает markChannelPostSeen чтобы не показывать повторно до нового поста.
Добавлены ключи channelNudge в ru.json и en.json.
EOF
)"
```

---

### Task 7: Admin `is_main` Toggle + Single-Main Enforcement

**Files:**
- Modify: `app/cabinet/routes/admin_channels.py` (add `POST /{id}/set-main`)
- Modify: `app/cabinet/schemas/channel.py` (already done in T5 — `ChannelResponse.is_main`)
- Modify: `src/api/adminChannels.ts` (add `is_main` to types; add `setMain()`)
- Modify: `src/pages/AdminChannelSubscriptions.tsx` (add `is_main` toggle in `ChannelCard`; show latest post read-only)
- Test backend: `tests/cabinet/test_admin_channel_is_main.py`
- Test frontend: `src/pages/AdminChannelSubscriptions.test.tsx` (new)

**Interfaces:**
- Consumes: `set_main_channel(db, channel_db_id)` from `app/database/crud/required_channel.py` (T3)
- Produces:
  - `POST /cabinet/admin/channel-subscriptions/{channel_db_id}/set-main` → `ChannelResponse`
  - `adminChannelsApi.setMain(id: number): Promise<RequiredChannel>`

- [ ] **Step 1: Write failing backend tests**

```python
# tests/cabinet/test_admin_channel_is_main.py
"""Tests for is_main enforcement: exactly one main channel."""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest


def test_set_main_route_registered():
    from app.cabinet.routes import router
    paths = {r.path: r.methods for r in router.routes if hasattr(r, 'methods')}
    assert '/cabinet/admin/channel-subscriptions/{channel_db_id}/set-main' in paths
    assert 'POST' in paths['/cabinet/admin/channel-subscriptions/{channel_db_id}/set-main']


@pytest.mark.asyncio
async def test_set_main_clears_others_and_returns_channel():
    from app.cabinet.routes import admin_channels as mod

    result_channel = types.SimpleNamespace(
        id=2,
        channel_id='-100999',
        channel_link='https://t.me/main',
        title='Main',
        is_active=True,
        is_main=True,
        sort_order=0,
        disable_trial_on_leave=True,
        disable_paid_on_leave=False,
        last_post_message_id=None,
        last_post_link=None,
        last_post_title=None,
        last_post_at=None,
    )
    db = AsyncMock()

    with (
        patch('app.cabinet.routes.admin_channels.set_main_channel', AsyncMock(return_value=result_channel)) as mock_set,
        patch.object(mod.channel_subscription_service, 'invalidate_channels_cache', AsyncMock()),
    ):
        resp = await mod.set_main_channel_endpoint(
            channel_db_id=2,
            db=db,
            _admin=types.SimpleNamespace(id=1),
        )

    mock_set.assert_awaited_once_with(db, 2)
    assert resp.is_main is True
    assert resp.id == 2


@pytest.mark.asyncio
async def test_set_main_404_when_channel_not_found():
    from fastapi import HTTPException
    from app.cabinet.routes import admin_channels as mod

    db = AsyncMock()

    with (
        patch('app.cabinet.routes.admin_channels.set_main_channel', AsyncMock(return_value=None)),
        patch.object(mod.channel_subscription_service, 'invalidate_channels_cache', AsyncMock()),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await mod.set_main_channel_endpoint(
                channel_db_id=999,
                db=db,
                _admin=types.SimpleNamespace(id=1),
            )
    assert exc_info.value.status_code == 404
```

- [ ] **Step 2: Run backend tests to verify they fail**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
.venv/bin/python3 -m pytest tests/cabinet/test_admin_channel_is_main.py -v
```

Expected: `3 FAILED`

- [ ] **Step 3: Add `set-main` endpoint to `admin_channels.py`**

In `app/cabinet/routes/admin_channels.py`:

Add import at the top:
```python
from app.database.crud.required_channel import (
    add_channel,
    delete_channel,
    get_all_channels,
    set_main_channel,
    toggle_channel,
    update_channel,
)
```

Add endpoint after `toggle_channel_endpoint`:

```python
@router.post('/{channel_db_id}/set-main', response_model=ChannelResponse)
async def set_main_channel_endpoint(
    channel_db_id: int,
    db: AsyncSession = Depends(get_cabinet_db),
    _admin: User = Depends(require_permission('channels:edit')),
) -> ChannelResponse:
    """Mark channel as the main channel. Clears is_main on all others."""
    ch = await set_main_channel(db, channel_db_id)
    if not ch:
        raise HTTPException(status_code=404, detail='Channel not found')
    await channel_subscription_service.invalidate_channels_cache()
    return ChannelResponse.model_validate(ch)
```

- [ ] **Step 4: py_compile check**

```bash
.venv/bin/python3 -m py_compile app/cabinet/routes/admin_channels.py && echo "OK"
```

Expected: `OK`

- [ ] **Step 5: Run backend tests**

```bash
.venv/bin/python3 -m pytest tests/cabinet/test_admin_channel_is_main.py -v
```

Expected: `3 passed`

- [ ] **Step 6: Update `src/api/adminChannels.ts`**

Add `is_main` and `last_post_*` to `RequiredChannel` interface and add `setMain` API call:

```typescript
export interface RequiredChannel {
  id: number;
  channel_id: string;
  channel_link: string | null;
  title: string | null;
  is_active: boolean;
  is_main: boolean;
  sort_order: number;
  disable_trial_on_leave: boolean;
  disable_paid_on_leave: boolean;
  last_post_message_id: number | null;
  last_post_link: string | null;
  last_post_title: string | null;
  last_post_at: string | null;
}
```

Add to `adminChannelsApi` object (after `cancelReport`):

```typescript
  setMain: async (id: number): Promise<RequiredChannel> => {
    const { data } = await apiClient.post<RequiredChannel>(
      `/cabinet/admin/channel-subscriptions/${id}/set-main`,
    );
    return data;
  },
```

- [ ] **Step 7: Add `is_main` toggle and latest post display to `AdminChannelSubscriptions.tsx`**

In `ChannelCard`, add `onSetMain` to the props:

```typescript
function ChannelCard({
  channel,
  onToggle,
  onDelete,
  onEdit,
  onUpdate,
  onSetMain,
}: {
  channel: RequiredChannel;
  onToggle: (id: number) => void;
  onDelete: (id: number) => void;
  onEdit: (channel: RequiredChannel) => void;
  onUpdate: (id: number, data: UpdateChannelRequest) => void;
  onSetMain: (id: number) => void;
}) {
```

Inside `ChannelCard` JSX, add the `is_main` toggle after the existing per-channel toggles (before the closing `</div>` of the card content area):

```tsx
          {/* is_main toggle */}
          <div className="mt-3 flex items-center justify-between gap-3 rounded-lg bg-dark-700/30 px-3 py-2">
            <div>
              <p className="text-xs font-medium text-dark-200">
                {t('admin.channelSubscriptions.isMain.label', 'Главный канал')}
              </p>
              <p className="mt-0.5 text-xs text-dark-400">
                {t('admin.channelSubscriptions.isMain.desc', 'Бот мониторит посты этого канала для нуджа')}
              </p>
            </div>
            <Toggle
              checked={channel.is_main}
              onChange={() => onSetMain(channel.id)}
            />
          </div>

          {/* Latest post read-only display (main channel only) */}
          {channel.is_main && channel.last_post_message_id && (
            <div className="mt-3 rounded-lg bg-dark-700/20 px-3 py-2">
              <p className="text-xs font-medium text-dark-300">
                {t('admin.channelSubscriptions.latestPost', 'Последний пост')}
              </p>
              {channel.last_post_title && (
                <p className="mt-0.5 text-xs text-dark-400 line-clamp-2">{channel.last_post_title}</p>
              )}
              {channel.last_post_link && (
                <a
                  href={channel.last_post_link}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-1 inline-block text-xs text-primary-400 hover:underline"
                >
                  {t('admin.channelSubscriptions.viewPost', 'Посмотреть →')}
                </a>
              )}
            </div>
          )}
```

In the page component where `ChannelCard` is rendered, add `setMainMutation` and pass `onSetMain`:

```tsx
  const setMainMutation = useMutation({
    mutationFn: (id: number) => adminChannelsApi.setMain(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin-channels'] });
      haptic.impact('light');
    },
    onError: () => {
      haptic.notification('error');
      notify.error(t('common.error'));
    },
  });
```

And pass `onSetMain={(id) => setMainMutation.mutate(id)}` to each `<ChannelCard />`.

- [ ] **Step 8: Add locale keys for is_main UI**

In `src/locales/ru.json`, under `admin.channelSubscriptions`, add:

```json
"isMain": {
  "label": "Главный канал",
  "desc": "Бот мониторит посты этого канала и показывает нудж"
},
"latestPost": "Последний пост",
"viewPost": "Посмотреть →"
```

In `src/locales/en.json`, under `admin.channelSubscriptions`, add:

```json
"isMain": {
  "label": "Main channel",
  "desc": "Bot monitors posts from this channel and shows the nudge"
},
"latestPost": "Latest post",
"viewPost": "View →"
```

- [ ] **Step 9: TypeScript + build + vitest**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx tsc --noEmit 2>&1 | head -20
npm run build 2>&1 | tail -10
npx vitest run 2>&1 | tail -10
```

Expected: all pass with no errors.

- [ ] **Step 10: Full backend regression pass**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
.venv/bin/python3 -m pytest tests/ -v --tb=short -q 2>&1 | tail -20
```

Expected: no new failures.

- [ ] **Step 11: Commit both repos**

```bash
# Backend
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
git add app/cabinet/routes/admin_channels.py tests/cabinet/test_admin_channel_is_main.py
git commit -m "$(cat <<'EOF'
feat(admin-api): эндпоинт POST /set-main для выбора главного канала

Устанавливает is_main=True на выбранном канале и сбрасывает у остальных —
гарантирует ровно один главный канал. Инвалидирует кеш каналов после изменения.
EOF
)"

# Frontend
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
git add src/api/adminChannels.ts src/pages/AdminChannelSubscriptions.tsx src/locales/ru.json src/locales/en.json
git commit -m "$(cat <<'EOF'
feat(admin-ui): тумблер «Главный канал» (is_main) и показ последнего поста в AdminChannelSubscriptions

Добавлен тумблер is_main на строку канала. При включении отправляет POST /set-main,
остальные каналы сбрасываются автоматически на бэкенде. Для главного канала
показывается read-only блок последнего поста с заголовком и ссылкой.
Добавлены ключи admin.channelSubscriptions.isMain в ru.json и en.json.
EOF
)"
```

---

## Self-Review

### 1. Spec Coverage Checklist

| Spec Requirement | Task |
|---|---|
| `RequiredChannel.is_main` bool default false | T1 |
| `last_post_message_id/link/title/at` on RequiredChannel | T1 |
| `User.last_seen_channel_post_id` int nullable | T1 |
| Alembic migration in `9\d{3}` style, chain from 9024 | T1 |
| `CHANNEL_SOFT_MODE: bool = True` in config | T2 |
| Soft mode: never deactivate (trial or paid) | T2 |
| Gate screen still shows in soft mode | T2 (short-circuit only calls `deactivate`, not the gate itself) |
| `channel_post` handler for main channel → update `last_post_*` | T3 |
| Non-main channels ignored | T3 |
| Graceful if bot not admin (no events arrive, no crash) | T3 |
| Bot post card after gate passes | T4 |
| Card text `🆕 Свежее в канале: {title}` + URL button | T4 |
| Set `user.last_seen_channel_post_id` after show | T4 |
| `GET /cabinet/channel-nudge` shape | T5 |
| `needs_subscribe` for telegram user (API check) | T5 |
| `needs_subscribe=true` for email-only user | T5 |
| Panel/Telegram error → degrade, no 500 | T5 |
| `show_post` by `last_seen_channel_post_id` comparison | T5 |
| `POST /cabinet/channel-nudge/seen` | T5 |
| `getChannelNudge()` + `markChannelPostSeen()` in frontend API | T6 |
| Dismissible popup on cabinet load | T6 |
| Post block (all, if show_post) | T6 |
| Subscribe block (only if needs_subscribe) | T6 |
| On show/close → `markChannelPostSeen` | T6 |
| Must NOT block cabinet | T6 |
| Admin `is_main` toggle in `AdminChannelSubscriptions.tsx` | T7 |
| `adminChannelsApi.setMain(id)` | T7 |
| Setting is_main clears others (backend enforces) | T7 |
| Show latest post read-only in admin | T7 |
| Spec edge: no main channel → `latest_post=null`, `show_post=false` | T5 (handled) |
| Spec edge: no posts → show subscribe-only if `needs_subscribe`, else skip | T5 (handled — `show_post=false` when no post) |
| Spec edge: `latest_post=null` + `needs_subscribe=true` → popup with subscribe-only | T5+T6 (needs_subscribe=true triggers popup even when show_post=false) |

### 2. Placeholder Scan

All code blocks in this plan contain real, runnable code. No "TBD", "fill in", or "similar to Task N" phrases are present. Commands include expected outputs.

### 3. Type Consistency Check

- `ChannelNudgeResponse` defined in T5 (schema), consumed in T6 (frontend type mirrors it as `ChannelNudgeData`)
- `ChannelPostInfo` defined in T5 schema, mirrored as `ChannelPostInfo` in `src/api/channelNudge.ts` T6
- `update_user_last_seen_post(db, user_id, post_id)` defined in T4, consumed in T5 (`channel_nudge.py`)
- `set_main_channel(db, channel_db_id)` defined in T3, consumed in T7 (`admin_channels.py`)
- `update_channel_post(db, channel_db_id, message_id, link, title, at)` defined in T3, consumed in T3 (`channel_post.py`)
- `channel_subscription_service.get_main_channel()` defined in T3, consumed in T4, T5
- `ChannelResponse.is_main` added in T5; `RequiredChannel.is_main` in `src/api/adminChannels.ts` added in T7 — consistent
- `_send_channel_post_nudge(bot, telegram_id, db_user, main_channel, db)` defined and tested in T4

### Spec Ambiguities Resolved

1. **"Если `latest_post=null` и `needs_subscribe`, показать попап-подписку один раз (seen помечать отдельным маркером/`last_seen_channel_post_id=0`)"** — The spec floats the idea of using `id=0` as the subscribe-only seen marker. **Resolution:** The frontend popup shows whenever `needs_subscribe || show_post`. If there is no post (`latest_post=null`), `markChannelPostSeen` is NOT called (no `post_id` available), so the subscribe-only popup will reappear on the next cabinet load. This matches "показываем при первом входе через seen=null" (spec §edge-case). Implementing a separate `last_seen_channel_post_id=0` sentinel adds complexity for MVP; deferred unless the owner explicitly requests it.

2. **Injection point for bot nudge card** — Spec says "after the gate passes". The gate UI is in `ChannelCheckerMiddleware`. The pass-through fires inside the `sub_channel_check` callback branch (when the user clicks "Check subscription"). **Resolution:** Nudge is injected in that exact branch before `return await handler(event, data)`.

3. **`last_post_at` column type** — Spec does not specify timezone. **Resolution:** Uses `AwareDateTime()` (timezone-aware), consistent with `created_at`/`updated_at` on the same model.

4. **Whether `is_main` filters affect the channel gate** — The gate should continue checking all required channels, not just `is_main`. **Resolution:** Gate logic in `channel_checker.py` is unchanged; only the nudge card uses `get_main_channel()`.
