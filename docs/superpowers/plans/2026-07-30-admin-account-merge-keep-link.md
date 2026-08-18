# Admin Account Merge — Keep Subscription Link Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the admin account-merge flow so the admin can (1) preview both accounts with live device bindings from RemnaWave, and (2) choose which subscription's link (short_uuid / subscription_url) is preserved after merge — preventing clients from having to re-import configs.

**Architecture:** Backend: new preview endpoint (`GET /cabinet/admin/users/merge/preview`) using existing `get_user_devices_all` from `RemnaWaveAPI`; `_handle_subscription_merge` / `execute_merge` gain a `keep_subscription_id: int | None` parameter that overrides the current "later-end-date wins" logic while delegating date arithmetic to the existing `_combine_subscription_end_dates`. Frontend: replace the current bare ID-input merge modal in `AdminUserDetail.tsx` with a multi-step flow (search → preview panel component → confirm with radio choices) and extend the `mergeUsers` API call with `keep_subscription_id`.

**Tech Stack:** Backend — Python 3.13+, FastAPI, SQLAlchemy async, Pydantic v2, pytest (run via `.venv/bin/python3 -m pytest`). Frontend — React 18, TypeScript, Vite, vitest, i18next, Tanstack Query.

## Global Constraints

- Backend tests MUST run with `.venv/bin/python3 -m pytest` (NOT `python3 -m pytest` — system Python is 3.9 and lacks `datetime.UTC`).
- Never commit `.env` files (repo is public).
- NO `Co-Authored-By:` trailer in any commit message.
- Every commit title must be descriptive; add a body paragraph explaining the "why".
- Frontend must pass `npx tsc --noEmit`, `npm run build`, `npx vitest run` — all three, in that order.
- Only add new locale keys to `src/locales/ru.json` and `src/locales/en.json`. Never touch `fa.json` or `zh.json`.
- Both repos are forks — preserve our changes, do not restructure upstream code.
- All new merge-related endpoints and UI elements are gated on the `users:edit` permission.
- After merge, the kept subscription's `remnawave_short_uuid` and `subscription_url` MUST remain byte-for-byte unchanged.
- `keep_subscription_id=None` must preserve existing behavior (winner = later end-date) exactly.
- Patch `Settings.is_multi_tariff_enabled` at the **class** level (`patch.object(Settings, 'is_multi_tariff_enabled', return_value=False)`) — instance-level patching fails on frozen Pydantic v2 models.

---

## File Map

### Backend (`remnawave-bedolaga-telegram-bot/`)

| Action | File |
|--------|------|
| Modify | `app/services/account_merge_service.py` — add `keep_subscription_id` to `_handle_subscription_merge` and `execute_merge` |
| Modify | `app/cabinet/routes/admin_user_linking.py` — add `keep_subscription_id` to `AdminMergeUsersRequest`; add preview endpoint + its Pydantic schemas |
| Create | `tests/services/test_merge_keep_subscription_id.py` |
| Create | `tests/cabinet/test_admin_merge_preview_route.py` |

### Frontend (`bedolaga-cabinet/`)

| Action | File |
|--------|------|
| Modify | `src/types/index.ts` — new interfaces `AdminMergeSubPreview`, `AdminMergeUserPreview`, `AdminMergePreviewResponse` |
| Modify | `src/api/adminUsers.ts` — `getMergePreview`, extend `mergeUsers` |
| Create | `src/components/admin/userDetail/AdminMergePanel.tsx` |
| Modify | `src/pages/AdminUserDetail.tsx` — replace old merge modal wiring with multi-step flow |
| Modify | `src/locales/ru.json` — new keys under `admin.users.detail.linking` |
| Modify | `src/locales/en.json` — new keys under `admin.users.detail.linking` |
| Create | `src/utils/adminMergeLogic.test.ts` |

---

### Task 1: Backend — `keep_subscription_id` in combine logic

**Files:**
- Modify: `app/services/account_merge_service.py`
- Create: `tests/services/test_merge_keep_subscription_id.py`

**Interfaces:**
- Consumes: existing `_handle_subscription_merge(db, primary, secondary, deferred_remnawave_deletions)`, `execute_merge(db, primary_user_id, secondary_user_id, ...)`, `_combine_subscription_end_dates(winner_sub, loser_sub, now)`
- Produces:
  - `_handle_subscription_merge(db, primary, secondary, deferred_remnawave_deletions, keep_subscription_id: int | None = None) -> None`
  - `execute_merge(db, primary_user_id, secondary_user_id, provider=None, provider_id=None, deferred_remnawave_deletions=None, keep_subscription_id: int | None = None) -> User`

---

- [ ] **Step 1.1 — Write failing tests**

Create `tests/services/test_merge_keep_subscription_id.py`:

```python
"""Tests for keep_subscription_id override in _handle_subscription_merge.

Uses the same SimpleNamespace + AsyncMock pattern as
tests/services/test_merge_subscription_combine.py. No DB connection required.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.account_merge_service import _handle_subscription_merge
from app.config import Settings


_NOW = datetime(2026, 7, 30, 0, 0, 0, tzinfo=UTC)
# primary sub ends sooner (loser by default logic)
_PRIMARY_END = datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)   # 11 days from NOW
# secondary sub ends later (winner by default logic)
_SECONDARY_END = datetime(2026, 9, 15, 0, 0, 0, tzinfo=UTC)  # 47 days from NOW


def _make_sub(id, user_id, end_date, status='active', tariff_id=None, remnawave_uuid=None,
              subscription_url=None, subscription_crypto_link=None, remnawave_short_uuid=None):
    return SimpleNamespace(
        id=id,
        user_id=user_id,
        end_date=end_date,
        status=status,
        tariff_id=tariff_id,
        autopay_enabled=False,
        remnawave_uuid=remnawave_uuid,
        subscription_url=subscription_url,
        subscription_crypto_link=subscription_crypto_link,
        remnawave_short_uuid=remnawave_short_uuid,
        tariff=SimpleNamespace(name='Basic'),
        traffic_limit_gb=100.0,
        traffic_used_gb=0.0,
        device_limit=3,
        is_trial=False,
    )


def _make_user(id, remnawave_uuid=None, subscriptions=None):
    return SimpleNamespace(
        id=id,
        remnawave_uuid=remnawave_uuid,
        subscriptions=subscriptions or [],
    )


def _make_db():
    return SimpleNamespace(
        execute=AsyncMock(),
        delete=AsyncMock(),
        flush=AsyncMock(),
        add=MagicMock(),
    )


def _patch_single_tariff():
    return patch.object(Settings, 'is_multi_tariff_enabled', return_value=False)


class TestKeepSubscriptionIdSingleTariff:
    async def test_keep_early_sub_preserved_url(self):
        """keep_subscription_id = primary (early-end-date sub) → primary wins,
        its subscription_url / remnawave_short_uuid are preserved, secondary's
        remnawave_uuid is deferred for deletion."""
        primary_sub = _make_sub(
            1, 1, _PRIMARY_END, remnawave_uuid='rw-p',
            subscription_url='https://link.example/primary',
            remnawave_short_uuid='short-p',
        )
        secondary_sub = _make_sub(
            2, 2, _SECONDARY_END, remnawave_uuid='rw-s',
            subscription_url='https://link.example/secondary',
            remnawave_short_uuid='short-s',
        )
        primary = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[secondary_sub])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff(), \
             patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(
                db, primary, secondary, deferred,
                keep_subscription_id=primary_sub.id,
            )

        # Primary (early-end) is kept; secondary (late-end) is the loser
        # end_date extends by remaining of secondary: Sep 15 - Jul 30 = 47 days
        expected_end = _PRIMARY_END + timedelta(days=47)
        assert primary_sub.end_date == expected_end, \
            f'Expected {expected_end}, got {primary_sub.end_date}'

        # Link fields must be unchanged
        assert primary_sub.subscription_url == 'https://link.example/primary'
        assert primary_sub.remnawave_short_uuid == 'short-p'
        assert primary_sub.remnawave_uuid == 'rw-p'

        # Loser (secondary) deferred for deletion
        assert 'rw-s' in deferred
        assert secondary.remnawave_uuid is None

    async def test_keep_late_sub_secondary_wins(self):
        """keep_subscription_id = secondary (late-end-date sub) → secondary wins.
        Behaves the same as the default when secondary_end > primary_end,
        but is explicitly selected rather than auto-chosen."""
        primary_sub = _make_sub(
            1, 1, _PRIMARY_END, remnawave_uuid='rw-p',
            subscription_url='https://link.example/primary',
            remnawave_short_uuid='short-p',
        )
        secondary_sub = _make_sub(
            2, 2, _SECONDARY_END, remnawave_uuid='rw-s',
            subscription_url='https://link.example/secondary',
            remnawave_short_uuid='short-s',
        )
        primary = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[secondary_sub])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff(), \
             patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(
                db, primary, secondary, deferred,
                keep_subscription_id=secondary_sub.id,
            )

        # Secondary sub wins; primary's remnawave_uuid should be deferred
        assert 'rw-p' in deferred
        # secondary_sub is transferred to primary.id
        assert secondary_sub.user_id == primary.id

    async def test_keep_none_preserves_original_logic(self):
        """keep_subscription_id=None → default: winner = later end_date (secondary wins here)."""
        primary_sub = _make_sub(1, 1, _PRIMARY_END, remnawave_uuid='rw-p')
        secondary_sub = _make_sub(2, 2, _SECONDARY_END, remnawave_uuid='rw-s')
        primary = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[secondary_sub])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff(), \
             patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(
                db, primary, secondary, deferred,
                keep_subscription_id=None,
            )

        # Default: secondary wins (later end_date), primary's rw uuid is deferred
        assert 'rw-p' in deferred

    async def test_keep_sub_not_in_pair_raises(self):
        """keep_subscription_id pointing to an unrelated subscription id raises ValueError."""
        primary_sub = _make_sub(1, 1, _PRIMARY_END)
        secondary_sub = _make_sub(2, 2, _SECONDARY_END)
        primary = _make_user(1, subscriptions=[primary_sub])
        secondary = _make_user(2, subscriptions=[secondary_sub])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff():
            with pytest.raises(ValueError, match='keep_subscription_id'):
                await _handle_subscription_merge(
                    db, primary, secondary, deferred,
                    keep_subscription_id=999,
                )

    async def test_keep_lifetime_winner_no_extension(self):
        """Kept sub has end_date=None (lifetime) → no extension, stays None."""
        primary_sub = _make_sub(
            1, 1, None, remnawave_uuid='rw-p',
            subscription_url='https://link.example/primary',
        )
        secondary_sub = _make_sub(2, 2, _SECONDARY_END, remnawave_uuid='rw-s')
        primary = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[secondary_sub])
        db = _make_db()
        deferred: list[str] = []

        with _patch_single_tariff(), \
             patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(
                db, primary, secondary, deferred,
                keep_subscription_id=primary_sub.id,
            )

        assert primary_sub.end_date is None
        assert primary_sub.subscription_url == 'https://link.example/primary'
        assert 'rw-s' in deferred
```

