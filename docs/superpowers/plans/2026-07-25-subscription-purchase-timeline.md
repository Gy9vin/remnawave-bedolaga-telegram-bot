# Хронология подписки — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Показать пользователю и админу наглядную историю покупок/продлений подписки (дата → тариф → простой/остаток → дата окончания) из `subscription_events`, у админа + кнопка «Скопировать» (Подробно/Компактно).

**Architecture:** Единый расчёт на бэке (CRUD `get_subscription_purchase_timeline`), два FastAPI-входа (self + admin), фронт-секция во вкладке «Подписка» юзера и админа + чистые форматтеры для копируемого текста.

**Tech Stack:** Python/SQLAlchemy async/FastAPI (репо `remnawave-bedolaga-telegram-bot`); React/TS/vitest (репо `bedolaga-cabinet`).

## Global Constraints

- Два репо: бэкенд `/Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot`, фронт `/Users/mihail/Desktop/Serv/bedolaga-cabinet`.
- Коммиты описательные (заголовок+тело), БЕЗ trailer `Co-Authored-By`.
- Бэкенд: тесты и py_compile через `.venv/bin/python3` (системный python3 = 3.9).
- Источник данных — только `subscription_events`, типы `purchase/renewal/activation`.
- Даты в ответе — ISO-строки (форматирует фронт). Длительности — целые секунды.
- Роуты кабинета под префиксом `/cabinet`.

---

## Task 1: CRUD `get_subscription_purchase_timeline`

**Files:**
- Modify: `app/database/crud/subscription_event.py`
- Test: `tests/crud/test_subscription_timeline.py`

**Interfaces:**
- Produces: `get_subscription_purchase_timeline(db: AsyncSession, user_id: int) -> list[dict]` — список строк по возрастанию даты, каждая: `{index:int, event_type:str, date:str(ISO), period_days:int|None, amount_kopeks:int|None, prev_end:str|None, new_end:str|None, downtime_seconds:int|None, carried_seconds:int|None}`.

- [ ] **Step 1: Failing test**
```python
# tests/crud/test_subscription_timeline.py
import sys as _sys
_sys.modules.pop('aiosqlite', None)
import aiosqlite as _a  # noqa: F401
_sys.modules['aiosqlite'] = _a

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import JSON, select  # noqa: F401
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database.models import SubscriptionEvent, User
from app.database.crud.subscription_event import get_subscription_purchase_timeline


def _patch_jsonb():
    for model in (User, SubscriptionEvent):
        for col in list(model.__table__.columns):
            if isinstance(col.type, JSONB):
                col.type = JSON()


@pytest_asyncio.fixture
async def session():
    _patch_jsonb()
    engine = create_async_engine('sqlite+aiosqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(User.__table__.create)
        await conn.run_sync(SubscriptionEvent.__table__.create)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _ev(s, uid, etype, at, **extra):
    s.add(SubscriptionEvent(user_id=uid, event_type=etype, occurred_at=at, extra=extra or None))
    await s.commit()


@pytest.mark.asyncio
async def test_timeline_downtime_and_carry(session):
    s = session
    s.add(User(id=1, referral_code='r1', balance_kopeks=0))
    await s.commit()
    base = datetime(2026, 3, 21, 2, 42, tzinfo=UTC)
    # 1) первая покупка 30 дней -> конец base+30
    await _ev(s, 1, 'purchase', base, period_days=30)
    # 2) покупка через ~33 дня (после конца) -> простой, отсчёт заново
    await _ev(s, 1, 'purchase', base + timedelta(days=33, hours=10), period_days=30)
    # 3) покупка ДО конца (renewal с авторитетными датами) -> остаток учтён
    p_end = (base + timedelta(days=33, hours=10) + timedelta(days=30))
    await _ev(s, 1, 'renewal', p_end - timedelta(days=2, hours=13), period_days=30,
              previous_end_date=p_end.isoformat(),
              new_end_date=(p_end + timedelta(days=30)).isoformat())

    rows = await get_subscription_purchase_timeline(s, 1)
    assert len(rows) == 3
    assert rows[0]['index'] == 1 and rows[0]['downtime_seconds'] is None and rows[0]['carried_seconds'] is None
    assert rows[0]['new_end'] == (base + timedelta(days=30)).isoformat()
    # событие 2 — простой (>0), отсчёт от даты покупки
    assert rows[1]['downtime_seconds'] and rows[1]['downtime_seconds'] > 0
    assert rows[1]['carried_seconds'] is None
    # событие 3 — остаток (carried>0), новая дата из авторитетного new_end_date
    assert rows[2]['carried_seconds'] and rows[2]['carried_seconds'] > 0
    assert rows[2]['new_end'] == (p_end + timedelta(days=30)).isoformat()


@pytest.mark.asyncio
async def test_timeline_empty(session):
    session.add(User(id=2, referral_code='r2', balance_kopeks=0))
    await session.commit()
    assert await get_subscription_purchase_timeline(session, 2) == []
```

