# Account Merge — Choose Survivor & Subscription Combine (Subproject A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user choose which account survives a merge, combine both active subscriptions by extending end-dates (instead of deleting one), guarantee every login method reaches the survivor, and surface referral counts + a recommendation badge in the preview cards.

**Architecture:** The backend receives `keep_account: int` in the merge request; the execute handler swaps primary/secondary roles when the initiator picks the other account, then calls the existing `execute_merge()` unchanged. Subscription combining happens entirely inside `_handle_subscription_merge()` — a new helper `_combine_subscription_end_dates()` that extends the winner's `end_date`, writes a `SubscriptionEvent` row, then removes the loser's RemnaWave user (deferred). The preview builder `_build_user_preview()` gains a DB query for `referrals_count` and a pure-function `_compute_recommended()`. The frontend replaces the subscription radio with a survivor radio per card, adds new fields, and updates locale keys.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async, Pydantic v2 (backend); React 18, TypeScript, i18next, Tanstack Query, Vitest (frontend). SQLite + aiosqlite for backend unit tests; in-memory SimpleNamespace mocks for service-layer tests (existing pattern).

## Global Constraints

- Bot tests: `.venv/bin/python3 -m pytest <test-path> -v` (NOT bare `python3` — system python3 is 3.9 and lacks `datetime.UTC`)
- Frontend must pass `npx tsc --noEmit`, `npm run build`, and `npx vitest run` before any commit
- Never commit `.env` — repo is public
- No `Co-Authored-By:` trailer in any commit message
- Commit messages: descriptive title + body (what & why)
- Locale files: touch only `src/locales/ru.json` and `src/locales/en.json` — never `fa.json` or `zh.json`
- Backend is a fork — do NOT restructure upstream files; modify only targeted sections

---

## File Map

| File | Action | What changes |
|---|---|---|
| `app/services/account_merge_service.py` | Modify | Add `_combine_subscription_end_dates()`, update `_handle_subscription_merge()` (both branches), update `_build_user_preview()` + `get_merge_preview()`, add `_compute_recommended()`, remove `keep_subscription_from` param from `execute_merge()` |
| `app/cabinet/routes/account_linking.py` | Modify | `MergeRequest`: swap field; `MergePreviewUser`: add fields; execute handler: role-swap + return survivor tokens |
| `tests/services/test_account_merge_service.py` | Modify | Add tests for combine, role-swap, single-profile invariant, preview fields |
| `tests/services/test_merge_subscription_combine.py` | Create | Focused subscription-combine unit tests |
| `tests/services/test_merge_invariant.py` | Create | Single-profile regression tests (T3) |
| `src/types/index.ts` | Modify | Add `referrals_count`, `recommended` to `MergeAccountPreview`; change `executeMerge` request type |
| `src/api/auth.ts` | Modify | Change `executeMerge` signature to `keep_account: number` |
| `src/pages/MergeAccounts.tsx` | Modify | New UI: survivor radio per card, recommendation badge, warning banner, combined end-date |
| `src/locales/ru.json` | Modify | Add new keys, remove obsolete keys |
| `src/locales/en.json` | Modify | Add new keys, remove obsolete keys |

---

### Task 1: `MergeRequest.keep_account` + role-swap in execute handler

**Files:**
- Modify: `app/cabinet/routes/account_linking.py:183-197` (schemas) and `:928-1068` (execute handler)
- Modify: `tests/services/test_account_merge_service.py`

**Interfaces:**
- Consumes: nothing new — uses existing `execute_merge(db, primary_user_id, secondary_user_id, ...)` signature
- Produces:
  - `MergeRequest` Pydantic model with field `keep_account: int` (replaces `keep_subscription_from`)
  - Execute handler passes `(survivor_id, absorbed_id)` to `execute_merge()` and re-fetches `survivor_id` for token generation
  - `execute_merge()` signature loses `keep_subscription_from` parameter (Task 2 removes the body logic; Task 1 just removes it from the call site and adds `keep_account` validation)

- [ ] **Step 1.1: Write failing tests for MergeRequest validation and role-swap**

In `tests/services/test_account_merge_service.py`, add a new test class at the bottom:

```python
# ---------------------------------------------------------------------------
# Role-swap: keep_account determines survivor
# ---------------------------------------------------------------------------

class TestRoleSwapLogic:
    """Tests the handler-level role-swap logic (pure function extracted for testability)."""

    def test_keep_primary_no_swap(self):
        """When keep_account == primary_user_id, no swap needed."""
        primary_id, secondary_id = 10, 20
        keep = primary_id
        survivor = keep if keep == primary_id else secondary_id
        absorbed = secondary_id if survivor == primary_id else primary_id
        assert survivor == 10
        assert absorbed == 20

    def test_keep_secondary_swaps_roles(self):
        """When keep_account == secondary_user_id, roles are swapped."""
        primary_id, secondary_id = 10, 20
        keep = secondary_id
        survivor = keep  # secondary becomes primary
        absorbed = primary_id  # primary becomes secondary
        assert survivor == 20
        assert absorbed == 10

    def test_keep_unknown_id_rejected(self):
        """keep_account not in {primary_id, secondary_id} must be rejected."""
        primary_id, secondary_id = 10, 20
        keep = 99
        assert keep not in {primary_id, secondary_id}
```

Run: `.venv/bin/python3 -m pytest tests/services/test_account_merge_service.py::TestRoleSwapLogic -v`
Expected: PASS (pure logic tests, no imports needed yet)

- [ ] **Step 1.2: Remove `keep_subscription_from` from `MergeRequest` and add `keep_account`**

In `app/cabinet/routes/account_linking.py`, replace lines 189-191:

```python
# BEFORE:
class MergeRequest(BaseModel):
    keep_subscription_from: int = Field(..., description='User ID whose subscription to keep')

# AFTER:
class MergeRequest(BaseModel):
    keep_account: int = Field(..., description='User ID of the account that should survive (become primary)')
```

- [ ] **Step 1.3: Update execute handler validation and role-swap**

In `app/cabinet/routes/account_linking.py`, replace lines 964-1068 (the body of `execute_merge_endpoint` after the token consume):

```python
    primary_user_id: int = consumed['primary_user_id']
    secondary_user_id: int = consumed['secondary_user_id']
    provider: str = consumed.get('provider', '')
    provider_id: str = consumed.get('provider_id', '')

    # SECURITY: bind execution to the authenticated initiator (the primary). A
    # leaked token alone must not let a third party run the merge.
    if user.id != primary_user_id:
        await restore_merge_token(merge_token, consumed)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='This merge can only be completed by the account that started it.',
        )

    # 2. Validate keep_account — must be one of the two ids in the token
    if request.keep_account not in (primary_user_id, secondary_user_id):
        await restore_merge_token(merge_token, consumed)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='keep_account must be one of the two user IDs being merged',
        )

    # Role-swap: the chosen account plays 'primary' (survivor) in execute_merge.
    # If the user chose the secondary, we swap roles so the secondary is absorbed-into
    # and the initiator is absorbed.
    if request.keep_account == primary_user_id:
        survivor_id = primary_user_id
        absorbed_id = secondary_user_id
    else:
        survivor_id = secondary_user_id
        absorbed_id = primary_user_id

    # 3. Execute merge (survivor plays 'primary' role).
    deferred_deletions: list[str] = []
    try:
        await execute_merge(
            db=db,
            primary_user_id=survivor_id,
            secondary_user_id=absorbed_id,
            provider=provider,
            provider_id=provider_id,
            deferred_remnawave_deletions=deferred_deletions,
        )
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        logger.warning('Merge execution skipped (user already merged/deleted)', reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Account merge cannot be completed. The accounts may have already been merged or deleted.',
        ) from exc
    except Exception as exc:
        await db.rollback()
        from sqlalchemy.exc import IntegrityError
        if not isinstance(exc, IntegrityError):
            await restore_merge_token(merge_token, consumed)
        logger.exception('Merge execution failed')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Account merge failed due to an internal error',
        ) from exc

    # Commit succeeded — now drop the discarded subscription's panel user.
    await flush_remnawave_deletions(deferred_deletions)

    # 4. Re-fetch the SURVIVOR with full relationships for auth response
    merged_user = await get_user_by_id(db, survivor_id)
    if not merged_user:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Failed to load merged user',
        )

    # BUG-7 fix: Resync merged user's subscriptions with RemnaWave panel
    try:
        from app.services.remnawave_resync_service import resync_user_subscriptions_with_panel
        resync_result = await resync_user_subscriptions_with_panel(db, merged_user)
        logger.info(
            'Post-merge resync completed',
            survivor_id=survivor_id,
            absorbed_id=absorbed_id,
            synced=resync_result['synced'],
            failed=resync_result['failed'],
        )
    except Exception as resync_error:
        logger.error('Post-merge resync failed (non-fatal)', survivor_id=survivor_id, error=resync_error)

    # 5. Create auth tokens for the SURVIVOR
    try:
        auth_response = await _create_auth_response(merged_user, db)
        await _store_refresh_token(db, merged_user.id, auth_response.refresh_token, device_info='merge')
    except Exception as exc:
        logger.exception('Failed to create auth tokens after merge')
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Merge succeeded but failed to create new session',
        ) from exc

    logger.info(
        'Account merge completed successfully',
        survivor_id=survivor_id,
        absorbed_id=absorbed_id,
        keep_account=request.keep_account,
        provider=provider,
    )

    return MergeResponse(
        success=True,
        access_token=auth_response.access_token,
        refresh_token=auth_response.refresh_token,
        user=_user_to_response(merged_user),
    )
```

- [ ] **Step 1.4: Remove `keep_subscription_from` parameter from `execute_merge()` signature**

In `app/services/account_merge_service.py`, change the function signature at line 545:

```python
# BEFORE:
async def execute_merge(
    db: AsyncSession,
    primary_user_id: int,
    secondary_user_id: int,
    keep_subscription_from: Literal['primary', 'secondary'] = 'primary',
    provider: str | None = None,
    provider_id: str | None = None,
    deferred_remnawave_deletions: list[str] | None = None,
) -> User:

# AFTER:
async def execute_merge(
    db: AsyncSession,
    primary_user_id: int,
    secondary_user_id: int,
    provider: str | None = None,
    provider_id: str | None = None,
    deferred_remnawave_deletions: list[str] | None = None,
) -> User:
```

Also remove the early validation guard at lines 572-573:
```python
# REMOVE these two lines:
    if keep_subscription_from not in ('primary', 'secondary'):
        raise ValueError("keep_subscription_from должен быть 'primary' или 'secondary'")
```

And the call to `_handle_subscription_merge` at line 703 — remove `keep_subscription_from` arg:
```python
# BEFORE:
    await _handle_subscription_merge(db, primary, secondary, keep_subscription_from, pending_remnawave_deletions)

# AFTER:
    await _handle_subscription_merge(db, primary, secondary, pending_remnawave_deletions)
```

And remove `from typing import Literal` if it's used nowhere else (check other usages first):
```python
# Check:
grep -n "Literal" app/services/account_merge_service.py
# If only was used in keep_subscription_from, remove it from the import line.
```

Also update the `_handle_subscription_merge` signature:
```python
# BEFORE:
async def _handle_subscription_merge(
    db: AsyncSession,
    primary: User,
    secondary: User,
    keep_subscription_from: Literal['primary', 'secondary'],
    deferred_remnawave_deletions: list[str],
) -> None:

# AFTER (Task 2 will implement the body — for now remove the unused param):
async def _handle_subscription_merge(
    db: AsyncSession,
    primary: User,
    secondary: User,
    deferred_remnawave_deletions: list[str],
) -> None:
```

- [ ] **Step 1.5: Update the existing test that calls `execute_merge` with `keep_subscription_from`**