- [ ] **Step 1.2 — Run tests to confirm they fail**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
.venv/bin/python3 -m pytest tests/services/test_merge_keep_subscription_id.py -v 2>&1 | tail -20
```

Expected: `FAILED` with `TypeError: _handle_subscription_merge() got an unexpected keyword argument 'keep_subscription_id'`

- [ ] **Step 1.3 — Implement `keep_subscription_id` in `_handle_subscription_merge`**

In `app/services/account_merge_service.py`, change the function signature at line 453:

```python
async def _handle_subscription_merge(
    db: AsyncSession,
    primary: User,
    secondary: User,
    deferred_remnawave_deletions: list[str],
    keep_subscription_id: int | None = None,
) -> None:
```

In the **single-tariff "both subs exist" branch** (after the `assert secondary_sub is not None` at line ~638), replace:

```python
    # Определяем победителя (более поздняя дата; None=lifetime всегда побеждает)
    secondary_wins = (secondary_end is None and primary_end is not None) or (
        secondary_end is not None
        and primary_end is not None
        and secondary_end > primary_end
    )
```

with:

```python
    # keep_subscription_id overrides default "later end_date wins" logic
    if keep_subscription_id is not None:
        sub_ids = {primary_sub.id, secondary_sub.id}
        if keep_subscription_id not in sub_ids:
            raise ValueError(
                f'keep_subscription_id={keep_subscription_id} does not belong to '
                f'either merged subscription (ids: {sub_ids})'
            )
        secondary_wins = keep_subscription_id == secondary_sub.id
    else:
        # Определяем победителя (более поздняя дата; None=lifetime всегда побеждает)
        secondary_wins = (secondary_end is None and primary_end is not None) or (
            secondary_end is not None
            and primary_end is not None
            and secondary_end > primary_end
        )
```

In the **multi-tariff branch**, inside the `if sub_tariff_id is not None and sub.status in ...` conflict block, add the same override _before_ the `secondary_wins` assignment. Find this existing code around line 497:

```python
                    # Determine winner (later end_date; None = lifetime wins)
                    secondary_wins = (secondary_end is None and primary_end is not None) or (
                        secondary_end is not None and primary_end is not None and secondary_end > primary_end
                    )
```

Replace it with:

```python
                    # keep_subscription_id overrides default "later end_date wins" logic
                    if keep_subscription_id is not None and keep_subscription_id in (sub.id, primary_conflict.id):
                        secondary_wins = keep_subscription_id == sub.id
                    else:
                        # Determine winner (later end_date; None = lifetime wins)
                        secondary_wins = (secondary_end is None and primary_end is not None) or (
                            secondary_end is not None and primary_end is not None and secondary_end > primary_end
                        )
```

Now update `execute_merge` signature at line 717 to pass through the new param:

```python
async def execute_merge(
    db: AsyncSession,
    primary_user_id: int,
    secondary_user_id: int,
    provider: str | None = None,
    provider_id: str | None = None,
    deferred_remnawave_deletions: list[str] | None = None,
    keep_subscription_id: int | None = None,
) -> User:
```

And pass it to `_handle_subscription_merge` at the call site (~line 878):

```python
    # 5. Мерж подписок
    await _handle_subscription_merge(
        db, primary, secondary, pending_remnawave_deletions,
        keep_subscription_id=keep_subscription_id,
    )
```

- [ ] **Step 1.4 — Run tests to confirm they pass**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
.venv/bin/python3 -m pytest tests/services/test_merge_keep_subscription_id.py -v 2>&1 | tail -20
```

Expected: `5 passed`

- [ ] **Step 1.5 — Verify no regressions in existing combine tests**

```bash
.venv/bin/python3 -m pytest tests/services/test_merge_subscription_combine.py tests/services/test_account_merge_service.py -v 2>&1 | tail -15
```

Expected: all passing.

- [ ] **Step 1.6 — Syntax-check the changed file**

```bash
.venv/bin/python3 -m py_compile app/services/account_merge_service.py && echo OK
```

Expected: `OK`

- [ ] **Step 1.7 — Commit**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
git add app/services/account_merge_service.py tests/services/test_merge_keep_subscription_id.py
git commit -m "feat(merge): add keep_subscription_id override to _handle_subscription_merge