- [ ] **Step 2: Run → fail**
`.venv/bin/python3 -m pytest tests/crud/test_subscription_timeline.py -v` → FAIL (ImportError). Если `_add`-модели требуют NOT-NULL колонок — доставь минимальные значения в тесте, ассерты не меняй.

- [ ] **Step 3: Implement** — в `app/database/crud/subscription_event.py` добавить (импорты `timedelta` и `datetime` уже есть в файле; `select` тоже):
```python
_TIMELINE_EVENT_TYPES = ('purchase', 'renewal', 'activation')


def _parse_iso_aware(value):
    """Parse an ISO datetime string to an aware datetime (UTC if naive)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


async def get_subscription_purchase_timeline(db: AsyncSession, user_id: int) -> list[dict]:
    """Хронология покупок/продлений для пользователя из subscription_events.

    Для renewal берём авторитетные previous_end_date/new_end_date из extra;
    для purchase/activation дату окончания реконструируем: если покупка после
    прошлого конца — отсчёт заново от даты покупки (был простой), иначе — плюсуем
    к остатку. Возвращает строки, отсортированные по дате (ISO), с секундами
    простоя/остатка (или None).
    """
    result = await db.execute(
        select(SubscriptionEvent)
        .where(
            SubscriptionEvent.user_id == user_id,
            SubscriptionEvent.event_type.in_(_TIMELINE_EVENT_TYPES),
        )
        .order_by(SubscriptionEvent.occurred_at.asc(), SubscriptionEvent.id.asc())
    )
    events = result.scalars().all()

    rows: list[dict] = []
    running_end = None
    for idx, ev in enumerate(events, start=1):
        extra = ev.extra or {}
        period_days = extra.get('period_days')
        if period_days is None and ev.event_type == 'activation':
            period_days = extra.get('trial_duration_days')

        date = _aware(ev.occurred_at)
        prev_end = running_end

        new_end_iso = extra.get('new_end_date')
        if new_end_iso:
            new_end = _parse_iso_aware(new_end_iso)
            prev_end_eff = _parse_iso_aware(extra.get('previous_end_date')) or prev_end
        else:
            days = int(period_days or 0)
            base = date if (prev_end is None or date >= prev_end) else prev_end
            new_end = base + timedelta(days=days)
            prev_end_eff = prev_end

        downtime_seconds = None
        carried_seconds = None
        if prev_end_eff is not None and date is not None:
            if date > prev_end_eff:
                downtime_seconds = int((date - prev_end_eff).total_seconds())
            elif date < prev_end_eff:
                carried_seconds = int((prev_end_eff - date).total_seconds())

        rows.append({
            'index': idx,
            'event_type': ev.event_type,
            'date': date.isoformat() if date else None,
            'period_days': int(period_days) if period_days is not None else None,
            'amount_kopeks': ev.amount_kopeks,
            'prev_end': prev_end_eff.isoformat() if prev_end_eff else None,
            'new_end': new_end.isoformat() if new_end else None,
            'downtime_seconds': downtime_seconds,
            'carried_seconds': carried_seconds,
        })
        running_end = new_end or running_end

    return rows
```
Проверить, что `datetime, UTC, timedelta` импортированы вверху файла (там есть `from datetime import UTC, datetime`; добавить `timedelta`, если нет).