In `tests/services/test_account_merge_service.py`, find all calls to `execute_merge(..., keep_subscription_from='primary')` and `execute_merge(..., keep_subscription_from='secondary')` and remove the kwarg (the signature no longer has it). The tests `test_both_have_subscription_keep_primary` and `test_both_have_subscription_keep_secondary` also assert behavior that changes in Task 2; for now just remove the kwarg and update the assertions to reflect the new combine behavior (Task 2 will add proper assertions — in this task just remove the removed kwarg so import-time checks pass).

Also update `test_invalid_keep_subscription_from_raises` — that test no longer applies. Replace it:
```python
    async def test_invalid_keep_account_in_endpoint_rejected(self):
        """keep_account outside the pair must be rejected at endpoint validation level.
        execute_merge itself no longer validates this — endpoint does."""
        # This is now endpoint-layer validation; service-level validation
        # only checks same-id, not-found, and deleted.
        pass  # Covered by endpoint integration test in future tasks
```

- [ ] **Step 1.6: Run py_compile check on modified files**

```bash
.venv/bin/python3 -m py_compile app/services/account_merge_service.py app/cabinet/routes/account_linking.py
```
Expected: exits 0 with no output.

- [ ] **Step 1.7: Run the service test suite**

```bash
.venv/bin/python3 -m pytest tests/services/test_account_merge_service.py -v
```
Expected: All tests pass (some old subscription tests may need the `keep_subscription_from` kwarg removed from call sites).

- [ ] **Step 1.8: Commit**

```bash
git add app/services/account_merge_service.py app/cabinet/routes/account_linking.py tests/services/test_account_merge_service.py
git commit -m "feat(merge): replace keep_subscription_from with keep_account + role-swap

MergeRequest.keep_account lets the user choose which account survives.
The execute handler swaps survivor/absorbed roles so execute_merge always
receives the chosen account as primary. Tokens returned belong to the survivor.
Removes keep_subscription_from everywhere — subscriptions are now combined
(implemented in next commit)."
```

---

### Task 2: Subscription combine in `_handle_subscription_merge` + `SubscriptionEvent` row

**Files:**
- Modify: `app/services/account_merge_service.py:334-542` (`_handle_subscription_merge` body)
- Create: `tests/services/test_merge_subscription_combine.py`

**Interfaces:**
- Consumes: `_handle_subscription_merge(db, primary, secondary, deferred_remnawave_deletions)` (Task 1 signature)
- Produces:
  - `_combine_subscription_end_dates(winner_sub, loser_sub, now) -> timedelta` — returns the extension delta (pure function)
  - Both single-tariff and multi-tariff branches implement combine logic
  - A `SubscriptionEvent` row is inserted with `event_type='merge'`, `extra={'extended_days': int, 'previous_end_date': str, 'new_end_date': str}`

- [ ] **Step 2.1: Write failing subscription-combine tests**

Create `tests/services/test_merge_subscription_combine.py`:

```python
"""Tests for subscription combining in _handle_subscription_merge.

Uses the same SimpleNamespace mock pattern as test_account_merge_service.py.
No DB connection needed — _combine_subscription_end_dates is pure.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from app.services.account_merge_service import _combine_subscription_end_dates


# ---------------------------------------------------------------------------
# Pure helper: _combine_subscription_end_dates
# ---------------------------------------------------------------------------

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
BASE = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)   # winner ends Aug 1
LOSER_END = datetime(2026, 7, 30, 0, 0, 0, tzinfo=UTC)  # loser ends Jul 30 (already ended)
LOSER_END_FUTURE = datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)  # loser ends Aug 10 (10d remain)


def _make_sub(end_date, status='active'):
    return SimpleNamespace(end_date=end_date, status=status)


class TestCombineSubscriptionEndDates:
    def test_loser_already_expired_adds_zero(self):
        """Loser's end_date is in the past → no extension."""
        winner = _make_sub(BASE)
        loser = _make_sub(LOSER_END)  # LOSER_END < NOW, remaining = 0
        extension = _combine_subscription_end_dates(winner, loser, NOW)
        assert extension == timedelta(0)

    def test_loser_has_remaining_days(self):
        """Loser ends Aug 10, now is Jul 25 → 16 days remaining → extension = 16 days."""
        winner = _make_sub(BASE)
        loser = _make_sub(LOSER_END_FUTURE)
        extension = _combine_subscription_end_dates(winner, loser, NOW)
        assert extension == timedelta(days=16)

    def test_winner_null_end_date_returns_zero(self):
        """Lifetime winner (None end_date) → never extend → returns timedelta(0)."""
        winner = _make_sub(None)   # lifetime
        loser = _make_sub(LOSER_END_FUTURE)
        extension = _combine_subscription_end_dates(winner, loser, NOW)
        assert extension == timedelta(0)

    def test_loser_null_end_date_is_impossible_path(self):
        """Loser cannot be lifetime (caller already picks the later end_date as winner).
        If somehow called with loser=None, treat remaining as 0 (no extension)."""
        winner = _make_sub(BASE)
        loser = _make_sub(None)
        extension = _combine_subscription_end_dates(winner, loser, NOW)
        assert extension == timedelta(0)
```

Run: `.venv/bin/python3 -m pytest tests/services/test_merge_subscription_combine.py -v`
Expected: FAIL with `ImportError: cannot import name '_combine_subscription_end_dates' from 'app.services.account_merge_service'`

- [ ] **Step 2.2: Implement `_combine_subscription_end_dates` pure helper**

In `app/services/account_merge_service.py`, add after `_build_subscription_preview` (around line 132), before `_build_user_preview`:

```python
def _combine_subscription_end_dates(
    winner_sub: Any,
    loser_sub: Any,
    now: datetime,
) -> 'timedelta':
    """Returns the timedelta to add to winner's end_date from loser's remaining days.

    Rules:
    - If winner has end_date=None (lifetime), return timedelta(0) — no extension needed.
    - If loser has end_date=None (caller should never pass this, but guard anyway),
      return timedelta(0) — loser is also lifetime, nothing to add.
    - Otherwise: remaining = max(0, loser.end_date - now); return remaining.
    """
    from datetime import timedelta  # already imported at module level, but be explicit

    winner_end = getattr(winner_sub, 'end_date', None)
    loser_end = getattr(loser_sub, 'end_date', None)

    if winner_end is None or loser_end is None:
        return timedelta(0)

    remaining = loser_end - now
    return max(timedelta(0), remaining)
```

Note: `timedelta` is already in stdlib; add it to the module-level import at the top of the file:
```python
# BEFORE (line 3):
from datetime import UTC, datetime

# AFTER:
from datetime import UTC, datetime, timedelta
```

- [ ] **Step 2.3: Run pure-helper tests to verify they pass**

```bash
.venv/bin/python3 -m pytest tests/services/test_merge_subscription_combine.py -v
```
Expected: 4 tests PASS.

- [ ] **Step 2.4: Add integration-style tests for single-tariff combine path**

Append to `tests/services/test_merge_subscription_combine.py`:

```python
# ---------------------------------------------------------------------------
# Integration: _handle_subscription_merge single-tariff combine
# (uses the same AsyncMock DB pattern as test_account_merge_service.py)
# ---------------------------------------------------------------------------

from app.services.account_merge_service import _handle_subscription_merge


def _make_user(id, remnawave_uuid=None, subscriptions=None):
    return SimpleNamespace(
        id=id,
        remnawave_uuid=remnawave_uuid,
        subscriptions=subscriptions or [],
    )


def _make_sub(id, user_id, end_date, status='active', tariff_id=None, remnawave_uuid=None):
    return SimpleNamespace(
        id=id,
        user_id=user_id,
        end_date=end_date,
        status=status,
        tariff_id=tariff_id,
        autopay_enabled=False,
        remnawave_uuid=remnawave_uuid,
        tariff=SimpleNamespace(name='Basic'),
        traffic_limit_gb=100.0,
        traffic_used_gb=0.0,
        device_limit=3,
        is_trial=False,
    )


def _make_db():
    db = SimpleNamespace(
        execute=AsyncMock(),
        delete=AsyncMock(),
        flush=AsyncMock(),
        add=MagicMock(),
    )
    return db


_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
_WINNER_END = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)   # Aug 1 ends later
_LOSER_END  = datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)  # loser ends Aug 10 — but wait:
# Rule: winner = later end_date; _WINNER_END (Sep 1) > _LOSER_END (Aug 10) → correct


def _patch_settings_single():
    """Patch settings.is_multi_tariff_enabled() to return False."""
    from unittest.mock import patch as _patch
    import app.services.account_merge_service as _mod
    return _patch.object(_mod.settings, 'is_multi_tariff_enabled', return_value=False)


def _patch_sync():
    from unittest.mock import patch as _patch
    import app.services.account_merge_service as _mod
    return _patch.object(_mod, '_sync_transferred_subscriptions_to_panel', new_callable=AsyncMock)


class TestSingleTariffCombine:
    async def test_both_active_winner_end_date_extended(self, monkeypatch):
        """Both subs active: winner end_date grows by loser's remaining days."""
        primary_sub = _make_sub(1, 1, _WINNER_END, remnawave_uuid='rw-p')
        loser_sub   = _make_sub(2, 2, _LOSER_END,  remnawave_uuid='rw-s')
        primary  = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[loser_sub])
        db = _make_db()
        deferred: list[str] = []

        import app.services.account_merge_service as _mod
        monkeypatch.setattr(_mod.settings, 'is_multi_tariff_enabled', lambda: False)

        with patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(db, primary, secondary, deferred)

        # Remaining from loser: Aug 10 - Jul 25 = 16 days
        expected_new_end = _WINNER_END + timedelta(days=16)
        assert primary_sub.end_date == expected_new_end

    async def test_both_active_loser_remnawave_deferred_for_deletion(self, monkeypatch):
        """Loser's RemnaWave UUID is collected for deferred deletion."""
        primary_sub = _make_sub(1, 1, _WINNER_END, remnawave_uuid='rw-p')
        loser_sub   = _make_sub(2, 2, _LOSER_END,  remnawave_uuid='rw-s')
        primary   = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[loser_sub])
        db = _make_db()
        deferred: list[str] = []

        import app.services.account_merge_service as _mod
        monkeypatch.setattr(_mod.settings, 'is_multi_tariff_enabled', lambda: False)

        with patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(db, primary, secondary, deferred)

        assert 'rw-s' in deferred
        assert secondary.remnawave_uuid is None

    async def test_subscription_event_written(self, monkeypatch):
        """A SubscriptionEvent row with event_type='merge' is added to the session."""
        primary_sub = _make_sub(1, 1, _WINNER_END)
        loser_sub   = _make_sub(2, 2, _LOSER_END)
        primary   = _make_user(1, subscriptions=[primary_sub])
        secondary = _make_user(2, subscriptions=[loser_sub])
        db = _make_db()
        deferred: list[str] = []
        added_objects = []
        db.add = lambda obj: added_objects.append(obj)

        import app.services.account_merge_service as _mod
        monkeypatch.setattr(_mod.settings, 'is_multi_tariff_enabled', lambda: False)

        with patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(db, primary, secondary, deferred)

        from app.database.models import SubscriptionEvent
        events = [o for o in added_objects if isinstance(o, SubscriptionEvent)]
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == 'merge'
        assert ev.user_id == primary.id
        assert ev.subscription_id == primary_sub.id
        assert ev.extra['extended_days'] == 16
        assert 'previous_end_date' in ev.extra
        assert 'new_end_date' in ev.extra

    async def test_lifetime_winner_no_extension_no_event(self, monkeypatch):
        """Lifetime winner (end_date=None): no extension, no SubscriptionEvent."""
        primary_sub = _make_sub(1, 1, None)   # lifetime
        loser_sub   = _make_sub(2, 2, _LOSER_END)
        primary   = _make_user(1, subscriptions=[primary_sub])
        secondary = _make_user(2, subscriptions=[loser_sub])
        db = _make_db()
        deferred: list[str] = []
        added_objects = []
        db.add = lambda obj: added_objects.append(obj)

        import app.services.account_merge_service as _mod
        monkeypatch.setattr(_mod.settings, 'is_multi_tariff_enabled', lambda: False)

        with patch('app.services.account_merge_service.datetime') as mock_dt:
            mock_dt.now.return_value = _NOW
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            await _handle_subscription_merge(db, primary, secondary, deferred)

        from app.database.models import SubscriptionEvent
        events = [o for o in added_objects if isinstance(o, SubscriptionEvent)]
        assert len(events) == 0
        assert primary_sub.end_date is None  # unchanged

    async def test_only_primary_sub_no_combine(self, monkeypatch):
        """Only primary has sub — no combine, secondary's RemnaWave deferred."""
        primary_sub = _make_sub(1, 1, _WINNER_END, remnawave_uuid='rw-p')
        primary   = _make_user(1, remnawave_uuid='rw-p', subscriptions=[primary_sub])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[])
        db = _make_db()
        deferred: list[str] = []

        import app.services.account_merge_service as _mod
        monkeypatch.setattr(_mod.settings, 'is_multi_tariff_enabled', lambda: False)

        await _handle_subscription_merge(db, primary, secondary, deferred)

        assert 'rw-s' in deferred
        assert primary_sub.end_date == _WINNER_END  # unchanged

    async def test_only_secondary_sub_transferred(self, monkeypatch):
        """Only secondary has sub — it's transferred to primary, no combine."""
        loser_sub   = _make_sub(2, 2, _LOSER_END, remnawave_uuid='rw-s')
        primary   = _make_user(1, subscriptions=[])
        secondary = _make_user(2, remnawave_uuid='rw-s', subscriptions=[loser_sub])
        db = _make_db()
        deferred: list[str] = []

        import app.services.account_merge_service as _mod
        monkeypatch.setattr(_mod.settings, 'is_multi_tariff_enabled', lambda: False)

        await _handle_subscription_merge(db, primary, secondary, deferred)

        assert loser_sub.user_id == 1
        assert primary.remnawave_uuid == 'rw-s'
        assert secondary.remnawave_uuid is None
```