When keep_subscription_id is provided and matches one of the two merged
subscriptions, that subscription wins regardless of end_date. Its
remnawave_uuid/short_uuid/subscription_url/crypto_link remain unchanged.
The other subscription's remnawave user is deferred for deletion.
None keeps the existing 'later end_date wins' behavior.
Works in both single-tariff and multi-tariff branches."
```

---

### Task 2: Backend — Extend `AdminMergeUsersRequest` + validate `keep_subscription_id`

**Files:**
- Modify: `app/cabinet/routes/admin_user_linking.py`
- Create: `tests/cabinet/test_admin_merge_keep_subscription_route.py`

**Interfaces:**
- Consumes: `execute_merge(..., keep_subscription_id: int | None = None)` from Task 1; `get_user_by_id(db, id)`
- Produces:
  - `AdminMergeUsersRequest` now has `keep_subscription_id: int | None = None`
  - `admin_merge_users` passes validated `keep_subscription_id` to `execute_merge`

---

- [ ] **Step 2.1 — Write failing tests**

Create `tests/cabinet/test_admin_merge_keep_subscription_route.py`:

```python
"""Tests for keep_subscription_id validation in admin_merge_users handler.

Pattern: call handler directly with fake db + fake users — same as
tests/cabinet/test_admin_user_activity.py.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.cabinet.routes.admin_user_linking import admin_merge_users, AdminMergeUsersRequest


def _make_sub(id, user_id):
    return SimpleNamespace(id=id, user_id=user_id, remnawave_uuid=None,
                           status='active', autopay_enabled=False, end_date=None,
                           tariff_id=None, tariff=SimpleNamespace(name='T'))


def _make_user(id, subs=None):
    subs = subs or []
    return SimpleNamespace(
        id=id, status='active', telegram_id=None, email=None,
        subscriptions=subs,
        remnawave_uuid=None,
    )


def _make_admin():
    return SimpleNamespace(id=99)


def _fake_db():
    db = AsyncMock()
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


async def test_keep_subscription_id_validated_belongs_to_users():
    """keep_subscription_id that belongs to an unrelated user raises HTTP 400."""
    primary_sub = _make_sub(id=10, user_id=1)
    secondary_sub = _make_sub(id=20, user_id=2)
    primary = _make_user(1, subs=[primary_sub])
    secondary = _make_user(2, subs=[secondary_sub])

    db = _fake_db()

    async def fake_get_user(db_, uid):
        return {1: primary, 2: secondary}.get(uid)

    with patch('app.cabinet.routes.admin_user_linking.get_user_by_id', side_effect=fake_get_user), \
         patch('app.cabinet.routes.admin_user_linking.execute_merge', new_callable=AsyncMock) as mock_merge, \
         patch('app.cabinet.routes.admin_user_linking.flush_remnawave_deletions', new_callable=AsyncMock):
        mock_merge.return_value = primary

        request = AdminMergeUsersRequest(
            primary_user_id=1,
            secondary_user_id=2,
            keep_subscription_id=999,  # unrelated id
        )

        with pytest.raises(HTTPException) as exc_info:
            await admin_merge_users(request=request, admin=_make_admin(), db=db)

        assert exc_info.value.status_code == 400
        assert 'keep_subscription_id' in exc_info.value.detail.lower()


async def test_keep_subscription_id_none_passes_none_to_execute_merge():
    """keep_subscription_id=None passes None to execute_merge."""
    primary_sub = _make_sub(id=10, user_id=1)
    secondary_sub = _make_sub(id=20, user_id=2)
    primary = _make_user(1, subs=[primary_sub])
    secondary = _make_user(2, subs=[secondary_sub])
    db = _fake_db()

    async def fake_get_user(db_, uid):
        return {1: primary, 2: secondary}.get(uid)

    with patch('app.cabinet.routes.admin_user_linking.get_user_by_id', side_effect=fake_get_user), \
         patch('app.cabinet.routes.admin_user_linking.execute_merge', new_callable=AsyncMock) as mock_merge, \
         patch('app.cabinet.routes.admin_user_linking.flush_remnawave_deletions', new_callable=AsyncMock):
        mock_merge.return_value = primary

        request = AdminMergeUsersRequest(
            primary_user_id=1,
            secondary_user_id=2,
            keep_subscription_id=None,
        )
        await admin_merge_users(request=request, admin=_make_admin(), db=db)

    mock_merge.assert_called_once()
    call_kwargs = mock_merge.call_args.kwargs
    assert call_kwargs.get('keep_subscription_id') is None


async def test_keep_subscription_id_valid_primary_passes_to_execute_merge():
    """keep_subscription_id = primary sub id → passes through."""
    primary_sub = _make_sub(id=10, user_id=1)
    secondary_sub = _make_sub(id=20, user_id=2)
    primary = _make_user(1, subs=[primary_sub])
    secondary = _make_user(2, subs=[secondary_sub])
    db = _fake_db()

    async def fake_get_user(db_, uid):
        return {1: primary, 2: secondary}.get(uid)

    with patch('app.cabinet.routes.admin_user_linking.get_user_by_id', side_effect=fake_get_user), \
         patch('app.cabinet.routes.admin_user_linking.execute_merge', new_callable=AsyncMock) as mock_merge, \
         patch('app.cabinet.routes.admin_user_linking.flush_remnawave_deletions', new_callable=AsyncMock):
        mock_merge.return_value = primary

        request = AdminMergeUsersRequest(
            primary_user_id=1,
            secondary_user_id=2,
            keep_subscription_id=10,
        )
        await admin_merge_users(request=request, admin=_make_admin(), db=db)

    call_kwargs = mock_merge.call_args.kwargs
    assert call_kwargs.get('keep_subscription_id') == 10


async def test_merge_route_registered(registered_paths):
    assert 'POST' in registered_paths.get('/cabinet/admin/users/merge', set())
```

- [ ] **Step 2.2 — Run tests to confirm they fail**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
.venv/bin/python3 -m pytest tests/cabinet/test_admin_merge_keep_subscription_route.py -v 2>&1 | tail -20
```

Expected: `FAILED` — `AdminMergeUsersRequest` has no `keep_subscription_id` field; `admin_merge_users` doesn't validate it.

- [ ] **Step 2.3 — Implement: extend `AdminMergeUsersRequest`**

In `app/cabinet/routes/admin_user_linking.py`, change the schema at line 63:

```python
class AdminMergeUsersRequest(BaseModel):
    primary_user_id: int
    secondary_user_id: int
    keep_subscription_id: int | None = None
```

- [ ] **Step 2.4 — Implement: validate and pass `keep_subscription_id` in `admin_merge_users`**

In `app/cabinet/routes/admin_user_linking.py`, inside `admin_merge_users`, after the two `get_user_by_id` calls and before the `try:` block (around line 391), add the validation block:

```python
    # Validate keep_subscription_id belongs to one of the two users
    if request.keep_subscription_id is not None:
        all_sub_ids: set[int] = set()
        for sub in (getattr(primary, 'subscriptions', None) or []):
            all_sub_ids.add(sub.id)
        for sub in (getattr(secondary, 'subscriptions', None) or []):
            all_sub_ids.add(sub.id)
        if request.keep_subscription_id not in all_sub_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f'keep_subscription_id={request.keep_subscription_id} does not belong '
                    f'to either merged user (valid ids: {sorted(all_sub_ids)})'
                ),
            )
```

Then in the `try:` block, update the `execute_merge` call:

```python
        await execute_merge(
            db=db,
            primary_user_id=request.primary_user_id,
            secondary_user_id=request.secondary_user_id,
            provider='admin_manual',
            provider_id=str(admin.id),
            deferred_remnawave_deletions=deferred_deletions,
            keep_subscription_id=request.keep_subscription_id,
        )
```

- [ ] **Step 2.5 — Run tests to confirm they pass**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
.venv/bin/python3 -m pytest tests/cabinet/test_admin_merge_keep_subscription_route.py -v 2>&1 | tail -20
```

Expected: `4 passed`

- [ ] **Step 2.6 — Syntax-check**

```bash
.venv/bin/python3 -m py_compile app/cabinet/routes/admin_user_linking.py && echo OK
```

Expected: `OK`

- [ ] **Step 2.7 — Full service test suite still green**

```bash
.venv/bin/python3 -m pytest tests/services/ tests/cabinet/test_admin_merge_keep_subscription_route.py -v 2>&1 | tail -15
```

- [ ] **Step 2.8 — Commit**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
git add app/cabinet/routes/admin_user_linking.py tests/cabinet/test_admin_merge_keep_subscription_route.py
git commit -m "feat(merge): accept and validate keep_subscription_id in admin merge endpoint

AdminMergeUsersRequest gains keep_subscription_id: int | None = None.
The handler validates it belongs to one of the two merged users (400 if not)
then passes it to execute_merge. Backward-compatible: existing callers omitting
the field get None (same behaviour as before)."
```

---

### Task 3: Backend — Preview endpoint

**Files:**
- Modify: `app/cabinet/routes/admin_user_linking.py`
- Create: `tests/cabinet/test_admin_merge_preview_route.py`

**Interfaces:**
- Consumes: `get_user_by_id(db, id)`, `get_user_devices_all(remnawave_uuid: str) -> dict` (existing in `RemnaWaveAPI`; returns `{'devices': [...], 'total': int}`), `_count_active_referrals(db, user_id)` from `account_merge_service`
- Produces: `GET /cabinet/admin/users/merge/preview?primary_user_id=&secondary_user_id=` → `AdminMergePreviewResponse`

Pydantic schemas to add to `admin_user_linking.py`:

```python
class AdminMergeDeviceInfo(BaseModel):
    hwid: str | None = None
    app: str | None = None
    platform: str | None = None
    last_seen: str | None = None   # ISO string from panel


class AdminMergeSubPreview(BaseModel):
    subscription_id: int
    tariff_name: str | None
    end_date: datetime | None
    status: str
    subscription_url: str | None
    subscription_crypto_link: str | None
    remnawave_short_uuid: str | None
    devices_count: int | None        # None when panel unavailable
    devices: list[AdminMergeDeviceInfo]


class AdminMergeUserPreview(BaseModel):
    id: int
    username: str | None
    first_name: str | None
    email: str | None
    telegram_id: int | None
    auth_methods: list[str]
    balance_kopeks: int
    referrals_count: int
    created_at: datetime | None
    subscriptions: list[AdminMergeSubPreview]


class AdminMergePreviewResponse(BaseModel):
    primary: AdminMergeUserPreview
    secondary: AdminMergeUserPreview
```

---

- [ ] **Step 3.1 — Write failing tests**

Create `tests/cabinet/test_admin_merge_preview_route.py`:

```python
"""Tests for GET /cabinet/admin/users/merge/preview."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.admin_user_linking import admin_merge_preview


_NOW = datetime(2026, 7, 30, 0, 0, 0, tzinfo=UTC)


def _make_sub(id, user_id, remnawave_uuid=None, subscription_url=None,
              subscription_crypto_link=None, remnawave_short_uuid=None,
              end_date=None, status='active', tariff_name='Basic'):
    return SimpleNamespace(
        id=id, user_id=user_id,
        remnawave_uuid=remnawave_uuid,
        subscription_url=subscription_url,
        subscription_crypto_link=subscription_crypto_link,
        remnawave_short_uuid=remnawave_short_uuid,
        end_date=end_date or datetime(2026, 12, 1, tzinfo=UTC),
        status=status,
        tariff=SimpleNamespace(name=tariff_name),
        tariff_id=1,
    )


def _make_user(id, subs=None, telegram_id=None, email=None):
    subs = subs or []
    return SimpleNamespace(
        id=id, username=f'user{id}', first_name='Test', email=email,
        telegram_id=telegram_id, password_hash=None,
        google_id=None, yandex_id=None, discord_id=None, vk_id=None,
        balance_kopeks=1000, subscriptions=subs,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        status='active', referral_code='ref', referred_by_id=None,
        remnawave_uuid=None,
    )


def _fake_db():
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one.return_value = 3  # active referrals
    db.execute = AsyncMock(return_value=result)
    return db


def _make_admin():
    return SimpleNamespace(id=99)