- [ ] **Step 4: Run → pass** `.venv/bin/python3 -m pytest tests/crud/test_subscription_timeline.py -v` → PASS; `.venv/bin/python3 -m py_compile app/database/crud/subscription_event.py`.
- [ ] **Step 5: Commit**
```bash
git add app/database/crud/subscription_event.py tests/crud/test_subscription_timeline.py
git commit -m "feat(subscription): CRUD хронологии покупок/продлений из subscription_events

get_subscription_purchase_timeline — по событиям purchase/renewal/activation
считает простой/остаток и дату окончания на каждом шаге."
```

---

## Task 2: User self endpoint `GET /cabinet/subscription/timeline`

**Files:**
- Modify: `app/cabinet/routes/subscription.py`
- Test: `tests/cabinet/test_subscription_timeline_routes.py`

**Interfaces:**
- Consumes: `get_subscription_purchase_timeline` (Task 1).
- Produces: `GET /cabinet/subscription/timeline` → `{'events': list, 'since': str|None}` (self, по токену).

- [ ] **Step 1: Failing test**
```python
# tests/cabinet/test_subscription_timeline_routes.py
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_self_timeline_route_registered():
    from app.cabinet.routes import router
    paths = {r.path: r.methods for r in router.routes if hasattr(r, 'methods')}
    assert 'GET' in paths['/cabinet/subscription/timeline']


@pytest.mark.asyncio
async def test_self_timeline_returns_events(monkeypatch):
    from app.cabinet.routes import subscription as mod
    rows = [{'index': 1, 'date': '2026-03-21T02:42:00+00:00', 'new_end': '2026-04-20T02:42:00+00:00',
             'period_days': 30, 'event_type': 'purchase', 'amount_kopeks': 10000,
             'prev_end': None, 'downtime_seconds': None, 'carried_seconds': None}]
    monkeypatch.setattr(mod, 'get_subscription_purchase_timeline', AsyncMock(return_value=rows))
    resp = await mod.get_subscription_timeline(user=SimpleNamespace(id=7), db=AsyncMock())
    assert resp['events'] == rows
    assert resp['since'] == '2026-03-21T02:42:00+00:00'
```

- [ ] **Step 2: Run → fail** `.venv/bin/python3 -m pytest tests/cabinet/test_subscription_timeline_routes.py -v` → FAIL.

- [ ] **Step 3: Implement** — в `app/cabinet/routes/subscription.py` добавить импорт `from app.database.crud.subscription_event import get_subscription_purchase_timeline` и эндпоинт (роутер уже с prefix, дающим `/cabinet/subscription`):
```python
@router.get('/timeline')
async def get_subscription_timeline(
    user=Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    """История покупок/продлений текущего пользователя."""
    events = await get_subscription_purchase_timeline(db, user.id)
    return {'events': events, 'since': events[0]['date'] if events else None}
```
(проверить, что `AsyncSession` импортирован в файле; если нет — добавить `from sqlalchemy.ext.asyncio import AsyncSession`.)

- [ ] **Step 4: Run → pass** pytest → PASS; `.venv/bin/python3 -m py_compile app/cabinet/routes/subscription.py`.
- [ ] **Step 5: Commit** `feat(cabinet): эндпоинт /cabinet/subscription/timeline (история юзера)`.

---

## Task 3: Admin endpoint `GET /cabinet/admin/users/{id}/subscription-timeline`

**Files:**
- Modify: `app/cabinet/routes/admin_users.py`
- Test: дополнить `tests/cabinet/test_subscription_timeline_routes.py`

**Interfaces:**
- Produces: `GET /cabinet/admin/users/{user_id}/subscription-timeline` (право `users:read`) → `{'events', 'since'}`. Хендлер `get_user_subscription_timeline`.