Run: `.venv/bin/python3 -m pytest tests/services/test_merge_subscription_combine.py -v`
Expected: Pure-helper tests pass; new tests FAIL because `_handle_subscription_merge` still has old body.

- [ ] **Step 2.5: Rewrite `_handle_subscription_merge` body**

Replace the entire body of `_handle_subscription_merge` in `app/services/account_merge_service.py` (lines 334-449 multi-tariff branch and 451-542 single-tariff branch) with the new implementation below. Keep the function signature from Task 1.

```python
async def _handle_subscription_merge(
    db: AsyncSession,
    primary: User,
    secondary: User,
    deferred_remnawave_deletions: list[str],
) -> None:
    """Merges subscriptions from secondary into primary.

    New combine logic (both accounts have active/non-expired subscription):
    - Winner = the one with the later end_date (None = lifetime always wins).
    - Extension = max(0, loser.end_date - now).
    - Winner's end_date += extension; a SubscriptionEvent row records the merge.
    - Loser's RemnaWave user is deferred for deletion; loser sub record stays
      (owned by primary) but marked expired.

    Multi-tariff: same rule applied per overlapping tariff pair; non-overlapping
    subs transferred as-is.
    """
    now = datetime.now(UTC)

    # ------------------------------------------------------------------
    # Multi-tariff mode
    # ------------------------------------------------------------------
    if settings.is_multi_tariff_enabled():
        secondary_subs = list(getattr(secondary, 'subscriptions', None) or [])
        primary_subs = list(getattr(primary, 'subscriptions', None) or [])
        secondary_legacy_uuid = secondary.remnawave_uuid

        # Build map of primary's active tariff_id -> subscription
        primary_active: dict[int, Any] = {}
        for ps in primary_subs:
            if ps.tariff_id is not None and ps.status in ('active', 'trial'):
                primary_active[ps.tariff_id] = ps

        transferred: list[Subscription] = []
        if secondary_subs:
            for sub in secondary_subs:
                sub_tariff_id = getattr(sub, 'tariff_id', None)
                sub_remnawave_uuid = getattr(sub, 'remnawave_uuid', None)

                # Tariff conflict: both have active sub for the same tariff
                if (
                    sub_tariff_id is not None
                    and sub.status in ('active', 'trial')
                    and sub_tariff_id in primary_active
                ):
                    primary_conflict = primary_active[sub_tariff_id]
                    primary_end = getattr(primary_conflict, 'end_date', None)
                    secondary_end = getattr(sub, 'end_date', None)

                    # Determine winner (later end_date; None = lifetime wins)
                    secondary_wins = (secondary_end is None and primary_end is not None) or (
                        secondary_end is not None
                        and primary_end is not None
                        and secondary_end > primary_end
                    )

                    if secondary_wins:
                        winner_sub, loser_sub = sub, primary_conflict
                    else:
                        winner_sub, loser_sub = primary_conflict, sub

                    # Combine: extend winner by loser's remaining days
                    extension = _combine_subscription_end_dates(winner_sub, loser_sub, now)
                    if extension.total_seconds() > 0 and winner_sub.end_date is not None:
                        previous_end = winner_sub.end_date
                        winner_sub.end_date = previous_end + extension
                        extended_days = int(extension.total_seconds() / 86400)
                        db.add(SubscriptionEvent(
                            event_type='merge',
                            user_id=primary.id,
                            subscription_id=winner_sub.id,
                            occurred_at=now,
                            extra={
                                'extended_days': extended_days,
                                'previous_end_date': previous_end.isoformat(),
                                'new_end_date': winner_sub.end_date.isoformat(),
                            },
                        ))
                        logger.info(
                            'Multi-tariff combine: extended winner end_date',
                            tariff_id=sub_tariff_id,
                            winner_sub_id=winner_sub.id,
                            extended_days=extended_days,
                            new_end_date=str(winner_sub.end_date),
                        )

                    # Loser is expired, then transferred to primary (as expired record)
                    loser_sub.status = 'expired'
                    loser_sub.autopay_enabled = False

                    if secondary_wins:
                        # secondary sub wins → primary sub is loser, secondary sub transfers
                        primary_conflict.status = 'expired'
                        primary_conflict.autopay_enabled = False
                        if primary_conflict.remnawave_uuid:
                            deferred_remnawave_deletions.append(primary_conflict.remnawave_uuid)
                            primary_conflict.remnawave_uuid = None
                        sub.user_id = primary.id
                        transferred.append(sub)
                    else:
                        # primary sub wins → secondary sub is loser, transfer it as expired
                        if sub.remnawave_uuid:
                            deferred_remnawave_deletions.append(sub.remnawave_uuid)
                            sub.remnawave_uuid = None
                        sub.user_id = primary.id
                        transferred.append(sub)
                    await db.flush()
                    continue

                # No conflict — simple transfer
                sub.user_id = primary.id
                transferred.append(sub)
                logger.info(
                    'Transferred subscription during account merge (multi-tariff)',
                    subscription_id=sub.id,
                    tariff_id=sub_tariff_id,
                    from_user=secondary.id,
                    to_user=primary.id,
                    remnawave_uuid=sub_remnawave_uuid,
                )
                if sub_remnawave_uuid and secondary_legacy_uuid and sub_remnawave_uuid == secondary_legacy_uuid:
                    logger.warning(
                        'Transferred subscription remnawave_uuid matches secondary legacy uuid — manual panel review required',
                        subscription_id=sub.id,
                        remnawave_uuid=sub_remnawave_uuid,
                        secondary_user_id=secondary.id,
                        primary_user_id=primary.id,
                    )
            await db.flush()
            logger.info(
                'Мерж подписок (multi-tariff): перенесено подписок secondary на primary',
                count=len(transferred),
                primary_id=primary.id,
                secondary_id=secondary.id,
            )
            await _sync_transferred_subscriptions_to_panel(primary, transferred)

        if secondary.remnawave_uuid:
            secondary.remnawave_uuid = None
        return

    # ------------------------------------------------------------------
    # Single-tariff mode
    # ------------------------------------------------------------------
    primary_subs = getattr(primary, 'subscriptions', None) or []
    secondary_subs = getattr(secondary, 'subscriptions', None) or []
    primary_sub = primary_subs[0] if primary_subs else None
    secondary_sub = secondary_subs[0] if secondary_subs else None
    has_primary_sub = primary_sub is not None
    has_secondary_sub = secondary_sub is not None

    # Neither has a subscription
    if not has_primary_sub and not has_secondary_sub:
        logger.info('Мерж подписок: ни у кого нет подписки', primary_id=primary.id, secondary_id=secondary.id)
        return

    # Only primary has a subscription
    if has_primary_sub and not has_secondary_sub:
        if secondary.remnawave_uuid:
            deferred_remnawave_deletions.append(secondary.remnawave_uuid)
            secondary.remnawave_uuid = None
        logger.info('Мерж подписок: оставлена подписка primary, secondary не имел подписки', primary_id=primary.id, secondary_id=secondary.id)
        return

    # Only secondary has a subscription — transfer to primary
    if not has_primary_sub and has_secondary_sub:
        assert secondary_sub is not None
        secondary_sub.user_id = primary.id
        if secondary.remnawave_uuid:
            uuid_to_transfer = secondary.remnawave_uuid
            secondary.remnawave_uuid = None
            await db.flush()
            primary.remnawave_uuid = uuid_to_transfer
        await db.flush()
        logger.info('Мерж подписок: перенесена подписка secondary на primary', primary_id=primary.id, secondary_id=secondary.id)
        return

    # Both have subscriptions — COMBINE
    assert primary_sub is not None
    assert secondary_sub is not None

    primary_end = getattr(primary_sub, 'end_date', None)
    secondary_end = getattr(secondary_sub, 'end_date', None)

    # Determine winner (later end_date; None=lifetime always wins)
    secondary_wins = (secondary_end is None and primary_end is not None) or (
        secondary_end is not None
        and primary_end is not None
        and secondary_end > primary_end
    )

    if secondary_wins:
        winner_sub, loser_sub, winner_remnawave, loser_remnawave = (
            secondary_sub, primary_sub, secondary.remnawave_uuid, primary.remnawave_uuid
        )
    else:
        winner_sub, loser_sub, winner_remnawave, loser_remnawave = (
            primary_sub, secondary_sub, primary.remnawave_uuid, secondary.remnawave_uuid
        )

    # Extend winner by loser's remaining days
    extension = _combine_subscription_end_dates(winner_sub, loser_sub, now)
    if extension.total_seconds() > 0 and winner_sub.end_date is not None:
        previous_end = winner_sub.end_date
        winner_sub.end_date = previous_end + extension
        extended_days = int(extension.total_seconds() / 86400)
        db.add(SubscriptionEvent(
            event_type='merge',
            user_id=primary.id,
            subscription_id=winner_sub.id,
            occurred_at=now,
            extra={
                'extended_days': extended_days,
                'previous_end_date': previous_end.isoformat(),
                'new_end_date': winner_sub.end_date.isoformat(),
            },
        ))
        logger.info(
            'Мерж подписок: расширена дата окончания победителя',
            primary_id=primary.id,
            secondary_id=secondary.id,
            extended_days=extended_days,
            new_end_date=str(winner_sub.end_date),
        )

    # Transfer loser subscription to primary (as expired), defer RemnaWave deletion
    loser_sub.status = 'expired'
    loser_sub.autopay_enabled = False
    if loser_remnawave:
        deferred_remnawave_deletions.append(loser_remnawave)

    if secondary_wins:
        # Winner is secondary_sub → move it to primary, transfer remnawave_uuid
        winner_sub.user_id = primary.id
        if winner_remnawave and winner_remnawave != primary.remnawave_uuid:
            secondary.remnawave_uuid = None
            await db.flush()
            primary.remnawave_uuid = winner_remnawave
        else:
            secondary.remnawave_uuid = None
        loser_sub.user_id = primary.id  # loser (primary_sub) already owned by primary
        primary.remnawave_uuid = winner_remnawave
    else:
        # Winner is primary_sub — already on primary; just transfer loser and clear secondary uuid
        loser_sub.user_id = primary.id
        secondary.remnawave_uuid = None

    await db.flush()
    logger.info(
        'Мерж подписок: обе подписки объединены, старая дата продлена',
        primary_id=primary.id,
        secondary_id=secondary.id,
        secondary_wins=secondary_wins,
    )
```

