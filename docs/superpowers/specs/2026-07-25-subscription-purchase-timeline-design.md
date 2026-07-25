# Хронология подписки (история покупок/продлений) — дизайн

**Дата:** 2026-07-25
**Статус:** дизайн утверждён, готов к плану
**Репозитории:** `remnawave-bedolaga-telegram-bot` (backend `app/`) + `bedolaga-cabinet` (frontend `src/`)

## Проблема / цель
Пользователи жалуются «вы воруете дни подписки», не понимая, что при покупке ПОСЛЕ истечения отсчёт идёт заново от даты покупки (простой не компенсируется), а при покупке ДО истечения остаток переносится. Нужна наглядная **хронология покупок/продлений** с расчётом простоя/остатка и датой окончания на каждом шаге:
1. **видна самому пользователю** в его кабинете (вкладка «Подписка») — чтобы админ говорил «зайди в Подписку и посмотри»;
2. **видна админу** в карточке юзера (вкладка «Подписка») + **кнопка «Скопировать»** (Подробно/Компактно) — чтобы слать в переписке.

Пример «Подробно»:
```
1) 21.03.2026, 02:42 — тариф 30 дней
   → окончание: 20.04.2026, 02:42
2) 23.04.2026, 13:02 — тариф 30 дней
   Подписка от 21.03 уже истекла (20.04.2026, 02:42) — простой ~3 дн 10 ч. Отсчёт заново.
   → окончание: 23.05.2026, 13:02
3) 20.05.2026, 23:18 — тариф 30 дней
   Подписка ещё активна, оставалось ~2 дн 13 ч. Остаток учтён.
   → окончание: 22.06.2026, 13:02
```

## Источник данных
Таблица **`subscription_events`** (`app/database/models.py`), заполняется через `create_subscription_event` / `admin_notification_service._record_subscription_event`. Берём события пользователя типов `purchase`, `renewal`, `activation`, упорядоченные по `occurred_at`.

Поля события:
- `occurred_at` — дата операции;
- `amount_kopeks` — сумма;
- `extra.period_days` — период (у `purchase` и `renewal`);
- `extra.previous_end_date` / `extra.new_end_date` — ISO-даты (только у `renewal`, авторитетны);
- у `activation` (триал): `extra.trial_duration_days`;
- у `purchase` даты окончания НЕТ → реконструируем (см. алгоритм).

**Ограничение (принято владельцем):** покупки старше момента, когда бот начал логировать `subscription_events`, в списке отсутствуют. Под списком показываем «история ведётся с <дата первого события>».

## Алгоритм расчёта (на бэкенде, единый для обоих входов)
`get_subscription_purchase_timeline(db, user_id) -> list[dict]`:
1. Загрузить события юзера (`event_type in ('purchase','renewal','activation')`), сортировка по `occurred_at ASC`.
2. Идти по порядку, держа `running_end` (дата окончания после предыдущего события; сначала `None`).
3. Для каждого события:
   - `date = occurred_at`; `period_days = extra.period_days` (для activation — `trial_duration_days`);
   - `prev_end = running_end`;
   - если в `extra` есть `new_end_date` → `new_end = extra.new_end_date` (авторитетно), `prev_end_effective = extra.previous_end_date or prev_end`;
   - иначе → `new_end = (date if (prev_end is None or date >= prev_end) else prev_end) + timedelta(days=period_days)`; `prev_end_effective = prev_end`;
   - `downtime_seconds = max(0, (date - prev_end_effective))` если `prev_end_effective` и `date > prev_end_effective`, иначе `None`;
   - `carried_seconds = max(0, (prev_end_effective - date))` если `prev_end_effective` и `date < prev_end_effective`, иначе `None`;
   - `running_end = new_end`.
4. Вернуть список dict: `{index, event_type, date(ISO), period_days, amount_kopeks, prev_end(ISO|null), new_end(ISO), downtime_seconds|null, carried_seconds|null}`. Форматирование дат/длительностей — на фронте.

## Архитектура

### Backend (`remnawave-bedolaga-telegram-bot/app/`)
1. **CRUD** `app/database/crud/subscription_event.py` — добавить `get_subscription_purchase_timeline(db, user_id) -> list[dict]` (запрос + алгоритм выше).
2. **User endpoint** — в `app/cabinet/routes/subscription.py` (или `subscription_modules/`): `GET /cabinet/subscription/timeline` → отдаёт хронологию текущего пользователя (user_id из токена). Возвращает `{events: [...], since: <ISO первого события|null>}`.
3. **Admin endpoint** — в `app/cabinet/routes/admin_users.py`: `GET /cabinet/admin/users/{user_id}/subscription-timeline` (под правом чтения юзеров, как у соседних admin-user эндпоинтов) → `{events, since}`.

### Frontend (`bedolaga-cabinet/src/`)
1. **API** `src/api/subscription.ts` — `getTimeline()` (свой); `src/api/adminUsers.ts` — `getSubscriptionTimeline(userId)`.
2. **Пользователь** — в `src/pages/Subscription.tsx` секция «История подписки»: список строк, каждая с датой/тарифом, строкой-пояснением (простой/остаток) и датой окончания. Read-only. Под списком — «история ведётся с …».
3. **Админ** — в `src/components/admin/userDetail/SubscriptionTab.tsx` та же секция + переключатель **Подробно/Компактно** + кнопка **«Скопировать»** (копирует текст выбранного формата через `navigator.clipboard`, как это уже делают другие copy-места в кабинете).
4. **Форматтеры** — общий util (напр. `src/utils/subscriptionTimeline.ts`): `formatDetailed(events)` и `formatCompact(events)` → строки для копирования; и хелпер длительности `humanizeDuration(seconds)` → «3 дн 10 ч».
5. **Локали** ru/en: заголовки, «простой ~{d}», «остаток учтён ~{d}», «Отсчёт заново», «окончание», подписи кнопок/переключателя.

## Форматы (что копируется у админа)
- **Подробно** — как в примере выше (нумерованный список + пояснения простоя/остатка).
- **Компактно** — `1) 21.03.2026 02:42 — 30 дн. → до 20.04.2026 02:42` (одна строка на событие, без пояснений).

## Обработка ошибок / крайние случаи
- Нет событий → секция показывает «Истории пока нет».
- Первое событие → без простоя/остатка (нет `prev_end`).
- `activation` (триал) → строка «триал N дней», участвует в расчёте `running_end`.
- Даты хранятся aware; форматирование — в таймзоне пользователя (как в остальном кабинете, `format_local_datetime` на бэке не нужен — отдаём ISO, фронт форматирует).

## Тестирование
- Backend unit: `get_subscription_purchase_timeline` на наборе событий — проверить downtime (покупка после конца), carried (покупка до конца), первое событие без prev, renewal с авторитетными previous/new_end_date, activation. (реальный SQLite-харнесс как в `tests/crud/test_google_migration_crud.py`.)
- Endpoint-тесты (admin + self): форма ответа `{events, since}`, гейт прав у админского.
- Frontend: сборка `tsc`+`build`; форматтеры `formatDetailed/formatCompact` — юнит на `src/utils/subscriptionTimeline.ts` (в кабинете есть vitest).

## Вне объёма
- Реконструкция истории из `transactions` для покупок старше логирования (владелец выбрал только `subscription_events`).
- Экспорт в файл/PDF.
