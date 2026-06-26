# Flexible Auto-Renewal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make subscription auto-renewal reliable and balance-aware: renew the longest period the balance covers (mode-aware, device-correct), retry every 3h within the last 3 days, fix the silent-skip bugs, and surface renewal status in the admin user card.

**Architecture:** Add a pure-ish selection helper on top of the existing `pricing_engine.calculate_renewal_price()` (which already routes classic vs tariff and includes device cost). Wire it into the existing monitoring mechanism A (and fix mechanism B), gate attempts by a new persisted `last_autopay_attempt_at`, persist the attempt outcome on `Subscription`, expose it via the admin API, render it in the cabinet card.

**Tech Stack:** Python 3.13, SQLAlchemy (async), Alembic, aiogram, FastAPI; React+TS (cabinet).

## Global Constraints

- Bot verification: `python3 -m py_compile <changed .py>` (use repo `.venv/bin/python`) + import/register tests. No reliable pytest-asyncio harness — do NOT rely on full `pytest` suite.
- Cabinet verification: `npm run type-check` + `npm run build`.
- Commits: NO `Co-Authored-By` trailer. Direct push to `main` is authorized for both repos.
- Pricing single source of truth: `pricing_engine.calculate_renewal_price(db, subscription, period_days, user=user)`. Never recompute device/period price outside it.
- Period sources: classic → `settings.AVAILABLE_RENEWAL_PERIODS`; tariff → `tariff.get_available_periods()`.
- No `autopay_period_days` cap — always pick the maximum affordable period.

---

### Task 1: Subscription columns + Alembic migration

**Files:**
- Modify: `app/database/models.py` (Subscription class, near existing autopay fields ~2123-2129)
- Create: `alembic/versions/<rev>_autopay_attempt_fields.py` (match existing migration style/numbering)

**Interfaces:**
- Produces columns on `subscriptions`: `last_autopay_attempt_at: DateTime|None`, `last_autopay_status: str|None`, `last_autopay_error: str|None`, `last_autopay_renewed_at: DateTime|None`, `last_autopay_period_days: int|None`.

- [ ] **Step 1:** Add columns to the `Subscription` model after `auto_renewed_before_expiry`:
```python
last_autopay_attempt_at = Column(AwareDateTime(), nullable=True)
last_autopay_status = Column(String(32), nullable=True)  # success|insufficient_balance|error|skipped
last_autopay_error = Column(String(512), nullable=True)
last_autopay_renewed_at = Column(AwareDateTime(), nullable=True)
last_autopay_period_days = Column(Integer, nullable=True)
```
- [ ] **Step 2:** Create the Alembic migration. Inspect an existing file in `alembic/versions/` for `down_revision` chaining and `AwareDateTime`/types usage; add `op.add_column('subscriptions', ...)` for each (all nullable, no backfill), and matching `op.drop_column` in `downgrade`.
- [ ] **Step 3:** Verify: `.venv/bin/python -m py_compile app/database/models.py alembic/versions/<rev>_*.py` → OK. Confirm `down_revision` equals the current head (`.venv/bin/alembic heads` if available, else read the latest existing revision).
- [ ] **Step 4:** Commit: `feat(db): autopay attempt/result fields on subscriptions + migration`.

---

### Task 2: `select_affordable_renewal` helper + device-charge audit

**Files:**
- Modify: `app/services/pricing_engine.py` (add method on the engine class)
- Create: `scripts/_test_select_affordable_renewal.py` (standalone logic test, run once, may delete after)
- Audit: `app/services/monitoring_service.py` autopay charge path + `app/services/subscription_auto_purchase_service.py`

**Interfaces:**
- Produces: `async def select_affordable_renewal(self, db, subscription, user) -> tuple[int, int] | None` returning `(period_days, price_kopeks)` for the longest affordable period, or `None`.