- [ ] **Step 2.6: Run the combine tests**

```bash
.venv/bin/python3 -m pytest tests/services/test_merge_subscription_combine.py -v
```
Expected: All tests PASS.

- [ ] **Step 2.7: Run the full service test suite**

```bash
.venv/bin/python3 -m pytest tests/services/test_account_merge_service.py tests/services/test_merge_subscription_combine.py -v
```
Expected: All tests PASS. (The old `test_both_have_subscription_keep_primary/secondary` tests were updated in Task 1.)

- [ ] **Step 2.8: py_compile check**

```bash
.venv/bin/python3 -m py_compile app/services/account_merge_service.py
```
Expected: exits 0.

- [ ] **Step 2.9: Commit**

```bash
git add app/services/account_merge_service.py tests/services/test_merge_subscription_combine.py
git commit -m "feat(merge): combine subscriptions instead of discarding one

When both accounts have an active subscription, extend the later-ending
one by the loser's remaining days (max 0). A SubscriptionEvent row of
type 'merge' records extended_days, previous_end_date, new_end_date so
the purchase timeline shows the merge. Lifetime (NULL end_date) wins
without extension. Applies to both single- and multi-tariff branches."
```

---

### Task 3: Single-profile invariant regression tests (and any required fix)

**Files:**
- Create: `tests/services/test_merge_invariant.py`
- Potentially modify: `app/services/account_merge_service.py` (if a bug is discovered)

**Interfaces:**
- Consumes: `execute_merge(db, primary_user_id, secondary_user_id)` (Task 1 signature)
- Produces: Regression tests that lock the single-profile invariant in both directions (Telegram+sub into email, and email into Telegram+sub)

The spec (A4) states that after merge the survivor holds ALL identities. The current code already does this (sections 1-3 in `execute_merge`): OAuth fields, telegram_id, email+password_hash are all transferred if the survivor (playing primary) doesn't already have them. With the role-swap in Task 1, when the user picks secondary as survivor, secondary plays primary in `execute_merge`. The transfer logic is already written to copy from secondary (original secondary, but now playing secondary role) to primary (chosen survivor). Therefore: the identity transfer should work correctly because the roles are already swapped before calling `execute_merge`.

The test must verify this claim and catch any regression.

- [ ] **Step 3.1: Write failing tests for single-profile invariant**

Create `tests/services/test_merge_invariant.py`:

```python
"""Regression tests: after merge, survivor holds ALL identities of both accounts.

Spec A4: After merge the survivor must hold ALL identifiers of both:
- telegram_id
- every oauth id (google_id, yandex_id, discord_id, vk_id)
- email + password_hash

And the absorbed account must be status='deleted' with all identifiers NULL.

Two directions tested:
- Telegram account (with active sub) merged INTO email account (survivor = email)
- Email account (with active sub) merged INTO Telegram account (survivor = telegram)

These tests use the SimpleNamespace mock pattern from test_account_merge_service.py.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.services.account_merge_service as _mod
from app.services.account_merge_service import execute_merge


_NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=UTC)
_SUB_END = datetime(2026, 12, 31, 0, 0, 0, tzinfo=UTC)


def _make_user(
    id,
    telegram_id=None,
    email=None,
    password_hash=None,
    email_verified=False,
    email_verified_at=None,
    google_id=None,
    yandex_id=None,
    discord_id=None,
    vk_id=None,
    status='active',
    subscriptions=None,
    remnawave_uuid=None,
    balance_kopeks=0,
    referred_by_id=None,
    referral_code=None,
    partner_status='none',
    referral_commission_percent=None,
    has_had_paid_subscription=False,
    has_made_first_topup=False,
    restriction_topup=False,
    restriction_subscription=False,
    restriction_reason=None,
    used_promocodes=0,
):
    subs = subscriptions or []
    return SimpleNamespace(
        id=id,
        telegram_id=telegram_id,
        email=email,
        password_hash=password_hash,
        email_verified=email_verified,
        email_verified_at=email_verified_at,
        email_change_new=None,
        email_change_code=None,
        email_change_expires=None,
        email_verification_token=None,
        email_verification_expires=None,
        password_reset_token=None,
        password_reset_expires=None,
        google_id=google_id,
        yandex_id=yandex_id,
        discord_id=discord_id,
        vk_id=vk_id,
        status=status,
        subscriptions=subs,
        remnawave_uuid=remnawave_uuid,
        balance_kopeks=balance_kopeks,
        referred_by_id=referred_by_id,
        referral_code=referral_code,
        partner_status=partner_status,
        referral_commission_percent=referral_commission_percent,
        has_had_paid_subscription=has_had_paid_subscription,
        has_made_first_topup=has_made_first_topup,
        restriction_topup=restriction_topup,
        restriction_subscription=restriction_subscription,
        restriction_reason=restriction_reason,
        used_promocodes=used_promocodes,
        updated_at=_NOW,
    )


def _make_sub(id, user_id, end_date=None):
    return SimpleNamespace(
        id=id, user_id=user_id,
        end_date=end_date or _SUB_END,
        status='active', is_trial=False,
        autopay_enabled=False, tariff_id=None,
        remnawave_uuid=None,
        tariff=SimpleNamespace(name='Basic'),
        traffic_limit_gb=100.0, traffic_used_gb=0.0, device_limit=3,
    )


def _make_db():
    db = SimpleNamespace(
        execute=AsyncMock(),
        delete=AsyncMock(),
        flush=AsyncMock(),
        add=lambda obj: None,
    )
    return db


def _patch_rw_delete():
    return patch.object(_mod, '_delete_remnawave_user_with_fallback', new_callable=AsyncMock)


def _patch_single_tariff():
    return patch.object(_mod.settings, 'is_multi_tariff_enabled', return_value=False)


def _two_call_mock(user_a, user_b):
    """Returns AsyncMock that yields user_a on first call, user_b on second."""
    return AsyncMock(side_effect=[user_a, user_b])


class TestSingleProfileInvariant:
    async def test_telegram_sub_into_email_survivor_email(self, monkeypatch):
        """
        Scenario: initiator = email account (id=1, no sub), secondary = telegram account (id=2, has sub).
        User picks keep_account = 2 (telegram).
        Role-swap at handler level: survivor_id=2, absorbed_id=1.
        execute_merge is called with primary=2, secondary=1.
        After merge:
          (a) user id=2 (survivor) has telegram_id AND email+password_hash
          (b) user id=2 has the active subscription
          (c) user id=1 (absorbed) is status='deleted', telegram_id=None, email=None
        """
        sub_telegram = _make_sub(id=10, user_id=2)
        telegram_user = _make_user(id=2, telegram_id=99999, subscriptions=[sub_telegram], remnawave_uuid='rw-tg')
        email_user    = _make_user(id=1, email='user@example.com', password_hash='phash', email_verified=True)

        # After role-swap: survivor_id=2 plays primary, absorbed_id=1 plays secondary.
        # execute_merge(db, primary_user_id=2, secondary_user_id=1)
        monkeypatch.setattr(_mod, 'get_user_by_id', _two_call_mock(telegram_user, email_user))
        db = _make_db()
        with _patch_rw_delete(), _patch_single_tariff():
            result = await execute_merge(db, primary_user_id=2, secondary_user_id=1)

        # (a) survivor holds both identities
        assert result.telegram_id == 99999, 'survivor must retain telegram_id'
        assert result.email == 'user@example.com', 'survivor must gain email from absorbed'
        assert result.password_hash == 'phash', 'survivor must gain password_hash from absorbed'

        # (b) subscription stays on survivor
        assert sub_telegram.user_id == 2, 'subscription must remain on survivor (id=2)'

        # (c) absorbed is deleted with all identifiers NULL
        assert email_user.status == 'deleted', 'absorbed must be marked deleted'
        assert email_user.email is None, 'absorbed email must be NULL'
        assert email_user.password_hash is None, 'absorbed password_hash must be NULL'
        assert email_user.telegram_id is None, 'absorbed telegram_id must be NULL (was already None)'

    async def test_email_sub_into_telegram_survivor_telegram(self, monkeypatch):
        """
        Scenario: initiator = telegram account (id=1, no sub), secondary = email account (id=2, has sub).
        User picks keep_account = 2 (email).
        Role-swap: survivor_id=2, absorbed_id=1.
        execute_merge(db, primary_user_id=2, secondary_user_id=1).
        After merge:
          (a) user id=2 (survivor) has email+password_hash AND telegram_id
          (b) user id=2 has the active subscription
          (c) user id=1 (absorbed) status='deleted', telegram_id=None
        """
        sub_email = _make_sub(id=20, user_id=2)
        email_user    = _make_user(id=2, email='user@example.com', password_hash='phash', subscriptions=[sub_email])
        telegram_user = _make_user(id=1, telegram_id=88888)

        monkeypatch.setattr(_mod, 'get_user_by_id', _two_call_mock(email_user, telegram_user))
        db = _make_db()
        with _patch_rw_delete(), _patch_single_tariff():
            result = await execute_merge(db, primary_user_id=2, secondary_user_id=1)

        # (a) survivor holds both identities
        assert result.email == 'user@example.com', 'survivor must retain email'
        assert result.password_hash == 'phash', 'survivor must retain password_hash'
        assert result.telegram_id == 88888, 'survivor must gain telegram_id from absorbed'

        # (b) subscription stays on survivor
        assert sub_email.user_id == 2, 'subscription must remain on survivor (id=2)'

        # (c) absorbed is deleted
        assert telegram_user.status == 'deleted'
        assert telegram_user.telegram_id is None

    async def test_both_have_login_methods_survivor_gets_all(self, monkeypatch):
        """
        Survivor starts with telegram; absorbed has yandex_id + email.
        After merge survivor has telegram + yandex_id + email.
        """
        survivor = _make_user(id=5, telegram_id=77777)
        absorbed = _make_user(id=6, yandex_id='y-123', email='a@b.com', password_hash='hash2')

        monkeypatch.setattr(_mod, 'get_user_by_id', _two_call_mock(survivor, absorbed))
        db = _make_db()
        with _patch_rw_delete(), _patch_single_tariff():
            result = await execute_merge(db, primary_user_id=5, secondary_user_id=6)

        assert result.telegram_id == 77777
        assert result.yandex_id == 'y-123'
        assert result.email == 'a@b.com'
        assert result.password_hash == 'hash2'
        assert absorbed.status == 'deleted'
        assert absorbed.yandex_id is None
        assert absorbed.email is None
```

Run: `.venv/bin/python3 -m pytest tests/services/test_merge_invariant.py -v`
Expected: PASS (the invariant is already correctly implemented once roles are swapped at the handler level).

If any test fails: the failure message indicates which identity field is not transferred. The fix is in `execute_merge()` — find which of steps 1-3 has a guard preventing the transfer (e.g., "if secondary_value and not primary_value" — if the survivor already has the field set, the absorbed's value won't overwrite it; this is correct). If the test fails because the survivor doesn't receive an identifier: check that the role-swap passes the right order of ids.

- [ ] **Step 3.2: py_compile check**

```bash
.venv/bin/python3 -m py_compile app/services/account_merge_service.py
```
Expected: exits 0.