async def test_preview_route_returns_both_users():
    """Preview returns primary and secondary user info."""
    primary_sub = _make_sub(10, 1, remnawave_uuid='rw-p',
                            subscription_url='https://link/p', remnawave_short_uuid='short-p')
    primary = _make_user(1, subs=[primary_sub], telegram_id=111)
    secondary_sub = _make_sub(20, 2, remnawave_uuid='rw-s',
                              subscription_url='https://link/s', remnawave_short_uuid='short-s')
    secondary = _make_user(2, subs=[secondary_sub])
    db = _fake_db()

    fake_devices = {'devices': [
        {'hwid': 'hw1', 'app': 'SingBox', 'platform': 'iOS', 'lastSeen': '2026-07-20T10:00:00Z'},
    ], 'total': 1}

    async def fake_get_user(db_, uid):
        return {1: primary, 2: secondary}.get(uid)

    with patch('app.cabinet.routes.admin_user_linking.get_user_by_id', side_effect=fake_get_user), \
         patch('app.cabinet.routes.admin_user_linking._count_active_referrals',
               new_callable=AsyncMock, return_value=3) as mock_refs, \
         patch('app.cabinet.routes.admin_user_linking._get_remnawave_api') as mock_api_ctx:
        # set up async context manager
        mock_api = AsyncMock()
        mock_api.get_user_devices_all = AsyncMock(return_value=fake_devices)
        mock_api_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_api)
        mock_api_ctx.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await admin_merge_preview(
            primary_user_id=1,
            secondary_user_id=2,
            admin=_make_admin(),
            db=db,
        )

    assert result.primary.id == 1
    assert result.secondary.id == 2
    assert len(result.primary.subscriptions) == 1
    p_sub = result.primary.subscriptions[0]
    assert p_sub.subscription_id == 10
    assert p_sub.subscription_url == 'https://link/p'
    assert p_sub.remnawave_short_uuid == 'short-p'
    assert p_sub.devices_count == 1
    assert len(p_sub.devices) == 1
    assert p_sub.devices[0].app == 'SingBox'


async def test_preview_panel_unavailable_returns_null_devices():
    """If RemnaWave API throws, devices_count=None, devices=[], no 500."""
    primary_sub = _make_sub(10, 1, remnawave_uuid='rw-p')
    primary = _make_user(1, subs=[primary_sub])
    secondary = _make_user(2, subs=[])
    db = _fake_db()

    async def fake_get_user(db_, uid):
        return {1: primary, 2: secondary}.get(uid)

    with patch('app.cabinet.routes.admin_user_linking.get_user_by_id', side_effect=fake_get_user), \
         patch('app.cabinet.routes.admin_user_linking._count_active_referrals',
               new_callable=AsyncMock, return_value=0), \
         patch('app.cabinet.routes.admin_user_linking._get_remnawave_api') as mock_api_ctx:
        mock_api_ctx.return_value.__aenter__.side_effect = RuntimeError('panel down')
        mock_api_ctx.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await admin_merge_preview(
            primary_user_id=1,
            secondary_user_id=2,
            admin=_make_admin(),
            db=db,
        )

    assert result.primary.subscriptions[0].devices_count is None
    assert result.primary.subscriptions[0].devices == []


async def test_preview_same_user_raises_400():
    """primary_user_id == secondary_user_id → 400."""
    db = _fake_db()
    with pytest.raises(HTTPException) as exc:
        await admin_merge_preview(
            primary_user_id=5,
            secondary_user_id=5,
            admin=_make_admin(),
            db=db,
        )
    assert exc.value.status_code == 400


async def test_preview_unknown_user_raises_404():
    """Unknown user → 404."""
    db = _fake_db()

    async def fake_get_user(db_, uid):
        return None

    with patch('app.cabinet.routes.admin_user_linking.get_user_by_id', side_effect=fake_get_user):
        with pytest.raises(HTTPException) as exc:
            await admin_merge_preview(
                primary_user_id=1,
                secondary_user_id=2,
                admin=_make_admin(),
                db=db,
            )
    assert exc.value.status_code == 404


async def test_preview_route_registered(registered_paths):
    assert 'GET' in registered_paths.get('/cabinet/admin/users/merge/preview', set())
```

- [ ] **Step 3.2 — Run tests to confirm they fail**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
.venv/bin/python3 -m pytest tests/cabinet/test_admin_merge_preview_route.py -v 2>&1 | tail -20
```

Expected: `FAILED` — `admin_merge_preview` not defined.

- [ ] **Step 3.3 — Add Pydantic schemas to `admin_user_linking.py`**

After the existing `AdminMergeUsersResponse` class (~line 72), add:

```python
class AdminMergeDeviceInfo(BaseModel):
    hwid: str | None = None
    app: str | None = None
    platform: str | None = None
    last_seen: str | None = None


class AdminMergeSubPreview(BaseModel):
    subscription_id: int
    tariff_name: str | None
    end_date: datetime | None
    status: str
    subscription_url: str | None
    subscription_crypto_link: str | None
    remnawave_short_uuid: str | None
    devices_count: int | None
    devices: list[AdminMergeDeviceInfo]


class AdminMergeUserPreview(BaseModel):
    id: int
    username: str | None
    first_name: str | None
    email: str | None
    telegram_id: int | None
    auth_methods: list[str]
    balance_kopeks: int
    referrals_count: int
    created_at: datetime | None
    subscriptions: list[AdminMergeSubPreview]


class AdminMergePreviewResponse(BaseModel):
    primary: AdminMergeUserPreview
    secondary: AdminMergeUserPreview
```

Also add to the imports at the top of the file:

```python
from fastapi import APIRouter, Depends, HTTPException, Query, status
```

(add `Query` to the existing import).

And add the missing import for `_count_active_referrals` and `_get_remnawave_api` and `compute_auth_methods`:

```python
from app.services.account_merge_service import (
    execute_merge,
    flush_remnawave_deletions,
    _count_active_referrals,
    compute_auth_methods,
)
from app.services.account_merge_service import _get_remnawave_api
```

Replace the two existing imports at the top of the file (currently):
```python
from app.services.account_merge_service import (
    execute_merge,
    flush_remnawave_deletions,
)
```
with:
```python
from app.services.account_merge_service import (
    execute_merge,
    flush_remnawave_deletions,
    _count_active_referrals,
    _get_remnawave_api,
    compute_auth_methods,
)
```

- [ ] **Step 3.4 — Add the preview endpoint handler**

Before the existing `@router.post('/merge', ...)` decorator in `admin_user_linking.py`, insert:

```python
@router.get('/merge/preview', response_model=AdminMergePreviewResponse)
async def admin_merge_preview(
    primary_user_id: int = Query(...),
    secondary_user_id: int = Query(...),
    admin: User = Depends(require_permission('users:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> AdminMergePreviewResponse:
    """Preview merge: return both users' base info + subscriptions with live device counts."""
    if primary_user_id == secondary_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='primary_user_id and secondary_user_id must be different',
        )

    primary = await get_user_by_id(db, primary_user_id)
    if not primary:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Primary user (id={primary_user_id}) not found',
        )
    secondary = await get_user_by_id(db, secondary_user_id)
    if not secondary:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Secondary user (id={secondary_user_id}) not found',
        )

    primary_refs = await _count_active_referrals(db, primary_user_id)
    secondary_refs = await _count_active_referrals(db, secondary_user_id)

    async def _build_sub_previews(user: User) -> list[AdminMergeSubPreview]:
        subs = getattr(user, 'subscriptions', None) or []
        previews: list[AdminMergeSubPreview] = []
        for sub in subs:
            remnawave_uuid = getattr(sub, 'remnawave_uuid', None)
            devices_count: int | None = None
            devices: list[AdminMergeDeviceInfo] = []
            if remnawave_uuid:
                try:
                    async with _get_remnawave_api() as api:
                        data = await api.get_user_devices_all(remnawave_uuid)
                    raw_devices = data.get('devices', [])
                    devices_count = data.get('total', len(raw_devices))
                    for d in raw_devices:
                        devices.append(AdminMergeDeviceInfo(
                            hwid=d.get('hwid'),
                            app=d.get('app') or d.get('appName'),
                            platform=d.get('platform'),
                            last_seen=d.get('lastSeen') or d.get('last_seen'),
                        ))
                except Exception:
                    logger.warning(
                        'Failed to fetch devices for subscription in merge preview',
                        subscription_id=sub.id,
                        remnawave_uuid=remnawave_uuid,
                        exc_info=True,
                    )
                    # devices_count stays None, devices stays []
            tariff_name = None
            if getattr(sub, 'tariff', None):
                tariff_name = sub.tariff.name
            previews.append(AdminMergeSubPreview(
                subscription_id=sub.id,
                tariff_name=tariff_name,
                end_date=getattr(sub, 'end_date', None),
                status=sub.status,
                subscription_url=getattr(sub, 'subscription_url', None),
                subscription_crypto_link=getattr(sub, 'subscription_crypto_link', None),
                remnawave_short_uuid=getattr(sub, 'remnawave_short_uuid', None),
                devices_count=devices_count,
                devices=devices,
            ))
        return previews

    primary_subs = await _build_sub_previews(primary)
    secondary_subs = await _build_sub_previews(secondary)

    def _build_user_preview_admin(user: User, subs: list[AdminMergeSubPreview], refs: int) -> AdminMergeUserPreview:
        return AdminMergeUserPreview(
            id=user.id,
            username=getattr(user, 'username', None),
            first_name=getattr(user, 'first_name', None),
            email=getattr(user, 'email', None),
            telegram_id=getattr(user, 'telegram_id', None),
            auth_methods=compute_auth_methods(user),
            balance_kopeks=getattr(user, 'balance_kopeks', 0),
            referrals_count=refs,
            created_at=getattr(user, 'created_at', None),
            subscriptions=subs,
        )

    return AdminMergePreviewResponse(
        primary=_build_user_preview_admin(primary, primary_subs, primary_refs),
        secondary=_build_user_preview_admin(secondary, secondary_subs, secondary_refs),
    )
```

