# Broadcast-by-Client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let admins send broadcasts targeted to users by their VPN client app (Happ, v2rayNG, …), backed by a synced `user_clients` table.

**Architecture:** Periodically bulk-pull RemnaWave HWID devices, parse the client app from each device's `userAgent`, and upsert a `user_clients` (user_id, app_name) mapping. The broadcast targeting reads this table to list apps with recipient counts and to resolve recipients for a `client:<app>` target. Sync runs daily in the monitoring scheduler plus an on-demand admin endpoint.

**Tech Stack:** Python 3.13, SQLAlchemy async, Alembic, FastAPI; React+TS (cabinet).

## Global Constraints

- Bot verification: `.venv/bin/python -m py_compile <files>` + import/symbol tests + unit tests for pure functions. No reliable pytest-asyncio suite.
- Cabinet verification: `npm run type-check` + `npm run build`.
- Commits: descriptive (title + body explaining what & why), NO `Co-Authored-By`. Direct push to `main` authorized (both repos).
- Client app derived AUTOMATICALLY from UA prefix (no curated dictionary). Raw UA stored for later.
- Current Alembic head: `9019`.
- Panel mapping: device `userUuid` → `User.remnawave_uuid` (fallback `Subscription.remnawave_uuid` → user_id).

---

### Task 1: `user_clients` model + migration

**Files:**
- Modify: `app/database/models.py` (new `UserClient` model)
- Create: migration in the alembic versions dir (head `9019` → `9020`)

**Interfaces:**
- Produces table `user_clients(id, user_id FK users.id, app_name String(64), last_seen_at DateTime|null, updated_at DateTime)`, UNIQUE `(user_id, app_name)`, index on `app_name`.

- [ ] **Step 1:** Add `UserClient` model near other user-related models, following the file's column/type conventions (use `AwareDateTime` like neighbors; `Column`, `String`, `Integer`, `ForeignKey`). UNIQUE constraint on `(user_id, app_name)`, index on `app_name`.
- [ ] **Step 2:** Create migration `down_revision='9019'` (next id `9020` per their scheme): `op.create_table('user_clients', ...)` with the columns + unique constraint + index; `downgrade` drops the table. Match datetime type used by neighbor migrations (`sa.DateTime(timezone=True)`).
- [ ] **Step 3:** Verify `.venv/bin/python -m py_compile app/database/models.py <migration>` → OK; `.venv/bin/alembic heads` shows single head `9020`.
- [ ] **Step 4:** Commit `feat(db): user_clients table (user→client app) + migration` with body.

---

### Task 2: `parse_client_app` UA parser

**Files:**
- Create: `app/utils/client_detect.py`
- Create: `scripts/_test_parse_client_app.py`

**Interfaces:**
- Produces: `def parse_client_app(user_agent: str | None) -> str` — returns display app name (UA prefix before `/`, `(`, or whitespace), `'Unknown'` if empty.

- [ ] **Step 1:** Implement:
```python
def parse_client_app(user_agent: str | None) -> str:
    if not user_agent:
        return 'Unknown'
    s = user_agent.strip()
    for sep in ('/', '(', ' '):
        i = s.find(sep)
        if i > 0:
            s = s[:i]
            break
    s = s.strip()
    return s or 'Unknown'
```
- [ ] **Step 2:** Test script asserts: `'Happ/1.2 (iPhone)'→'Happ'`, `'v2rayNG/1.9.5'→'v2rayNG'`, `'Streisand'→'Streisand'`, `''→'Unknown'`, `None→'Unknown'`, `'  Hiddify/2 '→'Hiddify'`. Run `.venv/bin/python scripts/_test_parse_client_app.py`, all PASS.
- [ ] **Step 3:** Commit `feat(client-detect): parse_client_app from UA prefix` with body.

---

### Task 3: `sync_user_clients` service + config

**Files:**
- Create: `app/services/client_sync_service.py`
- Modify: `app/config.py` (flags)

**Interfaces:**
- Consumes: `parse_client_app` (Task 2), `UserClient` (Task 1), `RemnaWaveAPI.get_all_hwid_devices()`.
- Produces: `async def sync_user_clients(db) -> dict` returning `{'devices': int, 'users': int, 'apps': int}`; module-level last-sync timestamp accessor `get_last_client_sync() -> datetime | None`.

- [ ] **Step 1:** Add config: `CLIENT_SYNC_ENABLED: bool = True`, `CLIENT_SYNC_INTERVAL_HOURS: int = 24` near other feature flags in `config.py`.
- [ ] **Step 2:** Implement `sync_user_clients(db)`:
  - Get a configured panel client via the project pattern (`SubscriptionService().get_api_client()` async ctx — confirm against `app/services/subscription_service.py:160`).
  - Page through `get_all_hwid_devices()` (read its return shape — `{'devices' or 'records': [...], 'total': ...}`; confirm key names against `remnawave_api.py:1213`).
  - Build `panel_uuid → user_id` map up front: one query over `User.remnawave_uuid` (+ a `Subscription.remnawave_uuid → user_id` fallback map) — no N+1.
  - For each device: `app = parse_client_app(d.get('userAgent'))`; resolve user_id; collect `{(user_id, app): max(updatedAt/createdAt)}`.
  - Upsert into `user_clients` (update `last_seen_at`, `updated_at`); DELETE rows for user_ids seen in this sync that are no longer present (prune stale apps). Use a single transaction or batched commits; wrap panel calls in try/except (log, continue).
  - Store last-sync timestamp (module-level var is fine for MVP, or a row in an existing system-state mechanism — pick the simplest already present).