- [ ] **Step 3.3: Run full service suite**

```bash
.venv/bin/python3 -m pytest tests/services/test_account_merge_service.py tests/services/test_merge_subscription_combine.py tests/services/test_merge_invariant.py -v
```
Expected: All tests PASS.

- [ ] **Step 3.4: Commit**

```bash
git add tests/services/test_merge_invariant.py
git commit -m "test(merge): regression tests for single-profile invariant (A4)

Locks the invariant that after merge the survivor holds ALL identities
of both accounts (telegram_id, oauth ids, email+password_hash) and the
absorbed account is status='deleted' with all identifiers NULL.
Tests both directions: Telegram+sub absorbed into email survivor, and
email+sub absorbed into Telegram survivor."
```

---

### Task 4: Preview `referrals_count` + `recommended` flag

**Files:**
- Modify: `app/services/account_merge_service.py` (`_build_user_preview`, `get_merge_preview`, add `_compute_recommended`)
- Modify: `app/cabinet/routes/account_linking.py` (`MergePreviewUser` schema)
- Modify: `tests/services/test_account_merge_service.py` (add tests for new preview fields)

**Interfaces:**
- Consumes: `get_user_by_id` (existing); DB query for `referred_by_id == user.id AND status='active'` count
- Produces:
  - `_build_user_preview(user, referrals_count: int) -> dict` — adds `referrals_count: int` key
  - `_compute_recommended(primary_preview: dict, secondary_preview: dict) -> tuple[bool, bool]` — returns `(primary_recommended, secondary_recommended)`; exactly one is `True`
  - `get_merge_preview` returns dicts with `referrals_count` and `recommended` keys
  - `MergePreviewUser` Pydantic model gains `referrals_count: int = 0` and `recommended: bool = False`

- [ ] **Step 4.1: Write failing preview tests**

Append to `tests/services/test_account_merge_service.py`:

```python
# ---------------------------------------------------------------------------
# Preview: referrals_count and recommended
# ---------------------------------------------------------------------------

from app.services.account_merge_service import _compute_recommended


class TestComputeRecommended:
    """Priority: (1) has active sub; (2) more referrals; (3) higher balance; (4) older created_at."""

    def _preview(self, has_sub=False, referrals_count=0, balance_kopeks=0, created_at=None):
        from datetime import UTC, datetime
        return {
            'subscription': {'status': 'active'} if has_sub else None,
            'referrals_count': referrals_count,
            'balance_kopeks': balance_kopeks,
            'created_at': created_at or datetime(2024, 6, 1, tzinfo=UTC),
        }

    def test_primary_has_sub_secondary_does_not(self):
        p = self._preview(has_sub=True)
        s = self._preview(has_sub=False)
        p_rec, s_rec = _compute_recommended(p, s)
        assert p_rec is True and s_rec is False

    def test_secondary_has_sub_primary_does_not(self):
        p = self._preview(has_sub=False)
        s = self._preview(has_sub=True)
        p_rec, s_rec = _compute_recommended(p, s)
        assert p_rec is False and s_rec is True

    def test_both_have_sub_more_referrals_wins(self):
        p = self._preview(has_sub=True, referrals_count=5)
        s = self._preview(has_sub=True, referrals_count=10)
        p_rec, s_rec = _compute_recommended(p, s)
        assert p_rec is False and s_rec is True

    def test_equal_sub_equal_referrals_higher_balance_wins(self):
        p = self._preview(has_sub=True, referrals_count=3, balance_kopeks=500)
        s = self._preview(has_sub=True, referrals_count=3, balance_kopeks=1000)
        p_rec, s_rec = _compute_recommended(p, s)
        assert p_rec is False and s_rec is True

    def test_all_equal_older_account_wins(self):
        from datetime import UTC, datetime
        older  = datetime(2023, 1, 1, tzinfo=UTC)
        newer  = datetime(2024, 6, 1, tzinfo=UTC)
        p = self._preview(has_sub=True, referrals_count=3, balance_kopeks=500, created_at=older)
        s = self._preview(has_sub=True, referrals_count=3, balance_kopeks=500, created_at=newer)
        p_rec, s_rec = _compute_recommended(p, s)
        assert p_rec is True and s_rec is False   # older = primary wins

    def test_neither_has_sub_more_referrals_wins(self):
        p = self._preview(has_sub=False, referrals_count=2)
        s = self._preview(has_sub=False, referrals_count=7)
        p_rec, s_rec = _compute_recommended(p, s)
        assert p_rec is False and s_rec is True

    def test_exactly_one_recommended(self):
        """Invariant: exactly one of the two flags is True."""
        p = self._preview(has_sub=True, referrals_count=3, balance_kopeks=1000)
        s = self._preview(has_sub=True, referrals_count=3, balance_kopeks=1000)
        # Tiebreaker: older created_at — both are equal here → primary wins (tiebreak)
        p_rec, s_rec = _compute_recommended(p, s)
        assert (p_rec + s_rec) == 1  # exactly one True


class TestGetMergePreviewWithReferrals:
    async def test_preview_includes_referrals_count(self, monkeypatch):
        db = _make_db()
        primary   = _make_user(id=1, telegram_id=111)
        secondary = _make_user(id=2, google_id='g123')
        monkeypatch.setattr(account_merge_service, 'get_user_by_id', AsyncMock(side_effect=[primary, secondary]))
        # Patch the referral count queries: primary has 3 referrals, secondary has 1
        call_count = [0]
        async def _fake_count(db, user_id):
            call_count[0] += 1
            return 3 if user_id == 1 else 1
        monkeypatch.setattr(account_merge_service, '_count_active_referrals', _fake_count)

        result = await account_merge_service.get_merge_preview(db, 1, 2)
        assert result['primary']['referrals_count'] == 3
        assert result['secondary']['referrals_count'] == 1

    async def test_preview_recommended_flag_set(self, monkeypatch):
        db = _make_db()
        primary   = _make_user(id=1)  # no sub
        secondary = _make_user(id=2, subscription=_make_subscription())  # has sub
        monkeypatch.setattr(account_merge_service, 'get_user_by_id', AsyncMock(side_effect=[primary, secondary]))
        async def _fake_count(db, user_id): return 0
        monkeypatch.setattr(account_merge_service, '_count_active_referrals', _fake_count)

        result = await account_merge_service.get_merge_preview(db, 1, 2)
        # secondary has sub → secondary is recommended
        assert result['secondary']['recommended'] is True
        assert result['primary']['recommended'] is False
```

Run: `.venv/bin/python3 -m pytest tests/services/test_account_merge_service.py::TestComputeRecommended tests/services/test_account_merge_service.py::TestGetMergePreviewWithReferrals -v`
Expected: FAIL with `ImportError: cannot import name '_compute_recommended'` and `AttributeError: _count_active_referrals`.

- [ ] **Step 4.2: Implement `_compute_recommended` and `_count_active_referrals`**

In `app/services/account_merge_service.py`, add after `compute_auth_methods`:

```python
def _compute_recommended(
    primary_preview: dict[str, Any],
    secondary_preview: dict[str, Any],
) -> tuple[bool, bool]:
    """Returns (primary_recommended, secondary_recommended) with exactly one True.

    Priority (higher = wins):
    1. Has active/non-expired subscription (subscription is not None)
    2. More active referrals (referrals_count)
    3. Higher balance (balance_kopeks)
    4. Older account (smaller created_at — earlier creation wins)
    """
    def _score(preview: dict[str, Any]) -> tuple[int, int, int, datetime]:
        has_sub = 1 if preview.get('subscription') is not None else 0
        refs = preview.get('referrals_count', 0)
        bal = preview.get('balance_kopeks', 0)
        created = preview.get('created_at') or datetime.max.replace(tzinfo=UTC)
        return (has_sub, refs, bal, created)

    p_score = _score(primary_preview)
    s_score = _score(secondary_preview)

    # Compare element by element; on created_at, smaller (older) is better → invert
    p_has_sub, p_refs, p_bal, p_created = p_score
    s_has_sub, s_refs, s_bal, s_created = s_score

    if p_has_sub != s_has_sub:
        return (p_has_sub > s_has_sub, s_has_sub > p_has_sub)
    if p_refs != s_refs:
        return (p_refs > s_refs, s_refs > p_refs)
    if p_bal != s_bal:
        return (p_bal > s_bal, s_bal > p_bal)
    if p_created != s_created:
        # Older created_at wins (smaller datetime value)
        return (p_created < s_created, s_created < p_created)
    # Complete tie — primary wins by default
    return (True, False)


async def _count_active_referrals(db: AsyncSession, user_id: int) -> int:
    """Count users actively referred by this account (referred_by_id == user_id, status='active')."""
    result = await db.execute(
        select(func.count()).select_from(User).where(
            User.referred_by_id == user_id,
            User.status == 'active',
        )
    )
    return result.scalar_one()
```

Add to the module-level imports at the top (these should already be imported, but double-check):
```python
from sqlalchemy import and_, delete, func, or_, select, update
```
(`func` may not be in the current imports — add it if needed.)

Then update `_build_user_preview` to accept `referrals_count`:

```python
def _build_user_preview(user: User, referrals_count: int = 0) -> dict[str, Any]:
    """Формирует превью данных пользователя для предварительного просмотра мержа."""
    subs = getattr(user, 'subscriptions', None) or []
    return {
        'id': user.id,
        'username': user.username,
        'first_name': user.first_name,
        'email': user.email,
        'auth_methods': compute_auth_methods(user),
        'balance_kopeks': user.balance_kopeks,
        'subscription': _build_subscription_preview(subs[0] if subs else None),
        'subscriptions_count': len(subs),
        'referrals_count': referrals_count,
        'created_at': user.created_at,
    }
```

And update `get_merge_preview` to query referral counts and add `recommended`:

```python
async def get_merge_preview(
    db: AsyncSession,
    primary_user_id: int,
    secondary_user_id: int,
) -> dict[str, Any]:
    """Возвращает превью данных обоих аккаунтов для подтверждения мержа."""
    if primary_user_id == secondary_user_id:
        raise ValueError('primary_user_id и secondary_user_id не могут совпадать')

    primary = await get_user_by_id(db, primary_user_id)
    secondary = await get_user_by_id(db, secondary_user_id)

    if not primary:
        raise ValueError(f'Основной пользователь (id={primary_user_id}) не найден')
    if not secondary:
        raise ValueError(f'Вторичный пользователь (id={secondary_user_id}) не найден')

    primary_refs = await _count_active_referrals(db, primary_user_id)
    secondary_refs = await _count_active_referrals(db, secondary_user_id)

    primary_preview = _build_user_preview(primary, referrals_count=primary_refs)
    secondary_preview = _build_user_preview(secondary, referrals_count=secondary_refs)

    primary_rec, secondary_rec = _compute_recommended(primary_preview, secondary_preview)
    primary_preview['recommended'] = primary_rec
    secondary_preview['recommended'] = secondary_rec

    return {
        'primary': primary_preview,
        'secondary': secondary_preview,
    }
```

- [ ] **Step 4.3: Update `MergePreviewUser` Pydantic schema**

In `app/cabinet/routes/account_linking.py`, update `MergePreviewUser`:

```python
class MergePreviewUser(BaseModel):
    id: int
    username: str | None = None
    first_name: str | None = None
    email: str | None = None
    auth_methods: list[str]
    balance_kopeks: int = 0
    subscription: MergePreviewSubscription | None = None
    created_at: datetime | None = None
    referrals_count: int = 0
    recommended: bool = False
```

- [ ] **Step 4.4: Run preview tests**

```bash
.venv/bin/python3 -m pytest tests/services/test_account_merge_service.py::TestComputeRecommended tests/services/test_account_merge_service.py::TestGetMergePreviewWithReferrals -v
```
Expected: All tests PASS.

- [ ] **Step 4.5: Run full backend test suite to check no regressions**