- [ ] **Step 3.5 — Run tests to confirm they pass**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
.venv/bin/python3 -m pytest tests/cabinet/test_admin_merge_preview_route.py -v 2>&1 | tail -20
```

Expected: `5 passed`

- [ ] **Step 3.6 — Syntax-check**

```bash
.venv/bin/python3 -m py_compile app/cabinet/routes/admin_user_linking.py && echo OK
```

Expected: `OK`

- [ ] **Step 3.7 — Full regression**

```bash
.venv/bin/python3 -m pytest tests/services/ tests/cabinet/ -v --tb=short 2>&1 | tail -20
```

- [ ] **Step 3.8 — Commit**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
git add app/cabinet/routes/admin_user_linking.py tests/cabinet/test_admin_merge_preview_route.py
git commit -m "feat(merge): add GET /cabinet/admin/users/merge/preview endpoint

Returns both users' base info + subscriptions list enriched with live device
counts and device list from RemnaWave (by each sub's remnawave_uuid).
Panel unavailability is handled gracefully: subscriptions are returned with
devices_count=null and empty devices list so the frontend can still show the
merge UI. Gated on users:edit permission."
```

---

### Task 4: Frontend — Types and API functions

**Files:**
- Modify: `src/types/index.ts`
- Modify: `src/api/adminUsers.ts`
- Create: `src/utils/adminMergeLogic.test.ts`

**Interfaces:**
- Produces:
  - `AdminMergeDeviceInfo`, `AdminMergeSubPreview`, `AdminMergeUserPreview`, `AdminMergePreviewResponse` in `src/types/index.ts`
  - `adminUsersApi.getMergePreview(primaryId: number, secondaryId: number): Promise<AdminMergePreviewResponse>`
  - `adminUsersApi.mergeUsers(primaryId: number, secondaryId: number, keepSubscriptionId?: number | null): Promise<{ success: boolean }>`

---

- [ ] **Step 4.1 — Write failing vitest**

Create `src/utils/adminMergeLogic.test.ts`:

```typescript
import { describe, it, expect } from 'vitest';
import type {
  AdminMergePreviewResponse,
  AdminMergeUserPreview,
  AdminMergeSubPreview,
  AdminMergeDeviceInfo,
} from '../types';

function buildMergePayload(
  primaryId: number,
  secondaryId: number,
  keepSubscriptionId: number | null,
): { primary_user_id: number; secondary_user_id: number; keep_subscription_id: number | null } {
  return {
    primary_user_id: primaryId,
    secondary_user_id: secondaryId,
    keep_subscription_id: keepSubscriptionId,
  };
}

function chooseKeptSub(
  preview: AdminMergePreviewResponse,
  subId: number | null,
): AdminMergeSubPreview | null {
  if (subId === null) return null;
  const allSubs = [
    ...preview.primary.subscriptions,
    ...preview.secondary.subscriptions,
  ];
  return allSubs.find((s) => s.subscription_id === subId) ?? null;
}

describe('buildMergePayload', () => {
  it('includes keep_subscription_id when provided', () => {
    const p = buildMergePayload(1, 2, 42);
    expect(p).toEqual({ primary_user_id: 1, secondary_user_id: 2, keep_subscription_id: 42 });
  });

  it('passes null when no sub selected', () => {
    const p = buildMergePayload(1, 2, null);
    expect(p.keep_subscription_id).toBeNull();
  });
});

describe('chooseKeptSub', () => {
  const sub1: AdminMergeSubPreview = {
    subscription_id: 10,
    tariff_name: 'Basic',
    end_date: '2026-12-01T00:00:00Z',
    status: 'active',
    subscription_url: 'https://link/a',
    subscription_crypto_link: null,
    remnawave_short_uuid: 'short-a',
    devices_count: 2,
    devices: [],
  };
  const sub2: AdminMergeSubPreview = {
    subscription_id: 20,
    tariff_name: 'Pro',
    end_date: '2027-01-01T00:00:00Z',
    status: 'active',
    subscription_url: 'https://link/b',
    subscription_crypto_link: null,
    remnawave_short_uuid: 'short-b',
    devices_count: 0,
    devices: [],
  };
  const user1: AdminMergeUserPreview = {
    id: 1, username: null, first_name: null, email: null, telegram_id: 111,
    auth_methods: ['telegram'], balance_kopeks: 0, referrals_count: 0,
    created_at: null, subscriptions: [sub1],
  };
  const user2: AdminMergeUserPreview = {
    id: 2, username: null, first_name: null, email: null, telegram_id: 222,
    auth_methods: ['telegram'], balance_kopeks: 0, referrals_count: 0,
    created_at: null, subscriptions: [sub2],
  };
  const preview: AdminMergePreviewResponse = { primary: user1, secondary: user2 };

  it('finds sub in primary', () => {
    expect(chooseKeptSub(preview, 10)?.subscription_id).toBe(10);
  });

  it('finds sub in secondary', () => {
    expect(chooseKeptSub(preview, 20)?.subscription_id).toBe(20);
  });

  it('returns null when subId is null', () => {
    expect(chooseKeptSub(preview, null)).toBeNull();
  });

  it('returns null for unknown id', () => {
    expect(chooseKeptSub(preview, 999)).toBeNull();
  });
});

describe('AdminMergeDeviceInfo type', () => {
  it('accepts all optional fields', () => {
    const d: AdminMergeDeviceInfo = {
      hwid: 'abc', app: 'SingBox', platform: 'iOS', last_seen: '2026-07-20T10:00:00Z',
    };
    expect(d.app).toBe('SingBox');
  });
});
```

- [ ] **Step 4.2 — Run vitest to confirm they fail**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx vitest run src/utils/adminMergeLogic.test.ts 2>&1 | tail -20
```

Expected: `FAILED` — `AdminMergePreviewResponse` etc. not exported from `../types`.

- [ ] **Step 4.3 — Add new types to `src/types/index.ts`**

After the existing `MergeResponse` interface (around line 872), append:

```typescript
// Admin Merge Preview (with device info, for users:edit flow)
export interface AdminMergeDeviceInfo {
  hwid: string | null;
  app: string | null;
  platform: string | null;
  last_seen: string | null;
}

export interface AdminMergeSubPreview {
  subscription_id: number;
  tariff_name: string | null;
  end_date: string | null;
  status: string;
  subscription_url: string | null;
  subscription_crypto_link: string | null;
  remnawave_short_uuid: string | null;
  devices_count: number | null;
  devices: AdminMergeDeviceInfo[];
}

export interface AdminMergeUserPreview {
  id: number;
  username: string | null;
  first_name: string | null;
  email: string | null;
  telegram_id: number | null;
  auth_methods: string[];
  balance_kopeks: number;
  referrals_count: number;
  created_at: string | null;
  subscriptions: AdminMergeSubPreview[];
}

export interface AdminMergePreviewResponse {
  primary: AdminMergeUserPreview;
  secondary: AdminMergeUserPreview;
}
```

- [ ] **Step 4.4 — Add API functions to `src/api/adminUsers.ts`**

After the existing `mergeUsers` function (around line 924), and before the closing `};`, add:

```typescript
  // Get merge preview (both users + subscriptions + live device counts)
  getMergePreview: async (
    primaryId: number,
    secondaryId: number,
  ): Promise<import('../types').AdminMergePreviewResponse> => {
    const response = await apiClient.get('/cabinet/admin/users/merge/preview', {
      params: { primary_user_id: primaryId, secondary_user_id: secondaryId },
    });
    return response.data;
  },
```

And update the existing `mergeUsers` function to accept `keepSubscriptionId`:

```typescript
  mergeUsers: async (
    primaryUserId: number,
    secondaryUserId: number,
    keepSubscriptionId?: number | null,
  ): Promise<{ success: boolean; transferred: Record<string, unknown> }> => {
    const response = await apiClient.post(`/cabinet/admin/users/merge`, {
      primary_user_id: primaryUserId,
      secondary_user_id: secondaryUserId,
      keep_subscription_id: keepSubscriptionId ?? null,
    });
    return response.data;
  },
```

- [ ] **Step 4.5 — Run vitest to confirm tests pass**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx vitest run src/utils/adminMergeLogic.test.ts 2>&1 | tail -20
```

Expected: `4 passed` (all tests in the file).