- [ ] **Step 1: Failing test (дополнить файл)**
```python
def test_admin_timeline_route_registered():
    from app.cabinet.routes import router
    paths = {r.path: r.methods for r in router.routes if hasattr(r, 'methods')}
    assert 'GET' in paths['/cabinet/admin/users/{user_id}/subscription-timeline']


@pytest.mark.asyncio
async def test_admin_timeline_returns_events(monkeypatch):
    from app.cabinet.routes import admin_users as mod
    rows = [{'index': 1, 'date': '2026-03-21T02:42:00+00:00', 'new_end': '2026-04-20T02:42:00+00:00'}]
    monkeypatch.setattr(mod, 'get_subscription_purchase_timeline', AsyncMock(return_value=rows))
    resp = await mod.get_user_subscription_timeline(user_id=5, admin=SimpleNamespace(id=1), db=AsyncMock())
    assert resp == {'events': rows, 'since': '2026-03-21T02:42:00+00:00'}
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement** — в `app/cabinet/routes/admin_users.py` добавить импорт `from app.database.crud.subscription_event import get_subscription_purchase_timeline` и эндпоинт (рядом с другими `/{user_id}/...`, роутер даёт `/cabinet/admin/users`):
```python
@router.get('/{user_id}/subscription-timeline')
async def get_user_subscription_timeline(
    user_id: int,
    admin: User = Depends(require_permission('users:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict:
    """История покупок/продлений подписки пользователя (для админа)."""
    events = await get_subscription_purchase_timeline(db, user_id)
    return {'events': events, 'since': events[0]['date'] if events else None}
```

- [ ] **Step 4: Run → pass** `.venv/bin/python3 -m pytest tests/cabinet/test_subscription_timeline_routes.py -v`; py_compile.
- [ ] **Step 5: Commit** `feat(cabinet): admin-эндпоинт истории подписки юзера`.

---

## Task 4: Фронт — API + типы

**Files:**
- Modify: `bedolaga-cabinet/src/api/subscription.ts`, `bedolaga-cabinet/src/api/adminUsers.ts`
- (тип общий) Create: `bedolaga-cabinet/src/types/timeline.ts`

**Interfaces:**
- Produces: тип `SubscriptionTimelineEvent`, `SubscriptionTimelineResponse`; `subscriptionApi.getTimeline()`, `adminUsersApi.getSubscriptionTimeline(userId)`.

- [ ] **Step 1:** `src/types/timeline.ts`:
```ts
export interface SubscriptionTimelineEvent {
  index: number;
  event_type: 'purchase' | 'renewal' | 'activation';
  date: string;
  period_days: number | null;
  amount_kopeks: number | null;
  prev_end: string | null;
  new_end: string | null;
  downtime_seconds: number | null;
  carried_seconds: number | null;
}
export interface SubscriptionTimelineResponse {
  events: SubscriptionTimelineEvent[];
  since: string | null;
}
```
- [ ] **Step 2:** в `src/api/subscription.ts` (в объект `subscriptionApi`) добавить:
```ts
  getTimeline: async (): Promise<SubscriptionTimelineResponse> => {
    const response = await apiClient.get<SubscriptionTimelineResponse>('/cabinet/subscription/timeline');
    return response.data;
  },
```
и импорт типа сверху: `import type { SubscriptionTimelineResponse } from '../types/timeline';`
- [ ] **Step 3:** в `src/api/adminUsers.ts` (в объект `adminUsersApi`):
```ts
  getSubscriptionTimeline: async (userId: number): Promise<SubscriptionTimelineResponse> => {
    const response = await apiClient.get<SubscriptionTimelineResponse>(
      `/cabinet/admin/users/${userId}/subscription-timeline`,
    );
    return response.data;
  },
```
+ импорт типа.
- [ ] **Step 4:** `npx tsc --noEmit` (чисто).
- [ ] **Step 5: Commit** `feat(cabinet): api истории подписки (self + admin) + типы`.

---

## Task 5: Форматтеры + vitest

**Files:**
- Create: `bedolaga-cabinet/src/utils/subscriptionTimeline.ts`
- Test: `bedolaga-cabinet/src/utils/subscriptionTimeline.test.ts`

**Interfaces:**
- Produces: `humanizeDuration(seconds, t)`, `formatDetailed(events, t)`, `formatCompact(events, t)` — возвращают строку для копирования. `t` — функция i18n (ключи ниже).

- [ ] **Step 1: Failing test**
```ts
import { describe, it, expect } from 'vitest';
import { humanizeDuration, formatCompact, formatDetailed } from './subscriptionTimeline';
import type { SubscriptionTimelineEvent } from '../types/timeline';

const t = (k: string, o?: Record<string, unknown>) =>
  o ? `${k}:${JSON.stringify(o)}` : k;

const events: SubscriptionTimelineEvent[] = [
  { index: 1, event_type: 'purchase', date: '2026-03-21T02:42:00+00:00', period_days: 30,
    amount_kopeks: 10000, prev_end: null, new_end: '2026-04-20T02:42:00+00:00',
    downtime_seconds: null, carried_seconds: null },
  { index: 2, event_type: 'purchase', date: '2026-04-23T13:02:00+00:00', period_days: 30,
    amount_kopeks: 10000, prev_end: '2026-04-20T02:42:00+00:00', new_end: '2026-05-23T13:02:00+00:00',
    downtime_seconds: 296400, carried_seconds: null },
];

describe('subscriptionTimeline', () => {
  it('humanizeDuration', () => {
    expect(humanizeDuration(296400, t)).toContain('3'); // ~3 дн
  });
  it('formatCompact one line per event', () => {
    const out = formatCompact(events, t);
    expect(out.split('\n').length).toBe(2);
  });
  it('formatDetailed shows downtime for event 2', () => {
    const out = formatDetailed(events, t);
    expect(out).toContain('1)');
    expect(out).toContain('2)');
    expect(out).toContain('timeline.downtime'); // ключ пояснения простоя
  });
});
```
- [ ] **Step 2: Run → fail** `cd /Users/mihail/Desktop/Serv/bedolaga-cabinet && npx vitest run src/utils/subscriptionTimeline.test.ts`.
- [ ] **Step 3: Implement** `src/utils/subscriptionTimeline.ts`:
```ts
import type { SubscriptionTimelineEvent } from '../types/timeline';

type T = (key: string, opts?: Record<string, unknown>) => string;

const fmt = (iso: string | null): string => {
  if (!iso) return '—';
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getDate())}.${p(d.getMonth() + 1)}.${d.getFullYear()}, ${p(d.getHours())}:${p(d.getMinutes())}`;
};

export function humanizeDuration(seconds: number, t: T): string {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const parts: string[] = [];
  if (d) parts.push(t('timeline.unitDays', { count: d }));
  if (h) parts.push(t('timeline.unitHours', { count: h }));
  return parts.join(' ') || t('timeline.unitHours', { count: 0 });
}

export function formatCompact(events: SubscriptionTimelineEvent[], t: T): string {
  return events
    .map((e) => `${e.index}) ${fmt(e.date)} — ${t('timeline.tariffDays', { count: e.period_days ?? 0 })} → ${t('timeline.until')} ${fmt(e.new_end)}`)
    .join('\n');
}

export function formatDetailed(events: SubscriptionTimelineEvent[], t: T): string {
  const lines: string[] = [];
  for (const e of events) {
    lines.push(`${e.index}) ${fmt(e.date)} — ${t('timeline.tariffDays', { count: e.period_days ?? 0 })}`);
    if (e.downtime_seconds) {
      lines.push(`   ${t('timeline.downtime', { prevEnd: fmt(e.prev_end), dur: humanizeDuration(e.downtime_seconds, t) })}`);
    } else if (e.carried_seconds) {
      lines.push(`   ${t('timeline.carried', { dur: humanizeDuration(e.carried_seconds, t) })}`);
    }
    lines.push(`   → ${t('timeline.end')}: ${fmt(e.new_end)}`);
  }
  return lines.join('\n');
}
```
- [ ] **Step 4: Run → pass** vitest. (i18n-ключи `timeline.*` добавим в Task 6/7; в тесте `t` — заглушка.)
- [ ] **Step 5: Commit** `feat(cabinet): форматтеры истории подписки (detailed/compact) + тесты`.