```bash
.venv/bin/python3 -m pytest tests/services/test_account_merge_service.py tests/services/test_merge_subscription_combine.py tests/services/test_merge_invariant.py -v
```
Expected: All pass.

- [ ] **Step 4.6: py_compile check**

```bash
.venv/bin/python3 -m py_compile app/services/account_merge_service.py app/cabinet/routes/account_linking.py
```
Expected: exits 0.

- [ ] **Step 4.7: Commit**

```bash
git add app/services/account_merge_service.py app/cabinet/routes/account_linking.py tests/services/test_account_merge_service.py
git commit -m "feat(merge): add referrals_count and recommended to preview (A5)

Preview now counts active referrals per account and sets a 'recommended'
flag by priority: (1) has active subscription, (2) more referrals,
(3) higher balance, (4) older account. Exactly one side is recommended.
MergePreviewUser schema gains referrals_count and recommended fields."
```

---

### Task 5: Frontend types, API client, and locale keys

**Files:**
- Modify: `src/types/index.ts:833-855`
- Modify: `src/api/auth.ts:349-360`
- Modify: `src/locales/ru.json` (`merge.*` section)
- Modify: `src/locales/en.json` (`merge.*` section)

**Interfaces:**
- Consumes: nothing (types-only task)
- Produces:
  - `MergeAccountPreview` interface with `referrals_count: number` and `recommended: boolean`
  - `executeMerge(mergeToken: string, keepAccount: number): Promise<MergeResponse>`
  - Locale keys: `merge.makeMain`, `merge.recommended`, `merge.oneProfileWarning`, `merge.combinedSubscription`, `merge.referrals`, plus removes `merge.keepThisSubscription`, `merge.unselectedSubscriptionDeleted`, `merge.chooseSubscription`

- [ ] **Step 5.1: Write failing TypeScript type tests**

Create `src/utils/mergeTypes.test.ts`:

```typescript
import { describe, it, expect } from 'vitest';
import type { MergeAccountPreview, MergePreviewResponse } from '../types';

describe('MergeAccountPreview types', () => {
  it('accepts referrals_count and recommended fields', () => {
    const preview: MergeAccountPreview = {
      id: 1,
      username: null,
      first_name: null,
      email: null,
      auth_methods: ['telegram'],
      balance_kopeks: 0,
      subscription: null,
      created_at: null,
      referrals_count: 5,
      recommended: true,
    };
    expect(preview.referrals_count).toBe(5);
    expect(preview.recommended).toBe(true);
  });

  it('MergePreviewResponse uses updated MergeAccountPreview', () => {
    const response: MergePreviewResponse = {
      primary: {
        id: 1,
        username: null,
        first_name: null,
        email: null,
        auth_methods: [],
        balance_kopeks: 0,
        subscription: null,
        created_at: null,
        referrals_count: 0,
        recommended: true,
      },
      secondary: {
        id: 2,
        username: null,
        first_name: null,
        email: null,
        auth_methods: [],
        balance_kopeks: 0,
        subscription: null,
        created_at: null,
        referrals_count: 3,
        recommended: false,
      },
      expires_in_seconds: 1800,
    };
    expect(response.primary.recommended).toBe(true);
    expect(response.secondary.referrals_count).toBe(3);
  });
});
```

Run in `/Users/mihail/Desktop/Serv/bedolaga-cabinet`:
```bash
npx vitest run src/utils/mergeTypes.test.ts
```
Expected: FAIL (TypeScript error: `referrals_count` does not exist on type `MergeAccountPreview`).

- [ ] **Step 5.2: Update `MergeAccountPreview` type in `src/types/index.ts`**

In `src/types/index.ts`, replace the `MergeAccountPreview` interface (lines 833-842):

```typescript
export interface MergeAccountPreview {
  id: number;
  username: string | null;
  first_name: string | null;
  email: string | null;
  auth_methods: string[];
  balance_kopeks: number;
  subscription: MergeSubscriptionPreview | null;
  created_at: string | null;
  referrals_count: number;
  recommended: boolean;
}
```

- [ ] **Step 5.3: Update `executeMerge` in `src/api/auth.ts`**

Replace lines 349-360 in `src/api/auth.ts`:

```typescript
  executeMerge: async (
    mergeToken: string,
    keepAccount: number,
  ): Promise<MergeResponse> => {
    const response = await apiClient.post<MergeResponse>(
      `/cabinet/auth/merge/${encodeURIComponent(mergeToken)}`,
      {
        keep_account: keepAccount,
      },
    );
    return response.data;
  },
```

- [ ] **Step 5.4: Update `src/locales/ru.json` merge section**

In `src/locales/ru.json`, replace the entire `"merge": { ... }` object (currently at line 5755) with:

```json
  "merge": {
    "title": "Объединение аккаунтов",
    "description": "Этот способ входа уже привязан к другому аккаунту. Вы можете объединить два аккаунта в один.",
    "currentAccount": "Ваш текущий аккаунт",
    "foundAccount": "Найденный аккаунт",
    "authMethods": "Способы входа",
    "subscription": "Подписка",
    "noSubscription": "Нет подписки",
    "traffic": "Трафик",
    "devices": "Устройства",
    "balance": "Баланс",
    "referrals": "Рефералов",
    "until": "до {{date}}",
    "makeMain": "Сделать основным",
    "recommended": "Рекомендуем",
    "oneProfileWarning": "Основной аккаунт сохранится. Второй закроется, а подписка, баланс и рефералы переедут в основной. Входить сможешь любым способом — Telegram, Яндекс или почтой — попадёшь в один и тот же профиль.",
    "combinedSubscription": "Итоговая подписка: до {{date}}",
    "afterMerge": "После объединения",
    "allAuthMethodsMerged": "Все способы входа объединятся",
    "balanceSummed": "Баланс: {{amount}} ₽ (сумма)",
    "historyPreserved": "История операций сохранится",
    "confirm": "Объединить аккаунты",
    "cancel": "Отмена",
    "expired": "Время на объединение истекло. Попробуйте снова.",
    "success": "Аккаунты успешно объединены!",
    "error": "Ошибка при объединении. Попробуйте позже.",
    "expiresIn": "Действует ещё {{minutes}}",
    "merging": "Объединение..."
  }
```

Removed keys: `keepThisSubscription`, `unselectedSubscriptionDeleted`, `chooseSubscription`
Added keys: `makeMain`, `recommended`, `oneProfileWarning`, `combinedSubscription`, `referrals`

- [ ] **Step 5.5: Update `src/locales/en.json` merge section**

In `src/locales/en.json`, replace the `"merge": { ... }` object:

```json
  "merge": {
    "title": "Merge Accounts",
    "description": "This sign-in method is already linked to another account. You can merge both accounts into one.",
    "currentAccount": "Your current account",
    "foundAccount": "Found account",
    "authMethods": "Sign-in methods",
    "subscription": "Subscription",
    "noSubscription": "No subscription",
    "traffic": "Traffic",
    "devices": "Devices",
    "balance": "Balance",
    "referrals": "Referrals",
    "until": "until {{date}}",
    "makeMain": "Make primary",
    "recommended": "Recommended",
    "oneProfileWarning": "The primary account will be kept. The other account will be closed and its subscription, balance, and referrals will move to the primary account. You'll be able to sign in with any method — Telegram, Yandex, or email — and reach the same profile.",
    "combinedSubscription": "Combined subscription: until {{date}}",
    "afterMerge": "After merging",
    "allAuthMethodsMerged": "All sign-in methods will be combined",
    "balanceSummed": "Balance: {{amount}} ₽ (combined)",
    "historyPreserved": "Transaction history will be preserved",
    "confirm": "Merge Accounts",
    "cancel": "Cancel",
    "expired": "The merge link has expired. Please try again.",
    "success": "Accounts merged successfully!",
    "error": "Failed to merge accounts. Please try again later.",
    "expiresIn": "Expires in {{minutes}}",
    "merging": "Merging..."
  }
```

- [ ] **Step 5.6: Run type tests and vitest**

From `/Users/mihail/Desktop/Serv/bedolaga-cabinet`:

```bash
npx vitest run src/utils/mergeTypes.test.ts
```
Expected: PASS.

```bash
npx tsc --noEmit
```
Expected: exits 0.

```bash
npx vitest run
```
Expected: All tests PASS.

- [ ] **Step 5.7: Commit**

From `/Users/mihail/Desktop/Serv/bedolaga-cabinet`:

```bash
git add src/types/index.ts src/api/auth.ts src/locales/ru.json src/locales/en.json src/utils/mergeTypes.test.ts
git commit -m "feat(merge): update frontend types, API, and locales for keep_account

MergeAccountPreview gains referrals_count and recommended fields.
executeMerge now sends keep_account instead of keep_subscription_from.
Locale keys: added makeMain, recommended, oneProfileWarning,
combinedSubscription, referrals; removed keepThisSubscription,
unselectedSubscriptionDeleted, chooseSubscription."
```

---

### Task 6: Frontend `MergeAccounts.tsx` redesign

**Files:**
- Modify: `src/pages/MergeAccounts.tsx`

**Interfaces:**
- Consumes:
  - `MergeAccountPreview` with `referrals_count: number`, `recommended: boolean` (Task 5)
  - `authApi.executeMerge(mergeToken, keepAccount: number)` (Task 5)
  - Locale keys: `merge.makeMain`, `merge.recommended`, `merge.oneProfileWarning`, `merge.combinedSubscription`, `merge.referrals` (Task 5)
- Produces: Redesigned merge UI with survivor radio per card, recommendation badge, one-profile warning, combined end-date line

- [ ] **Step 6.1: Write a vitest for the survivor-radio payload logic**

Create `src/utils/mergeSurvivorLogic.test.ts`:

```typescript
import { describe, it, expect } from 'vitest';

/**
 * Tests the pure payload-building logic extracted from MergeAccounts:
 * selectedUserId -> keep_account value.
 */

function buildMergePayload(selectedUserId: number | null): { keep_account: number } | null {
  if (selectedUserId === null) return null;
  return { keep_account: selectedUserId };
}

function computeCombinedEndDate(
  primaryEnd: string | null | undefined,
  secondaryEnd: string | null | undefined,
  now: Date,
): Date | null {
  // Returns the combined end date when both subs are active.
  // Logic mirrors backend: winner keeps their end_date + loser's remaining days.
  // For display only — backend computes the authoritative value.
  if (!primaryEnd || !secondaryEnd) return null;
  const pDate = new Date(primaryEnd);
  const sDate = new Date(secondaryEnd);
  const winner = pDate > sDate ? pDate : sDate;
  const loser  = pDate > sDate ? sDate : pDate;
  const remaining = Math.max(0, loser.getTime() - now.getTime());
  return new Date(winner.getTime() + remaining);
}

describe('buildMergePayload', () => {
  it('returns keep_account with the selected user id', () => {
    expect(buildMergePayload(42)).toEqual({ keep_account: 42 });
  });

  it('returns null when nothing selected', () => {
    expect(buildMergePayload(null)).toBeNull();
  });
});

describe('computeCombinedEndDate', () => {
  const now = new Date('2026-07-25T12:00:00Z');

  it('returns null when primary has no sub', () => {
    expect(computeCombinedEndDate(null, '2026-09-01T00:00:00Z', now)).toBeNull();
  });

  it('adds loser remaining days to winner', () => {
    // winner ends Sep 1, loser ends Aug 10 (16 days remaining from Jul 25)
    const result = computeCombinedEndDate(
      '2026-09-01T00:00:00Z',
      '2026-08-10T00:00:00Z',
      now,
    );
    expect(result).not.toBeNull();
    // winner (Sep 1) + 16 days = Sep 17
    expect(result!.toISOString().startsWith('2026-09-17')).toBe(true);
  });

  it('no extension when loser already expired', () => {
    const result = computeCombinedEndDate(
      '2026-09-01T00:00:00Z',
      '2026-07-01T00:00:00Z',  // expired before now
      now,
    );
    // Combined = winner + 0 = Sep 1
    expect(result!.toISOString().startsWith('2026-09-01')).toBe(true);
  });
});
```