- [ ] **Step 1:** Implement the helper. Period source by mode; iterate descending; price via existing `calculate_renewal_price`:
```python
async def select_affordable_renewal(self, db, subscription, user):
    if subscription.tariff_id is not None and subscription.tariff is not None:
        periods = sorted(subscription.tariff.get_available_periods(), reverse=True)
    else:
        periods = sorted(settings.AVAILABLE_RENEWAL_PERIODS, reverse=True)
    balance = user.balance_kopeks or 0
    for period in periods:
        if not isinstance(period, int) or period <= 0:
            continue
        pricing = await self.calculate_renewal_price(db, subscription, period, user=user)
        price = pricing.total_kopeks  # confirm RenewalPricing total field name
        if price <= balance:
            return period, price
    return None
```
Confirm the actual total field on `RenewalPricing` (read the dataclass) and the accessor for `AVAILABLE_RENEWAL_PERIODS` (likely `settings.AVAILABLE_RENEWAL_PERIODS` list or a getter).
- [ ] **Step 2:** Logic test (mock pricing) in the script: monkeypatch `calculate_renewal_price` to return prices `{30:20000,90:51000,180:96000,360:180000}`; assert balance 51000 → `(90, 51000)`; balance 30000 → `(30, 20000)`; balance 10000 → `None`; balance 200000 → `(360, 180000)`. Run with `.venv/bin/python scripts/_test_select_affordable_renewal.py`, expect all asserts pass.
- [ ] **Step 3:** **Device-charge audit.** Trace the actual balance deduction in mechanism A (`monitoring_service.py` around the `subtract_user_balance` call) and mechanism B. Confirm the amount deducted equals `calculate_renewal_price(...).total_kopeks` for the chosen period. If any path recomputes device/period price separately (e.g. multiplies devices again), replace it with the engine value. Document findings in the commit body.
- [ ] **Step 4:** Verify py_compile of changed files. Commit: `feat(pricing): select_affordable_renewal + ensure renewal charge == engine price (devices)`.

---

### Task 3: Mechanism A — 3h cadence, hour-accurate window, flexible period, persist outcome

**Files:**
- Modify: `app/services/monitoring_service.py` (`_process_autopayments` ~1415-1770)

**Interfaces:**
- Consumes: `pricing_engine.select_affordable_renewal`, `Subscription.last_autopay_*` (Task 1, 2).

- [ ] **Step 1:** Replace the integer-day window check (`days_before_expiry = (sub.end_date - current_time).days`) with hour-accurate: compute `hours_left = (sub.end_date - current_time).total_seconds()/3600`; enter when `0 < hours_left <= (min(sub.autopay_days_before or 3, 3) * 24)`.
- [ ] **Step 2:** Add 3h throttle: skip if `sub.last_autopay_attempt_at` is set and `current_time - sub.last_autopay_attempt_at < timedelta(hours=3)`. (Keep the in-memory `_notified_users` only for success dedup; the 3h gate replaces per-tick retry spam.)
- [ ] **Step 3:** Replace fixed-period selection with `period, price = await pricing_engine.select_affordable_renewal(db, sub, user)`. If `None` → set `last_autopay_status='insufficient_balance'`, `last_autopay_attempt_at=now`, keep existing fail-notification logic, continue.
- [ ] **Step 4:** On each attempt set `last_autopay_attempt_at=now`. On success set `last_autopay_status='success'`, `last_autopay_renewed_at=now`, `last_autopay_period_days=period`, `last_autopay_error=None`. On exception set `last_autopay_status='error'`, `last_autopay_error=str(exc)[:512]`. Commit these on the subscription.
- [ ] **Step 5:** Verify: py_compile + import-test that `monitoring_service` imports and `_process_autopayments` exists. Commit: `feat(autopay): 3h cadence, hour-accurate window, flexible period, persisted outcome`.

---

### Task 4: Mechanism B fix + ENABLE_AUTOPAY default

**Files:**
- Modify: `app/services/monitoring_service.py` (`_process_auto_renew_before_expiry` ~1808-1892)
- Modify: `app/config.py` (`ENABLE_AUTOPAY` default ~443)

**Interfaces:**
- Consumes: `select_affordable_renewal`.