- [ ] **Step 4.6 — TypeScript check and build**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx tsc --noEmit 2>&1 | tail -20
npm run build 2>&1 | tail -10
```

Expected: no errors.

- [ ] **Step 4.7 — Full vitest suite**

```bash
npx vitest run 2>&1 | tail -10
```

Expected: all passing.

- [ ] **Step 4.8 — Commit**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
git add src/types/index.ts src/api/adminUsers.ts src/utils/adminMergeLogic.test.ts
git commit -m "feat(merge): add AdminMergePreviewResponse types + getMergePreview API, extend mergeUsers

New interfaces AdminMergeDeviceInfo / AdminMergeSubPreview /
AdminMergeUserPreview / AdminMergePreviewResponse added to types/index.ts.
adminUsersApi.getMergePreview fetches the new backend preview endpoint.
mergeUsers gains optional keepSubscriptionId parameter passed as
keep_subscription_id in the POST body."
```

---

### Task 5: Frontend — `AdminMergePanel` component

**Files:**
- Create: `src/components/admin/userDetail/AdminMergePanel.tsx`
- Modify: `src/locales/ru.json`
- Modify: `src/locales/en.json`

**Interfaces:**
- Consumes:
  - `adminUsersApi.getMergePreview(primaryId, secondaryId): Promise<AdminMergePreviewResponse>` from Task 4
  - `adminUsersApi.mergeUsers(primaryId, secondaryId, keepSubscriptionId?): Promise<{success:boolean}>` from Task 4
  - `AdminMergePreviewResponse`, `AdminMergeUserPreview`, `AdminMergeSubPreview` from `../../../types`
- Produces:
  - `<AdminMergePanel primaryUserId={number} onClose={() => void} onSuccess={() => void} />`

The component manages its own steps internally:
1. **Search step** — text input; on submit calls `adminUsersApi.getUsers({search})` to find secondary.
2. **Preview step** — calls `getMergePreview`; shows two-column comparison.
3. **Confirm button** — calls `mergeUsers` with chosen `keepSubscriptionId`.

---

- [ ] **Step 5.1 — Add locale keys**

In `src/locales/ru.json`, inside `admin.users.detail.linking`, add these keys (merge the object, do not replace the whole file):

```json
"mergeV2": "Объединить аккаунты (новый флоу)",
"mergeSearchPlaceholder": "Поиск по имени, email, TG ID...",
"mergeSearchLabel": "Найти второй аккаунт",
"mergeSearchButton": "Найти",
"mergePreviewTitle": "Сравнение аккаунтов",
"mergePrimaryAccountLabel": "Приоритетный аккаунт (выживет)",
"mergeSecondaryAccountLabel": "Второй аккаунт (будет удалён)",
"mergeChooseSurvivorLabel": "Приоритетный аккаунт",
"mergeChooseLinkLabel": "Сохранить ссылку подписки",
"mergeDevicesLabel": "Устройства",
"mergeDevicesNone": "нет",
"mergeDevicesUnavailable": "данные недоступны",
"mergeWarningV2": "Выбранный аккаунт выживает. Данные второго аккаунта переезжают к нему. Ссылка выбранной подписки сохраняется. Действие необратимо.",
"mergeConfirmV2": "Подтвердить объединение",
"mergeBackToSearch": "Назад к поиску",
"mergeNoSubscription": "нет подписки"
```

In `src/locales/en.json`, inside `admin.users.detail.linking`, add:

```json
"mergeV2": "Merge accounts (new flow)",
"mergeSearchPlaceholder": "Search by name, email, TG ID...",
"mergeSearchLabel": "Find second account",
"mergeSearchButton": "Search",
"mergePreviewTitle": "Account comparison",
"mergePrimaryAccountLabel": "Primary account (survives)",
"mergeSecondaryAccountLabel": "Secondary account (will be deleted)",
"mergeChooseSurvivorLabel": "Priority account",
"mergeChooseLinkLabel": "Keep subscription link",
"mergeDevicesLabel": "Devices",
"mergeDevicesNone": "none",
"mergeDevicesUnavailable": "data unavailable",
"mergeWarningV2": "The selected account survives. Data from the other account moves to it. The chosen subscription link is preserved. This action is irreversible.",
"mergeConfirmV2": "Confirm merge",
"mergeBackToSearch": "Back to search",
"mergeNoSubscription": "no subscription"
```

**How to add keys without overwriting the file:** Use `python3` to merge:

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
python3 - <<'EOF'
import json, copy

NEW_RU = {
  "mergeV2": "Объединить аккаунты (новый флоу)",
  "mergeSearchPlaceholder": "Поиск по имени, email, TG ID...",
  "mergeSearchLabel": "Найти второй аккаунт",
  "mergeSearchButton": "Найти",
  "mergePreviewTitle": "Сравнение аккаунтов",
  "mergePrimaryAccountLabel": "Приоритетный аккаунт (выживет)",
  "mergeSecondaryAccountLabel": "Второй аккаунт (будет удалён)",
  "mergeChooseSurvivorLabel": "Приоритетный аккаунт",
  "mergeChooseLinkLabel": "Сохранить ссылку подписки",
  "mergeDevicesLabel": "Устройства",
  "mergeDevicesNone": "нет",
  "mergeDevicesUnavailable": "данные недоступны",
  "mergeWarningV2": "Выбранный аккаунт выживает. Данные второго аккаунта переезжают к нему. Ссылка выбранной подписки сохраняется. Действие необратимо.",
  "mergeConfirmV2": "Подтвердить объединение",
  "mergeBackToSearch": "Назад к поиску",
  "mergeNoSubscription": "нет подписки"
}

NEW_EN = {
  "mergeV2": "Merge accounts (new flow)",
  "mergeSearchPlaceholder": "Search by name, email, TG ID...",
  "mergeSearchLabel": "Find second account",
  "mergeSearchButton": "Search",
  "mergePreviewTitle": "Account comparison",
  "mergePrimaryAccountLabel": "Primary account (survives)",
  "mergeSecondaryAccountLabel": "Secondary account (will be deleted)",
  "mergeChooseSurvivorLabel": "Priority account",
  "mergeChooseLinkLabel": "Keep subscription link",
  "mergeDevicesLabel": "Devices",
  "mergeDevicesNone": "none",
  "mergeDevicesUnavailable": "data unavailable",
  "mergeWarningV2": "The selected account survives. Data from the other account moves to it. The chosen subscription link is preserved. This action is irreversible.",
  "mergeConfirmV2": "Confirm merge",
  "mergeBackToSearch": "Back to search",
  "mergeNoSubscription": "no subscription"
}

for fname, new_keys in [('src/locales/ru.json', NEW_RU), ('src/locales/en.json', NEW_EN)]:
    with open(fname) as f:
        data = json.load(f)
    data['admin']['users']['detail']['linking'].update(new_keys)
    with open(fname, 'w') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f'Updated {fname}')
EOF
```

- [ ] **Step 5.2 — Verify locale integrity test**

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
.venv/bin/python3 -m pytest tests/test_locale_integrity.py -v 2>&1 | tail -10
```

Expected: passing (this test checks that en and ru keys match — both files got the same keys).

- [ ] **Step 5.3 — Create `AdminMergePanel.tsx`**

Create `src/components/admin/userDetail/AdminMergePanel.tsx`:

```tsx
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { adminUsersApi, type UserListItem } from '../../../api/adminUsers';
import type {
  AdminMergePreviewResponse,
  AdminMergeSubPreview,
  AdminMergeDeviceInfo,
} from '../../../types';
import { useNotify } from '../../../platform/hooks/useNotify';

interface Props {
  primaryUserId: number;
  onClose: () => void;
  onSuccess: () => void;
}

type Step = 'search' | 'preview' | 'confirming';

export function AdminMergePanel({ primaryUserId, onClose, onSuccess }: Props) {
  const { t } = useTranslation();
  const notify = useNotify();

  const [step, setStep] = useState<Step>('search');
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<UserListItem[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [secondaryUserId, setSecondaryUserId] = useState<number | null>(null);

  const [preview, setPreview] = useState<AdminMergePreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  // Which user survives: primaryUserId or secondaryUserId
  const [survivorId, setSurvivorId] = useState<number>(primaryUserId);
  // Which subscription's link to keep (null = default by later end-date)
  const [keepSubId, setKeepSubId] = useState<number | null>(null);

  const [mergeLoading, setMergeLoading] = useState(false);

  const handleSearch = async () => {
    if (!searchQuery.trim()) return;
    setSearchLoading(true);
    try {
      const res = await adminUsersApi.getUsers({ search: searchQuery.trim(), limit: 10 });
      const filtered = res.users.filter((u) => u.id !== primaryUserId);
      setSearchResults(filtered);
    } catch {
      notify.error('Search failed');
    } finally {
      setSearchLoading(false);
    }
  };

  const handleSelectSecondary = async (user: UserListItem) => {
    setSecondaryUserId(user.id);
    setPreviewLoading(true);
    setStep('preview');
    try {
      const data = await adminUsersApi.getMergePreview(primaryUserId, user.id);
      setPreview(data);
      setSurvivorId(primaryUserId);
      setKeepSubId(null);
    } catch {
      notify.error('Failed to load preview');
      setStep('search');
      setSecondaryUserId(null);
    } finally {
      setPreviewLoading(false);
    }
  };

  const handleConfirm = async () => {
    if (!secondaryUserId || !preview) return;
    // Determine primary/secondary for the API based on survivor choice
    const [apiPrimary, apiSecondary] =
      survivorId === primaryUserId
        ? [primaryUserId, secondaryUserId]
        : [secondaryUserId, primaryUserId];

    setMergeLoading(true);
    try {
      await adminUsersApi.mergeUsers(apiPrimary, apiSecondary, keepSubId);
      notify.success(t('admin.users.detail.linking.success.merged'));
      onSuccess();
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: string } } };
      notify.error(axiosErr?.response?.data?.detail || 'Merge failed');
    } finally {
      setMergeLoading(false);
    }
  };

  const renderDevices = (devices: AdminMergeDeviceInfo[], count: number | null) => {
    if (count === null)
      return (
        <span className="text-xs text-dark-500 italic">
          {t('admin.users.detail.linking.mergeDevicesUnavailable')}
        </span>
      );
    if (count === 0)
      return (
        <span className="text-xs text-dark-500">
          {t('admin.users.detail.linking.mergeDevicesNone')}
        </span>
      );
    return (
      <div className="flex flex-wrap gap-1 mt-1">
        {devices.map((d, i) => (
          <span key={i} className="rounded bg-dark-700 px-1.5 py-0.5 text-xs text-dark-300">
            {d.app || d.platform || d.hwid || '?'}
          </span>
        ))}
        {count > devices.length && (
          <span className="text-xs text-dark-500">+{count - devices.length}</span>
        )}
      </div>
    );
  };

  const renderSubCard = (
    sub: AdminMergeSubPreview,
    isSelected: boolean,
    onSelect: () => void,
    showRadio: boolean,
  ) => (
    <div
      key={sub.subscription_id}
      className={`rounded-lg border p-3 text-xs ${
        isSelected
          ? 'border-accent-500/60 bg-accent-500/10'
          : 'border-dark-600 bg-dark-800/50'
      }`}
    >
      <div className="flex items-center justify-between mb-1">
        <span className="font-medium text-dark-100">
          {sub.tariff_name || sub.status}
        </span>
        {showRadio && (
          <label className="flex items-center gap-1 cursor-pointer text-dark-300">
            <input
              type="radio"
              name="keep-sub"
              checked={isSelected}
              onChange={onSelect}
              className="accent-accent-500"
            />
            {t('admin.users.detail.linking.mergeChooseLinkLabel')}
          </label>
        )}
      </div>
      {sub.end_date && (
        <div className="text-dark-400">
          {new Date(sub.end_date).toLocaleDateString()}
        </div>
      )}
      {sub.subscription_url && (
        <div className="mt-1 truncate text-dark-500" title={sub.subscription_url}>
          {sub.subscription_url.slice(0, 48)}…
        </div>
      )}
      <div className="mt-2">
        <span className="text-dark-400">
          {t('admin.users.detail.linking.mergeDevicesLabel')}:{' '}
        </span>
        {renderDevices(sub.devices, sub.devices_count)}
      </div>
    </div>
  );

  const renderUserColumn = (
    userPreview: AdminMergePreviewResponse['primary'],
    isCurrentPrimary: boolean,
    label: string,
    subs: AdminMergeSubPreview[],
  ) => {
    const isSurvivor = userPreview.id === survivorId;
    return (
      <div
        className={`flex-1 rounded-xl border p-4 ${
          isSurvivor ? 'border-accent-500/50 bg-accent-500/5' : 'border-dark-600 bg-dark-800/30'
        }`}
      >
        <div className="mb-2 flex items-center justify-between">
          <span className="text-xs font-semibold uppercase tracking-wide text-dark-400">
            {label}
          </span>
          <label className="flex items-center gap-1 cursor-pointer text-xs text-dark-300">
            <input
              type="radio"
              name="survivor"
              checked={isSurvivor}
              onChange={() => setSurvivorId(userPreview.id)}
              className="accent-accent-500"
            />
            {t('admin.users.detail.linking.mergeChooseSurvivorLabel')}
          </label>
        </div>
        <div className="text-sm font-semibold text-dark-100">
          {userPreview.first_name || userPreview.username || `#${userPreview.id}`}
        </div>
        <div className="text-xs text-dark-500 mb-1">#{userPreview.id}</div>
        {userPreview.email && (
          <div className="text-xs text-dark-400">{userPreview.email}</div>
        )}
        <div className="text-xs text-dark-400">
          {userPreview.auth_methods.join(', ')}
        </div>
        <div className="mt-3 space-y-2">
          {subs.length === 0 ? (
            <div className="text-xs text-dark-500 italic">
              {t('admin.users.detail.linking.mergeNoSubscription')}
            </div>
          ) : (
            subs.map((sub) =>
              renderSubCard(
                sub,
                keepSubId === sub.subscription_id,
                () => setKeepSubId(sub.subscription_id),
                true,
              ),
            )
          )}
        </div>
      </div>
    );
  };

  // ─── SEARCH STEP ───────────────────────────────────────────────────────────
  if (step === 'search') {
    return (
      <div className="space-y-3">
        <div>
          <label className="mb-1 block text-xs text-dark-400">
            {t('admin.users.detail.linking.mergeSearchLabel')}
          </label>
          <div className="flex gap-2">
            <input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
              placeholder={t('admin.users.detail.linking.mergeSearchPlaceholder')}
              className="input flex-1"
              autoFocus
            />
            <button
              onClick={handleSearch}
              disabled={searchLoading || !searchQuery.trim()}
              className="rounded-lg bg-accent-500/20 px-3 py-2 text-sm font-medium text-accent-300 hover:bg-accent-500/30 disabled:opacity-50"
            >
              {searchLoading ? '…' : t('admin.users.detail.linking.mergeSearchButton')}
            </button>
          </div>
        </div>

        {searchResults.map((u) => (
          <button
            key={u.id}
            onClick={() => handleSelectSecondary(u)}
            className="w-full rounded-lg border border-dark-600 bg-dark-800/40 p-3 text-left hover:border-dark-500 hover:bg-dark-700/40"
          >
            <div className="text-sm font-medium text-dark-100">
              {u.full_name || u.username || `#${u.id}`}
            </div>
            <div className="text-xs text-dark-500">
              #{u.id}
              {u.subscription_end_date &&
                ` · до ${new Date(u.subscription_end_date).toLocaleDateString()}`}
            </div>
          </button>
        ))}

        <button
          onClick={onClose}
          className="w-full rounded-lg bg-dark-700 py-2 text-sm text-dark-400 hover:bg-dark-600"
        >
          {t('common.cancel')}
        </button>
      </div>
    );
  }

  // ─── PREVIEW STEP ──────────────────────────────────────────────────────────
  if (step === 'preview') {
    if (previewLoading || !preview) {
      return (
        <div className="flex items-center justify-center py-10 text-dark-400">
          {t('common.loading')}
        </div>
      );
    }

    const primaryPreview = preview.primary;
    const secondaryPreview = preview.secondary;

    return (
      <div className="space-y-4">
        <div className="text-sm font-semibold text-dark-200">
          {t('admin.users.detail.linking.mergePreviewTitle')}
        </div>

        <div className="flex gap-3">
          {renderUserColumn(
            primaryPreview,
            true,
            t('admin.users.detail.linking.mergePrimaryAccountLabel'),
            primaryPreview.subscriptions,
          )}
          {renderUserColumn(
            secondaryPreview,
            false,
            t('admin.users.detail.linking.mergeSecondaryAccountLabel'),
            secondaryPreview.subscriptions,
          )}
        </div>

        <div className="rounded-lg border border-error-500/30 bg-error-500/10 p-3 text-xs text-error-300">
          {t('admin.users.detail.linking.mergeWarningV2')}
        </div>

        <div className="flex gap-2">
          <button
            onClick={() => {
              setStep('search');
              setPreview(null);
              setSecondaryUserId(null);
              setKeepSubId(null);
            }}
            className="flex-1 rounded-lg bg-dark-700 py-2 text-sm text-dark-400 hover:bg-dark-600"
          >
            {t('admin.users.detail.linking.mergeBackToSearch')}
          </button>
          <button
            onClick={handleConfirm}
            disabled={mergeLoading}
            className="flex-1 rounded-lg bg-error-500 py-2 text-sm font-medium text-white hover:bg-error-600 disabled:opacity-50"
          >
            {mergeLoading
              ? t('common.loading')
              : t('admin.users.detail.linking.mergeConfirmV2')}
          </button>
        </div>
      </div>
    );
  }

  return null;
}
```

- [ ] **Step 5.4 — TypeScript-check**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx tsc --noEmit 2>&1 | tail -20
```

Expected: no errors.

- [ ] **Step 5.5 — Build**

```bash
npm run build 2>&1 | tail -10
```

Expected: build succeeds.

- [ ] **Step 5.6 — Full vitest run**

```bash
npx vitest run 2>&1 | tail -10
```

Expected: all passing.

- [ ] **Step 5.7 — Commit**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
git add src/components/admin/userDetail/AdminMergePanel.tsx \
        src/locales/ru.json \
        src/locales/en.json
git commit -m "feat(merge-panel): add AdminMergePanel component with search + preview + confirm steps