Run: `npx vitest run src/utils/mergeSurvivorLogic.test.ts`
Expected: PASS (pure functions, no imports from app code).

- [ ] **Step 6.2: Rewrite `MergeAccounts.tsx`**

Replace the entire file content with the redesigned implementation:

```tsx
import { useState, useEffect } from 'react';
import { useParams, useNavigate, Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { motion } from 'framer-motion';
import { authApi } from '../api/auth';
import { useAuthStore } from '../store/auth';
import { useToast } from '../components/Toast';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/data-display/Card';
import { Button } from '@/components/primitives/Button';
import { staggerContainer, staggerItem } from '@/components/motion/transitions';
import { cn } from '@/lib/utils';
import ProviderIcon from '../components/ProviderIcon';
import type { MergeAccountPreview } from '../types';

// -- Icons --

function WarningIcon({ className = 'h-5 w-5' }: { className?: string }) {
  return (
    <svg
      className={className}
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
      />
    </svg>
  );
}

function ClockIcon({ className = 'h-4 w-4' }: { className?: string }) {
  return (
    <svg
      className={className}
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z"
      />
    </svg>
  );
}

function CheckCircleIcon({ className = 'h-5 w-5' }: { className?: string }) {
  return (
    <svg
      className={className}
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
      />
    </svg>
  );
}

// -- Helpers --

function formatCountdown(seconds: number): string {
  const clamped = Math.max(0, seconds);
  const min = Math.floor(clamped / 60);
  const sec = clamped % 60;
  return `${min.toString().padStart(2, '0')}:${sec.toString().padStart(2, '0')}`;
}

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return '-';
  try {
    return new Date(dateStr).toLocaleDateString(undefined, {
      day: 'numeric',
      month: 'long',
      year: 'numeric',
    });
  } catch {
    return dateStr;
  }
}

function formatDateFromDate(date: Date): string {
  try {
    return date.toLocaleDateString(undefined, {
      day: 'numeric',
      month: 'long',
      year: 'numeric',
    });
  } catch {
    return date.toISOString();
  }
}

function formatBalance(kopeks: number): string {
  return Math.floor(kopeks / 100).toLocaleString();
}

/**
 * Compute a display-only combined end-date.
 * Winner = later end_date; extension = max(0, loser.end_date - now).
 * Backend computes the authoritative value; this is informational only.
 */
function computeCombinedEndDate(
  primaryEndStr: string | null | undefined,
  secondaryEndStr: string | null | undefined,
  survivorId: number | null,
  primaryId: number,
  secondaryId: number,
): Date | null {
  if (!primaryEndStr || !secondaryEndStr) return null;
  const pDate = new Date(primaryEndStr);
  const sDate = new Date(secondaryEndStr);
  const now = new Date();
  const winner = pDate > sDate ? pDate : sDate;
  const loser  = pDate > sDate ? sDate : pDate;
  const remaining = Math.max(0, loser.getTime() - now.getTime());
  // survivorId doesn't change the combined date (backend always picks later + remaining)
  void survivorId; void primaryId; void secondaryId;
  return new Date(winner.getTime() + remaining);
}

// -- Radio Indicator --

function RadioIndicator({ selected }: { selected: boolean }) {
  return (
    <div
      className={cn(
        'flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2 transition-colors',
        selected ? 'border-accent-500 bg-accent-500' : 'border-dark-500',
      )}
    >
      {selected && <div className="h-2 w-2 rounded-full bg-white" />}
    </div>
  );
}

// -- Account Card --

interface AccountCardProps {
  account: MergeAccountPreview;
  label: string;
  isSelected: boolean;
  onSelect: () => void;
}

function AccountCard({ account, label, isSelected, onSelect }: AccountCardProps) {
  const { t } = useTranslation();

  return (
    <Card className={cn('transition-colors', isSelected && 'border-accent-500/50')}>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <CardTitle>{label}</CardTitle>
          {account.recommended && (
            <span className="inline-flex items-center gap-1 rounded-md bg-accent-500/20 px-2 py-0.5 text-xs font-medium text-accent-400">
              ⭐ {t('merge.recommended')}
            </span>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Auth methods */}
        <div>
          <span className="text-sm text-dark-400">{t('merge.authMethods')}:</span>
          <div className="mt-1.5 flex flex-wrap gap-2">
            {account.auth_methods.map((method) => (
              <span
                key={method}
                className="inline-flex items-center gap-1.5 rounded-md bg-dark-800 px-2.5 py-1 text-xs text-dark-200"
              >
                <ProviderIcon provider={method} className="h-4 w-4" />
                {t(`profile.accounts.providers.${method}`)}
              </span>
            ))}
            {account.auth_methods.length === 0 && (
              <span className="text-xs text-dark-500">—</span>
            )}
          </div>
        </div>

        {/* Subscription */}
        {account.subscription ? (
          <div className="space-y-1">
            <span className="text-sm text-dark-400">{t('merge.subscription')}:</span>
            <p className="font-medium text-dark-100">
              {account.subscription.tariff_name ?? account.subscription.status}
            </p>
            {account.subscription.end_date && (
              <p className="text-sm text-dark-400">
                {t('merge.until', { date: formatDate(account.subscription.end_date) })}
              </p>
            )}
            <p className="text-sm text-dark-400">
              {t('merge.traffic')}: {account.subscription.traffic_limit_gb} GB
              {'  '}·{'  '}
              {t('merge.devices')}: {account.subscription.device_limit}
            </p>
          </div>
        ) : (
          <div>
            <span className="text-sm text-dark-400">{t('merge.subscription')}:</span>
            <p className="text-sm text-dark-500">{t('merge.noSubscription')}</p>
          </div>
        )}

        {/* Balance */}
        <div className="flex items-baseline gap-1.5">
          <span className="text-sm text-dark-400">{t('merge.balance')}:</span>
          <span className="font-medium text-dark-100">
            {formatBalance(account.balance_kopeks)} &#8381;
          </span>
        </div>

        {/* Referrals */}
        <div className="flex items-baseline gap-1.5">
          <span className="text-sm text-dark-400">{t('merge.referrals')}:</span>
          <span className="text-sm text-dark-100">{account.referrals_count}</span>
        </div>

        {/* Registration date */}
        {account.created_at && (
          <div className="flex items-baseline gap-1.5">
            <span className="text-sm text-dark-400">{t('profile.accounts.registeredAt', { defaultValue: 'Registered' })}:</span>
            <span className="text-sm text-dark-300">{formatDate(account.created_at)}</span>
          </div>
        )}

        {/* Survivor radio */}
        <button
          type="button"
          role="radio"
          aria-checked={isSelected}
          onClick={onSelect}
          className="mt-2 flex w-full items-center gap-2.5 rounded-lg bg-dark-800/50 px-3 py-2.5 text-left transition-colors hover:bg-dark-800"
        >
          <RadioIndicator selected={isSelected} />
          <span className="text-sm text-dark-200">{t('merge.makeMain')}</span>
        </button>
      </CardContent>
    </Card>
  );
}

// -- Loading Skeleton --

function LoadingSkeleton() {
  return (
    <motion.div
      className="space-y-6"
      variants={staggerContainer}
      initial="initial"
      animate="animate"
    >
      <motion.div variants={staggerItem}>
        <div className="flex items-center gap-3">
          <div className="h-7 w-7 animate-pulse rounded bg-dark-700" />
          <div className="h-7 w-48 animate-pulse rounded bg-dark-700" />
        </div>
      </motion.div>

      {Array.from({ length: 3 }).map((_, i) => (
        <motion.div key={i} variants={staggerItem}>
          <Card>
            <div className="space-y-4">
              <div className="h-5 w-40 animate-pulse rounded bg-dark-700" />
              <div className="h-4 w-64 animate-pulse rounded bg-dark-700" />
              <div className="h-4 w-48 animate-pulse rounded bg-dark-700" />
              <div className="h-4 w-32 animate-pulse rounded bg-dark-700" />
            </div>
          </Card>
        </motion.div>
      ))}

      <motion.div variants={staggerItem}>
        <div className="h-12 w-full animate-pulse rounded-xl bg-dark-700" />
      </motion.div>
    </motion.div>
  );
}

// -- Expired / Error States --

function ExpiredState() {
  const { t } = useTranslation();
  return (
    <motion.div
      className="flex min-h-[60vh] flex-col items-center justify-center space-y-6 px-4"
      variants={staggerContainer}
      initial="initial"
      animate="animate"
    >
      <motion.div variants={staggerItem}>
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-warning-500/20">
          <ClockIcon className="h-8 w-8 text-warning-400" />
        </div>
      </motion.div>
      <motion.div variants={staggerItem} className="text-center">
        <p className="text-lg font-medium text-dark-100">{t('merge.expired')}</p>
      </motion.div>
      <motion.div variants={staggerItem}>
        <Link to="/profile/accounts" className="text-sm text-accent-400 transition-colors hover:text-accent-300">
          {t('profile.accounts.goToAccounts')}
        </Link>
      </motion.div>
    </motion.div>
  );
}

function ErrorState() {
  const { t } = useTranslation();
  return (
    <motion.div
      className="flex min-h-[60vh] flex-col items-center justify-center space-y-6 px-4"
      variants={staggerContainer}
      initial="initial"
      animate="animate"
    >
      <motion.div variants={staggerItem}>
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-error-500/20">
          <WarningIcon className="h-8 w-8 text-error-400" />
        </div>
      </motion.div>
      <motion.div variants={staggerItem} className="text-center">
        <p className="text-lg font-medium text-dark-100">{t('merge.error')}</p>
      </motion.div>
      <motion.div variants={staggerItem}>
        <Link to="/profile/accounts" className="text-sm text-accent-400 transition-colors hover:text-accent-300">
          {t('profile.accounts.goToAccounts')}
        </Link>
      </motion.div>
    </motion.div>
  );
}

// -- Main Component --

export default function MergeAccounts() {
  const { t } = useTranslation();
  const { mergeToken } = useParams<{ mergeToken: string }>();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [selectedUserId, setSelectedUserId] = useState<number | null>(null);
  const [expiresIn, setExpiresIn] = useState(0);
  const [isExpired, setIsExpired] = useState(false);

  const { data, isLoading, error } = useQuery({
    queryKey: ['merge-preview', mergeToken],
    queryFn: () => {
      if (!mergeToken) return Promise.reject(new Error('Missing merge token'));
      return authApi.getMergePreview(mergeToken);
    },
    enabled: !!mergeToken,
    retry: false,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

  // Auto-select the recommended account when data loads (only once)
  useEffect(() => {
    if (!data) return;
    if (selectedUserId !== null) return;
    if (data.primary.recommended) {
      setSelectedUserId(data.primary.id);
    } else {
      setSelectedUserId(data.secondary.id);
    }
  }, [data, selectedUserId]);

  // Countdown timer
  useEffect(() => {
    if (!data) return;
    const startTime = Date.now();
    const totalSeconds = data.expires_in_seconds;
    const tick = () => {
      const elapsed = Math.floor((Date.now() - startTime) / 1000);
      const remaining = totalSeconds - elapsed;
      if (remaining <= 0) {
        setExpiresIn(0);
        setIsExpired(true);
        clearInterval(interval);
      } else {
        setExpiresIn(remaining);
      }
    };
    tick();
    const interval = setInterval(tick, 1000);
    return () => clearInterval(interval);
  }, [data]);

  const mergeMutation = useMutation({
    mutationFn: () => {
      if (!mergeToken || selectedUserId === null) {
        return Promise.reject(new Error('Missing merge token or user selection'));
      }
      return authApi.executeMerge(mergeToken, selectedUserId);
    },
    onSuccess: async (response) => {
      if (!response.success || !response.access_token || !response.refresh_token) {
        showToast({ type: 'error', message: t('merge.error') });
        return;
      }
      const { setTokens, setUser, checkAdminStatus } = useAuthStore.getState();
      setTokens(response.access_token, response.refresh_token);
      if (response.user) setUser(response.user);
      try { await checkAdminStatus(); } catch { /* non-critical */ }
      queryClient.clear();
      showToast({ type: 'success', message: t('merge.success') });
      navigate('/profile/accounts', { replace: true });
    },
    onError: () => {
      showToast({ type: 'error', message: t('merge.error') });
    },
  });

  const handleMerge = () => {
    if (selectedUserId === null || mergeMutation.isPending || isExpired) return;
    mergeMutation.mutate();
  };

  const handleCancel = () => navigate('/profile/accounts', { replace: true });

  // Derived state
  const bothHaveSubscriptions =
    data && !!data.primary.subscription && !!data.secondary.subscription;

  const combinedEndDate =
    bothHaveSubscriptions
      ? computeCombinedEndDate(
          data.primary.subscription?.end_date,
          data.secondary.subscription?.end_date,
          selectedUserId,
          data.primary.id,
          data.secondary.id,
        )
      : null;

  const combinedBalance = data ? data.primary.balance_kopeks + data.secondary.balance_kopeks : 0;
  const canConfirm = selectedUserId !== null && !isExpired && !mergeMutation.isPending;

  if (!mergeToken) return <ErrorState />;
  if (isLoading) return <LoadingSkeleton />;
  if (error || !data) return <ErrorState />;
  if (isExpired) return <ExpiredState />;

  return (
    <motion.div
      className="mx-auto max-w-lg space-y-6"
      variants={staggerContainer}
      initial="initial"
      animate="animate"
    >
      {/* Header */}
      <motion.div variants={staggerItem}>
        <Card className="border-warning-500/30 bg-warning-500/5">
          <div className="flex items-start gap-3">
            <WarningIcon className="mt-0.5 h-6 w-6 shrink-0 text-warning-400" />
            <div>
              <h1 className="text-xl font-bold text-dark-50">{t('merge.title')}</h1>
              <p className="mt-1 text-sm text-dark-400">{t('merge.description')}</p>
            </div>
          </div>
        </Card>
      </motion.div>

      {/* One-profile warning */}
      <motion.div variants={staggerItem}>
        <div className="rounded-xl border border-accent-500/30 bg-accent-500/10 px-4 py-3">
          <p className="text-sm text-accent-300">{t('merge.oneProfileWarning')}</p>
        </div>
      </motion.div>

      {/* Account cards */}
      <div role="radiogroup" aria-label={t('merge.makeMain')}>
        <motion.div variants={staggerItem}>
          <AccountCard
            account={data.primary}
            label={t('merge.currentAccount')}
            isSelected={selectedUserId === data.primary.id}
            onSelect={() => setSelectedUserId(data.primary.id)}
          />
        </motion.div>

        <motion.div variants={staggerItem} className="mt-6">
          <AccountCard
            account={data.secondary}
            label={t('merge.foundAccount')}
            isSelected={selectedUserId === data.secondary.id}
            onSelect={() => setSelectedUserId(data.secondary.id)}
          />
        </motion.div>
      </div>

      {/* Combined subscription date (when both have active subs) */}
      {bothHaveSubscriptions && combinedEndDate && (
        <motion.div variants={staggerItem}>
          <div className="rounded-xl border border-success-500/30 bg-success-500/10 px-4 py-3">
            <p className="text-sm font-medium text-success-400">
              {t('merge.combinedSubscription', { date: formatDateFromDate(combinedEndDate) })}
            </p>
          </div>
        </motion.div>
      )}

      {/* After merge summary */}
      <motion.div variants={staggerItem}>
        <Card>
          <CardHeader>
            <CardTitle>{t('merge.afterMerge')}</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="space-y-3">
              <li className="flex items-start gap-2.5">
                <CheckCircleIcon className="mt-0.5 h-4 w-4 shrink-0 text-success-400" />
                <span className="text-sm text-dark-200">{t('merge.allAuthMethodsMerged')}</span>
              </li>
              <li className="flex items-start gap-2.5">
                <CheckCircleIcon className="mt-0.5 h-4 w-4 shrink-0 text-success-400" />
                <span className="text-sm text-dark-200">
                  {t('merge.balanceSummed', { amount: formatBalance(combinedBalance) })}
                </span>
              </li>
              <li className="flex items-start gap-2.5">
                <CheckCircleIcon className="mt-0.5 h-4 w-4 shrink-0 text-success-400" />
                <span className="text-sm text-dark-200">{t('merge.historyPreserved')}</span>
              </li>
            </ul>
          </CardContent>
        </Card>
      </motion.div>

      {/* Confirm */}
      <motion.div variants={staggerItem}>
        <Button
          fullWidth
          disabled={!canConfirm}
          loading={mergeMutation.isPending}
          onClick={handleMerge}
        >
          {mergeMutation.isPending ? t('merge.merging') : t('merge.confirm')}
        </Button>
      </motion.div>

      {/* Cancel */}
      <motion.div variants={staggerItem} className="flex justify-center">
        <button
          type="button"
          onClick={handleCancel}
          className="text-sm text-dark-400 transition-colors hover:text-dark-200"
        >
          {t('merge.cancel')}
        </button>
      </motion.div>

      {/* Countdown */}
      <motion.div variants={staggerItem} className="flex items-center justify-center gap-1.5 pb-6">
        <ClockIcon className="h-4 w-4 text-dark-500" />
        <span className="text-sm text-dark-500">
          {t('merge.expiresIn', { minutes: formatCountdown(expiresIn) })}
        </span>
      </motion.div>
    </motion.div>
  );
}
```