- [ ] **Step 1:** Replace hardcoded `30` in `_process_auto_renew_before_expiry` with `select_affordable_renewal(db, subscription, user)`; if `None` → skip (insufficient), do not charge. Remove the `calculate_renewal_price(..., 30, ...)` call.
- [ ] **Step 2:** First confirm the real reset behaviour of `auto_renewed_before_expiry` (grep all writes). Ensure it is reset to `False` whenever a subscription is successfully extended (so B works each cycle). If `extend_subscription` does not reset it, add the reset there.
- [ ] **Step 3:** Idempotency: since mechanism A now covers the 3-day window, ensure B does not double-charge — B should no-op if the subscription was already renewed this cycle (e.g. `last_autopay_renewed_at` within current period / end_date already advanced).
- [ ] **Step 4:** Change `ENABLE_AUTOPAY` default to `True` in `config.py`; update `.env.example` if present.
- [ ] **Step 5:** Verify py_compile + import-test. Commit: `fix(autopay): mechanism B uses flexible period, guard reset, idempotency; ENABLE_AUTOPAY default on`.

---

### Task 5: Admin API — expose autopay fields

**Files:**
- Modify: `app/cabinet/schemas/users.py` (`UserSubscriptionInfo` ~54-72)
- Modify: `app/cabinet/routes/admin_users.py` (`get_user_detail` ~674-817 serialization)

**Interfaces:**
- Produces JSON fields on the subscription object: `autopay_days_before`, `last_autopay_attempt_at`, `last_autopay_status`, `last_autopay_renewed_at`, `last_autopay_period_days`.

- [ ] **Step 1:** Add the fields to `UserSubscriptionInfo` (Optional, default None), matching existing field style.
- [ ] **Step 2:** Populate them in `get_user_detail` where `UserSubscriptionInfo` is built (read from the subscription object).
- [ ] **Step 3:** Verify py_compile + import the router module. Commit: `feat(api): expose autopay attempt/result in admin user detail`.

---

### Task 6: Cabinet UI — autopay block in admin user card

**Files:**
- Modify: `bedolaga-cabinet/src/types/index.ts` (Subscription type — add the new optional fields)
- Modify: `bedolaga-cabinet/src/pages/AdminUserDetail.tsx` (subscription details block ~2233) or `src/components/admin/userDetail/SubscriptionTab.tsx`
- Modify: `bedolaga-cabinet/src/locales/{ru,en,fa,zh}.json` (labels)

**Interfaces:**
- Consumes API fields from Task 5.

- [ ] **Step 1:** Add optional fields to the `Subscription` TS type: `last_autopay_attempt_at?: string|null`, `last_autopay_status?: string|null`, `last_autopay_renewed_at?: string|null`, `last_autopay_period_days?: number|null`.
- [ ] **Step 2:** Add an "Автопродление" row to the subscription details grid: badge enabled/disabled (`autopay_enabled`) + `за N дней` (`autopay_days_before`); result line mapping `last_autopay_status` → ✅ продлено (`last_autopay_renewed_at` + `last_autopay_period_days`д) / ⚠️ недостаточно баланса / ❌ ошибка / — нет попыток (with `last_autopay_attempt_at`). Reuse a sibling row's markup/classes.
- [ ] **Step 3:** Add i18n keys (e.g. `admin.users.detail.autopay.*`) to all 4 locales (ru real, en/fa/zh per existing fallback convention).
- [ ] **Step 4:** Verify `npm run type-check` + `npm run build`. Commit: `feat(admin-ui): autopay status & last renewal in user card`.

---

## Self-Review

- Spec coverage: §1 flexible→Task2/3/4; §2 device-charge→Task2; §3 cadence→Task3; §4 bugs→Task3/4; §5 persistence→Task1/3/4; §6 API→Task5; §7 UI→Task6; §8 migration→Task1. All covered.
- Placeholders: field name `RenewalPricing.total_kopeks` and `settings.AVAILABLE_RENEWAL_PERIODS` accessor are flagged to confirm against real code in Task 2 Step 1 (not assumptions baked in).
- Type consistency: `select_affordable_renewal -> (period_days, price_kopeks)` used identically in Tasks 3 & 4; the 5 column names match across Tasks 1, 3, 4, 5, 6.