Three-step flow: (1) search for second account by name/email/TG;
(2) side-by-side preview showing subscriptions with live device bindings
from RemnaWave; (3) confirm with survivor radio and keep-link radio.
Locale keys added to ru.json and en.json only."
```

---

### Task 6: Frontend — Wire `AdminMergePanel` into `AdminUserDetail`

**Files:**
- Modify: `src/pages/AdminUserDetail.tsx`

**Interfaces:**
- Consumes: `<AdminMergePanel primaryUserId={number} onClose={() => void} onSuccess={() => void} />` from Task 5
- Produces: the "Объединить" button in the Info tab opens a `Dialog` containing `AdminMergePanel`

---

- [ ] **Step 6.1 — Add import and new state variables**

In `src/pages/AdminUserDetail.tsx`, add the import near the other admin component imports (around line 40):

```typescript
import { AdminMergePanel } from '../components/admin/userDetail/AdminMergePanel';
```

Find the "Merge users modal" comment block (around line 363):

```typescript
  // Merge users modal
  const [showMergeModal, setShowMergeModal] = useState(false);
  const [mergeSecondaryId, setMergeSecondaryId] = useState('');
  const [mergeLoading, setMergeLoading] = useState(false);
  const [mergeConfirmed, setMergeConfirmed] = useState(false);
```

Add one new state variable below it (keep the old ones — the old modal still works as fallback):

```typescript
  const [showNewMergePanel, setShowNewMergePanel] = useState(false);
```

- [ ] **Step 6.2 — Replace the "Объединить" button with the new panel trigger**

Find the existing button at ~line 2060:

```tsx
                {hasPermission('users:edit') && (
                  <button
                    onClick={() => {
                      setMergeConfirmed(false);
                      setShowMergeModal(true);
                    }}
                    disabled={actionLoading}
                    className="col-span-2 rounded-lg bg-violet-500/15 px-3 py-2 text-sm font-medium text-violet-400 transition-all hover:bg-violet-500/25 disabled:opacity-50"
                  >
                    {t('admin.users.detail.linking.merge')}
                  </button>
                )}
```

Replace with:

```tsx
                {hasPermission('users:edit') && (
                  <button
                    onClick={() => setShowNewMergePanel(true)}
                    disabled={actionLoading}
                    className="col-span-2 rounded-lg bg-violet-500/15 px-3 py-2 text-sm font-medium text-violet-400 transition-all hover:bg-violet-500/25 disabled:opacity-50"
                  >
                    {t('admin.users.detail.linking.merge')}
                  </button>
                )}
```

- [ ] **Step 6.3 — Add the new merge Dialog using `AdminMergePanel`**

Find the old merge `Dialog` block in the JSX (starts at ~line 3352 with `<Dialog open={showMergeModal}`). Leave it in place. **Before** the closing `</div>` of the return statement (after all existing Dialogs), add a new Dialog:

```tsx
      {/* New Admin Merge Panel — full preview with subscription link choice */}
      <Dialog
        open={showNewMergePanel && !!userId && !!user}
        onOpenChange={(o) => {
          if (!o) setShowNewMergePanel(false);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t('admin.users.detail.linking.merge')}</DialogTitle>
          </DialogHeader>
          {showNewMergePanel && userId && (
            <AdminMergePanel
              primaryUserId={userId}
              onClose={() => setShowNewMergePanel(false)}
              onSuccess={() => {
                setShowNewMergePanel(false);
                loadUser();
              }}
            />
          )}
        </DialogContent>
      </Dialog>
```

Note: `loadUser` is the existing function that re-fetches the user card.

- [ ] **Step 6.4 — TypeScript check**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx tsc --noEmit 2>&1 | tail -20
```

Expected: no errors.

- [ ] **Step 6.5 — Build**

```bash
npm run build 2>&1 | tail -10
```

Expected: build succeeds.

- [ ] **Step 6.6 — Full vitest run**

```bash
npx vitest run 2>&1 | tail -10
```

Expected: all passing.

- [ ] **Step 6.7 — Commit**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
git add src/pages/AdminUserDetail.tsx
git commit -m "feat(merge-panel): wire AdminMergePanel into AdminUserDetail

The 'Объединить' button in the Info tab now opens AdminMergePanel inside
a Dialog. The panel manages its own search/preview/confirm steps.
The old ID-input modal remains in the page for fallback but is no longer
the primary entry point."
```

---

## Self-Review

### 1. Spec Coverage

| Spec requirement | Covered in |
|---|---|
| `get_user_hwid_devices` method in remnawave_api.py | **Resolved:** `get_user_devices_all(uuid)` already exists in `remnawave_api.py` (lines 1368–1392) and does exactly what the spec describes. The plan reuses it rather than adding a duplicate. |
| `keep_subscription_id` in `_handle_subscription_merge` / `execute_merge` | Task 1 |
| Single- and multi-tariff branches both honoring `keep_subscription_id` | Task 1 (both branches patched) |
| `AdminMergeUsersRequest.keep_subscription_id: int | None = None` | Task 2 |
| 400 when `keep_subscription_id` doesn't belong to either user | Task 2 |
| Preview endpoint `GET /cabinet/admin/users/merge/preview` | Task 3 |
| `AdminMergePreviewResponse` with device counts | Task 3 |
| Panel unavailability → `devices_count=null`, no failure | Task 3 |
| Frontend `getMergePreview` + extended `mergeUsers` | Task 4 |
| Frontend types `AdminMergePreviewResponse` etc. | Task 4 |
| "Объединить" button gated on `users:edit` | Task 6 (button already gated; new panel respects it) |
| Search step for second account | Task 5 (`AdminMergePanel` step `search`) |
| Comparison panel with subscriptions + device bindings | Task 5 (`AdminMergePanel` step `preview`) |
| Radio for survivor + radio for which subscription link | Task 5 (`survivorId` + `keepSubId` state) |
| Warning banner | Task 5 (`mergeWarningV2`) |
| Confirm → `mergeUsers(primary, secondary, keep_subscription_id)` | Task 5 `handleConfirm` |
| Refetch on success | Task 6 (`loadUser()` in `onSuccess`) |
| Locale keys only in `ru.json` / `en.json` | Task 5, step 5.1 |
| `keep_subscription_id=None` → default behavior unchanged | Task 1 tests + `test_keep_none_preserves_original_logic` |
| Kept sub's `remnawave_short_uuid` / `subscription_url` unchanged | Task 1 `test_keep_early_sub_preserved_url` asserts this |

### 2. Placeholder Scan

No "TBD", "implement later", "fill in details", or "similar to Task N" phrases. All code blocks are complete.

### 3. Type/Name Consistency

- `AdminMergeDeviceInfo` — defined in `src/types/index.ts` (Task 4), used in `AdminMergePanel.tsx` (Task 5). Consistent.
- `AdminMergeSubPreview` — defined in `src/types/index.ts` (Task 4), used in `AdminMergePanel.tsx` (Task 5). `.subscription_id` (not `.id`) — consistent across types and vitest.
- `AdminMergeUserPreview` — defined Task 4, used Task 5 as `AdminMergePreviewResponse['primary']`.
- `AdminMergePreviewResponse` — defined Task 4, returned by `getMergePreview` in `adminUsers.ts`, used in `AdminMergePanel`.
- Backend Pydantic: `AdminMergeSubPreview.subscription_id`, `AdminMergeUserPreview`, `AdminMergePreviewResponse` — consistent between route handler (Task 3) and tests (Task 3).
- `_handle_subscription_merge(db, primary, secondary, deferred_remnawave_deletions, keep_subscription_id=None)` — new signature defined in Task 1, used in `execute_merge` (also Task 1), tested in Task 1.
- `execute_merge(..., keep_subscription_id: int | None = None)` — defined Task 1, called in `admin_merge_users` (Task 2 adds the argument).
- `admin_merge_preview` — function name used in tests (Task 3) matches the function defined in Task 3.
- `_count_active_referrals` / `_get_remnawave_api` / `compute_auth_methods` imported in Task 3 from `account_merge_service` where they are defined (confirmed by reading the source).
- `get_user_devices_all(remnawave_uuid)` — method that already exists on `RemnaWaveAPI`; called in Task 3 preview handler. Consistent.

### Ambiguities Resolved

1. **`get_user_hwid_devices` vs existing methods** — The prompt asked to add `get_user_hwid_devices(uuid)`, but `RemnaWaveAPI` already has both `get_user_devices(uuid)` (single page) and `get_user_devices_all(uuid)` (paginated, handles 404). The plan reuses `get_user_devices_all` in the preview endpoint rather than creating a third identical method. This avoids code duplication while satisfying the spec's intent.

2. **`adminUsersApi.getMergePreview` import collision** — The frontend already has `MergePreviewResponse` (for user-initiated merge) and `MergeAccountPreview`. The new admin preview uses distinct type names (`AdminMergePreviewResponse`, `AdminMergeUserPreview`, `AdminMergeSubPreview`, `AdminMergeDeviceInfo`) to avoid collision.

3. **`survivorId` → `primary_user_id` mapping** — The spec says the admin picks a "priority account" (survivor). The API `POST /merge` always treats `primary_user_id` as the survivor. The `AdminMergePanel.handleConfirm` swaps primary/secondary as needed based on the `survivorId` radio choice.

4. **Multi-tariff `keep_subscription_id` scope** — The spec says the rule applies to "the chosen pair" in multi-tariff mode. The implementation applies `keep_subscription_id` only when it matches one of the two conflicting subscriptions in the same-tariff-id block; non-overlapping subscriptions are transferred as-is. This matches the spec's "остальные подписки просто переносятся" statement.