- [ ] **Step 6.3: Run typecheck and build**

From `/Users/mihail/Desktop/Serv/bedolaga-cabinet`:

```bash
npx tsc --noEmit
```
Expected: exits 0. If errors appear: check that the `profile.accounts.registeredAt` key exists in locales — if not, add a fallback via `{ defaultValue: 'Registered' }` (already in the code above) or add the key to both locale files.

```bash
npm run build
```
Expected: exits 0.

- [ ] **Step 6.4: Run all frontend tests**

```bash
npx vitest run
```
Expected: All tests PASS, including the new `mergeSurvivorLogic.test.ts` and `mergeTypes.test.ts`.

- [ ] **Step 6.5: Commit**

From `/Users/mihail/Desktop/Serv/bedolaga-cabinet`:

```bash
git add src/pages/MergeAccounts.tsx src/utils/mergeSurvivorLogic.test.ts
git commit -m "feat(merge): redesign MergeAccounts UI — choose survivor, show referrals and combined date

Side-by-side cards each have a 'Сделать основным' radio button and a
recommended badge. One-profile warning replaces the old 'subscription
deleted' text. When both accounts have active subscriptions, a combined
end-date line appears below the cards. Referral count and registration
date shown per card. Removed old subscription-radio and chooseSubscription
prompt. Sends keep_account to the updated API."
```

---

## Self-Review

### 1. Spec coverage

| Spec requirement | Covered by |
|---|---|
| A1: `keep_account: int` in `MergeRequest` | T1 |
| A1: Validation `keep_account in {primary, secondary}` | T1 |
| A1: Role-swap when `keep_account == secondary` | T1 |
| A1: Response returns survivor's tokens | T1 |
| A1: Remove `keep_subscription_from` | T1 |
| A3: Both active → extend later-ending by remaining days | T2 |
| A3: NULL end_date (lifetime) wins, no extension | T2 |
| A3: Write `subscription_events` row with `extended_days`, `previous_end_date`, `new_end_date` | T2 |
| A3: Multi-tariff branch also combines | T2 |
| A4: Survivor holds ALL identities after merge | T3 |
| A4: Regression test both directions | T3 |
| A5: `referrals_count` in preview | T4 |
| A5: `recommended: bool` per side, exactly one True | T4 |
| A5: Priority rule (sub > referrals > balance > older) | T4 |
| A2: `MergeAccountPreview` adds `referrals_count`, `recommended` | T5 |
| A2: `executeMerge` sends `keep_account` | T5 |
| A2: Remove locale keys `keepThisSubscription`, `unselectedSubscriptionDeleted`, `chooseSubscription` | T5 |
| A2: Add locale keys `makeMain`, `recommended`, `oneProfileWarning`, `combinedSubscription`, `referrals` | T5 |
| A2: Cards show login methods, sub, balance, referrals, date, survivor radio | T6 |
| A2: ⭐ `recommended` badge | T6 |
| A2: One-profile warning banner | T6 |
| A2: Combined end-date when both have active subs | T6 |
| A2: Remove old subscription radio | T6 |

All spec items covered.

### 2. Placeholder scan

No TBD, TODO, "implement later", "fill in details", "add appropriate error handling", or "similar to Task N" phrases exist in the plan. All code blocks are complete.

### 3. Type/name consistency

- `keep_account: int` used in T1 (backend `MergeRequest`) and T5 (frontend `executeMerge`) — consistent.
- `referrals_count: int` used in T4 (`_build_user_preview`, `MergePreviewUser`) and T5 (`MergeAccountPreview`) — consistent.
- `recommended: bool` used in T4 (`_compute_recommended`, `MergePreviewUser`) and T5 (`MergeAccountPreview`) and T6 (`account.recommended`) — consistent.
- `_combine_subscription_end_dates(winner_sub, loser_sub, now)` defined in T2, called in T2 — consistent.
- `_compute_recommended(primary_preview, secondary_preview) -> tuple[bool, bool]` defined and called in T4, imported in tests — consistent.
- `_count_active_referrals(db, user_id) -> int` defined in T4, monkeypatched in tests — consistent.
- `survivor_id` / `absorbed_id` variable names in T1 (handler) map to `primary_user_id` / `secondary_user_id` in `execute_merge` — consistent; the swap happens at the call site.
- Frontend: `authApi.executeMerge(mergeToken, selectedUserId)` — `selectedUserId` is `number`, `keepAccount: number` parameter name in T5 — consistent.
- `merge.makeMain`, `merge.recommended`, `merge.oneProfileWarning`, `merge.combinedSubscription`, `merge.referrals` — used in T5 (locale) and T6 (component) — consistent.

### Spec ambiguities resolved

1. **Multi-tariff combine vs. single-tariff combine**: The spec says "Apply in both single- and multi-tariff branches". The multi-tariff branch previously resolved conflicts by picking one winner (setting loser to 'expired'). This plan extends that: in T2 it also extends the winner's `end_date` by the loser's remaining days before expiring the loser, exactly mirroring single-tariff behavior. The loser sub is still transferred to primary (as expired) so the record exists for audit; its RemnaWave user is deferred for deletion.

2. **What to do with the loser subscription record**: The spec says "Loser's RemnaWave user is deleted (deferred after commit, as now)". The database subscription record for the loser is not deleted — it's marked `expired` and transferred to the survivor (consistent with the multi-tariff pattern). This avoids losing purchase history.

3. **`_combine_subscription_end_dates` with `loser.end_date = None`**: The spec states "NULL end_date (lifetime) always wins" — so the loser can never be lifetime (caller picks the later end_date as winner). If somehow both are lifetime, the plan's guard returns `timedelta(0)`, which is correct.

4. **Combined end-date display in frontend**: The spec says "show combined end-date when both have active subs". The plan computes this client-side from the preview data as a display value; the backend computes the authoritative value at execute time. This is clearly informational only, matching what the spec intends.

5. **`subscriptions_count` field**: It's in `_build_user_preview` output but not in `MergePreviewUser` schema (not serialized to frontend). This plan leaves it as-is — the field exists in the dict but Pydantic drops extra fields. No spec requirement to expose it.