---

## Task 6: Секция у пользователя (`Subscription.tsx`)

**Files:**
- Modify: `bedolaga-cabinet/src/pages/Subscription.tsx`
- Modify: `bedolaga-cabinet/src/locales/ru.json`, `src/locales/en.json`

- [ ] **Step 1:** Добавить запрос (рядом с другими useQuery в `Subscription.tsx`):
```ts
const timeline = useQuery({
  queryKey: ['subscription-timeline'],
  queryFn: subscriptionApi.getTimeline,
});
```
- [ ] **Step 2:** Секция (после блока текущей подписки, до/после reissue — на усмотрение, но внутри `Subscription`): заголовок `t('timeline.title')`, список строк. Для каждого события: дата + `t('timeline.tariffDays',{count})`; строка-пояснение (простой/остаток) через `humanizeDuration`; `→ t('timeline.end')`. Пусто → `t('timeline.empty')`. Под списком — `t('timeline.since',{date})` если `timeline.data?.since`. Классы — как в остальном `Subscription.tsx` (`bg-dark-800/50`, `text-dark-*` и т.п., свериться с файлом).
- [ ] **Step 3:** Локали `ru.json` блок `timeline`:
```json
"timeline": {
  "title": "История подписки",
  "empty": "Истории пока нет",
  "since": "История ведётся с {{date}}",
  "tariffDays": "тариф {{count}} дн.",
  "until": "до",
  "end": "окончание",
  "downtime": "На этот момент подписка уже истекла ({{prevEnd}}) — простой ~{{dur}}. Отсчёт пошёл заново.",
  "carried": "Подписка ещё была активна, остаток ~{{dur}} учтён.",
  "unitDays": "{{count}} дн",
  "unitHours": "{{count}} ч",
  "detailed": "Подробно",
  "compact": "Компактно",
  "copy": "Скопировать",
  "copied": "Скопировано"
}
```
+ англоязычный эквивалент в `en.json`.
- [ ] **Step 4:** `npx tsc --noEmit` + `npm run build`.
- [ ] **Step 5: Commit** `feat(cabinet): раздел «История подписки» в кабинете пользователя`.