- [ ] **Step 3:** Verify `py_compile` + import the module (bypass heavy `__init__` via importlib if Crypto/DB import blocks). Confirm `sync_user_clients` and `get_last_client_sync` exist.
- [ ] **Step 4:** Commit `feat(client-sync): sync_user_clients from panel HWID devices into user_clients` with body.

---

### Task 4: Scheduler daily wiring + manual endpoint

**Files:**
- Modify: `app/services/monitoring_service.py` (daily call)
- Modify: `app/cabinet/routes/admin_broadcasts.py` (manual sync endpoint)

**Interfaces:**
- Consumes: `sync_user_clients`, `get_last_client_sync`.

- [ ] **Step 1:** In the monitoring loop, add a once-per-`CLIENT_SYNC_INTERVAL_HOURS` call to `sync_user_clients(db)` gated by `CLIENT_SYNC_ENABLED` — follow the existing "run X only every N" pattern in this file (there is already an hourly `_last_cleanup` timestamp pattern; mirror it with a `_last_client_sync` field). Wrap in the file's `_run_monitoring_task` error isolation so a failure doesn't break the loop.
- [ ] **Step 2:** Add endpoint `POST /cabinet/admin/broadcasts/clients/sync` (permission `broadcasts:read`, matching neighboring routes) → runs `sync_user_clients(db)`, returns `{synced: {...}, last_sync_at}`. Guard against concurrent runs with a simple in-process lock/flag.
- [ ] **Step 3:** Verify `py_compile` both files + import the router module.
- [ ] **Step 4:** Commit `feat(client-sync): daily scheduler run + manual admin sync endpoint` with body.

---

### Task 5: Broadcast `client` filter (backend)

**Files:**
- Modify: `app/cabinet/routes/admin_broadcasts.py` (filters response + recipient resolution + preview/create/send)
- Modify: `app/cabinet/schemas/broadcasts.py` (filter item / request fields if needed)

**Interfaces:**
- Consumes: `user_clients` table.
- Produces: `client_filters: [{app_name, recipient_count, last_sync_at}]` in the filters response; new broadcast target `client:<app_name>` (or `client_app` field) honored by preview/create/send.

- [ ] **Step 1:** In `GET /cabinet/admin/broadcasts/filters`, add `client_filters`: query distinct `app_name` from `user_clients` with a count of users that ALSO satisfy the TG-channel base condition (has `telegram_id`, status active/not-blocked — reuse the exact base predicate used by existing filters). Include `last_sync_at` from `get_last_client_sync()`.
- [ ] **Step 2:** Add a recipient-resolution branch for target `client:<app>`: select users `JOIN user_clients ON user_id WHERE app_name=<app>` + the same base channel predicate. Mirror how existing targets build their user query (read the current resolution helper before editing).
- [ ] **Step 3:** Wire `POST /preview`, `POST /broadcasts`, `POST /broadcasts/send` to accept and count/resolve the `client:<app>` target. Add the schema field if the request model needs it (`broadcasts.py`).
- [ ] **Step 4:** Verify `py_compile` + import; sanity-check the SQL builds (no syntax error) via import.
- [ ] **Step 5:** Commit `feat(broadcast): target users by client app (client_filters + client:<app> target)` with body.

---

### Task 6: Cabinet UI — client filter + refresh button

**Files:**
- Modify: `bedolaga-cabinet/src/api/adminBroadcasts.ts` (types + sync call)
- Modify: `bedolaga-cabinet/src/pages/AdminBroadcastCreate.tsx`
- Modify: `bedolaga-cabinet/src/locales/{ru,en,fa,zh}.json`

**Interfaces:**
- Consumes: filters `client_filters` + `POST .../clients/sync` from Task 4/5.

- [ ] **Step 1:** Add types for `client_filters` and an API method `syncClients()` → `POST /cabinet/admin/broadcasts/clients/sync`. Extend the filters type.
- [ ] **Step 2:** In `AdminBroadcastCreate.tsx`, render a "По клиенту" filter group from `client_filters` (app name + recipient count), selectable as the broadcast target (set target = `client:<app>`). Reuse the existing filter-group markup/pattern (tariff_filters block).
- [ ] **Step 3:** Add a "Обновить список клиентов" button → calls `syncClients()`, shows spinner, refetches filters on success, displays `last_sync_at` ("Обновлено: …").
- [ ] **Step 4:** Add i18n keys (`admin.broadcasts.clientFilter.*`) to all 4 locales (ru real; en/fa/zh per convention).
- [ ] **Step 5:** Verify `npm run type-check` + `npm run build`.
- [ ] **Step 6:** Commit `feat(broadcast-ui): targeting by client app + refresh button` with body.

---

## Self-Review
- Spec coverage: §1 data→T1; §2 sync→T3; §3 parser→T2; §4 scheduler+manual→T4; §5 filter backend→T5; §6 UI→T6; §7 bounds honored (no user-card, multi-app via rows, prune stale). All covered.
- Placeholders: real shapes to confirm against code flagged explicitly (get_all_hwid_devices return keys, filters base predicate, scheduler pattern) — not assumptions baked in.
- Type consistency: `parse_client_app -> str`, `sync_user_clients(db) -> dict`, `user_clients(user_id, app_name)`, target `client:<app>` used identically across T2–T6.