---

## Task 7: Секция у админа + переключатель + копирование (`SubscriptionTab.tsx`)

**Files:**
- Modify: `bedolaga-cabinet/src/components/admin/userDetail/SubscriptionTab.tsx`
- (использует локали `timeline.*` из Task 6; util Task 5; api Task 4)

**Interfaces:**
- Consumes: `adminUsersApi.getSubscriptionTimeline(userId)`, `formatDetailed/formatCompact` (Task 5), `copyToClipboard` из `src/utils/clipboard.ts`.

- [ ] **Step 1:** Определить `userId`, который приходит в `SubscriptionTab` (свериться с props/родителем `AdminUserDetail`; вероятно `user.id`). Добавить query:
```ts
const timeline = useQuery({
  queryKey: ['admin-subscription-timeline', userId],
  queryFn: () => adminUsersApi.getSubscriptionTimeline(userId),
  enabled: !!userId,
});
const [mode, setMode] = useState<'detailed' | 'compact'>('detailed');
```
- [ ] **Step 2:** Секция «История подписки»: тот же список, что у юзера, + переключатель `t('timeline.detailed')`/`t('timeline.compact')` (mode) + кнопка `t('timeline.copy')`:
```tsx
onClick={async () => {
  const text = mode === 'detailed'
    ? formatDetailed(timeline.data?.events ?? [], t)
    : formatCompact(timeline.data?.events ?? [], t);
  await copyToClipboard(text);
}}
```
(свериться с сигнатурой `copyToClipboard` в `src/utils/clipboard.ts`; если она принимает/возвращает иначе — адаптировать.)
- [ ] **Step 3:** `npx tsc --noEmit` + `npm run build`.
- [ ] **Step 4: Commit** `feat(cabinet): история подписки в админ-карточке юзера + копирование (detailed/compact)`.

---

## Self-Review
**Покрытие спеки:** CRUD-расчёт (Task 1) ✓; self-эндпоинт (Task 2) ✓; admin-эндпоинт (Task 3) ✓; api+типы (Task 4) ✓; форматтеры detailed/compact + copy-текст (Task 5) ✓; секция у юзера (Task 6) ✓; секция+переключатель+копирование у админа (Task 7) ✓; `since` (пометка «история ведётся с») ✓; только subscription_events ✓.

**Плейсхолдеры:** конкретный код в каждом шаге; отмечены лишь точки сверки (классы, сигнатура `copyToClipboard`, props `userId`) — не заглушки.

**Согласованность типов:** `get_subscription_purchase_timeline` возвращает те же ключи, что читают эндпоинты и `SubscriptionTimelineEvent` (index/event_type/date/period_days/amount_kopeks/prev_end/new_end/downtime_seconds/carried_seconds); `{events, since}` — единый контракт self+admin+фронт; `timeline.*` i18n-ключи одни во всех местах.
