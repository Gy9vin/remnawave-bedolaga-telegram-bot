# Оплата за другого и подарок по стоимости — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Плательщик может оплатить продление чужой подписки по цене получателя, а подарок по коду перестаёт обнулять получателю лимит устройств — его сумма конвертируется в дни.

**Architecture:** Общее ядро — «сумма → дни по цене получателя». Цена считается существующим `pricing_engine.calculate_renewal_price` по подписке получателя. Часть B меняет активацию подарка в `guest_purchase_service`. Часть A добавляет таблицу `sponsored_payments`, резолвер получателя и разделение ролей плательщик/получатель в `SubscriptionRenewalService.finalize`.

**Tech Stack:** Python 3.13 / aiogram 3 / FastAPI / SQLAlchemy async / Alembic (номера наших миграций `9\d{3}`, текущая голова `9027`) / pytest (`.venv/bin/python -m pytest`).

**Спека:** `docs/superpowers/specs/2026-08-09-pay-for-another-design.md`

## Global Constraints

- Тесты: `.venv/bin/python -m pytest`. После правок `.py` обязателен `python3 -m py_compile`.
- Коммит-сообщения на **русском**: заголовок + тело (что и зачем).
- **Никогда** не добавлять trailer `Co-Authored-By`.
- Форк: не перестраивать upstream-файлы, наши правки приоритетнее.
- Номер новой миграции — `9028`, `down_revision = '9027'`.
- В прогоне тестов есть ~38 предсуществующих падений (хвост мержа upstream v4) — они не наши.
- Порядок: сперва часть B (баг в проде), затем часть A.

## File Structure

| Файл | Действие | Ответственность |
|---|---|---|
| `app/services/gift_value_service.py` | Создать | Конвертация суммы подарка в дни по цене получателя |
| `app/services/guest_purchase_service.py` | Изменить (~1424–1545) | Активация подарка: дни вместо подмены тарифа |
| `app/services/recipient_lookup.py` | Создать | Резолв получателя по `@нику` / id / email |
| `app/database/models.py` | Изменить | Модель `SponsoredPayment` |
| `migrations/alembic/versions/9028_sponsored_payments.py` | Создать | Таблица `sponsored_payments` |
| `app/services/subscription_renewal_service.py` | Изменить (`finalize`) | Разделение ролей плательщик/получатель |
| `app/services/sponsored_payment_service.py` | Создать | Котировка, оплата с баланса, применение |
| `app/cabinet/routes/sponsored.py` | Создать | `POST /cabinet/sponsored/lookup`, `POST /cabinet/sponsored/pay` |
| `app/handlers/sponsored_payment.py` | Создать | Точка входа в боте (FSM) |

---

## Часть B. Подарок по стоимости

### Task B1: Конвертация суммы подарка в дни

**Files:**
- Create: `app/services/gift_value_service.py`
- Test: `tests/services/test_gift_value_conversion.py`

**Interfaces:**
- Consumes: `pricing_engine.calculate_renewal_price(db, subscription, period_days, user=user)` → объект с `final_total: int` (копейки).
- Produces:
  ```python
  @dataclass
  class GiftValue:
      days: int              # сколько дней зачесть
      remainder_kopeks: int  # остаток на баланс получателя
      basis_period_days: int # период, по которому считали цену
      price_per_period: int  # цена этого периода у получателя, копейки

  async def convert_gift_to_days(db, *, subscription, user, amount_kopeks, preferred_period_days) -> GiftValue
  def available_periods_for(subscription) -> list[int]
  ```

- [ ] **Step 1: Написать падающий тест**

```python
"""Сумма подарка превращается в дни по цене получателя.

Допустройства тарифицируются за каждый период, поэтому «месяц» для человека с
одним устройством и с десятью — разный товар. Фиксированный пакет, применённый
к произвольному получателю, либо отдаёт лишнее даром, либо отбирает оплаченное.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import gift_value_service as svc


def _sub(periods=None):
    tariff = SimpleNamespace(is_active=True, period_prices={'30': 20000} if periods is None else periods)
    return SimpleNamespace(id=1, tariff_id=7, tariff=tariff)


@pytest.mark.asyncio
async def test_same_price_gives_the_bought_period(monkeypatch):
    monkeypatch.setattr(
        svc.pricing_engine, 'calculate_renewal_price',
        AsyncMock(return_value=SimpleNamespace(final_total=20000)),
    )
    value = await svc.convert_gift_to_days(
        AsyncMock(), subscription=_sub(), user=SimpleNamespace(id=2),
        amount_kopeks=20000, preferred_period_days=30,
    )
    assert value.days == 30
    assert value.remainder_kopeks == 0


@pytest.mark.asyncio
async def test_expensive_recipient_gets_fewer_days(monkeypatch):
    """10 устройств → 450 ₽/мес. Подарок за 200 ₽ = 13 дней."""
    monkeypatch.setattr(
        svc.pricing_engine, 'calculate_renewal_price',
        AsyncMock(return_value=SimpleNamespace(final_total=45000)),
    )
    value = await svc.convert_gift_to_days(
        AsyncMock(), subscription=_sub(), user=SimpleNamespace(id=2),
        amount_kopeks=20000, preferred_period_days=30,
    )
    assert value.days == 13
    assert value.remainder_kopeks == 500  # 200 ₽ − 13 × 15 ₽/день


@pytest.mark.asyncio
async def test_cheap_recipient_gets_more_days(monkeypatch):
    """Мы получили 500 ₽ и отдали товара на 500 ₽ — это тоже честно."""
    monkeypatch.setattr(
        svc.pricing_engine, 'calculate_renewal_price',
        AsyncMock(return_value=SimpleNamespace(final_total=10000)),
    )
    value = await svc.convert_gift_to_days(
        AsyncMock(), subscription=_sub(), user=SimpleNamespace(id=2),
        amount_kopeks=20000, preferred_period_days=30,
    )
    assert value.days == 60
    assert value.remainder_kopeks == 0


@pytest.mark.asyncio
async def test_gift_smaller_than_a_day_goes_entirely_to_balance(monkeypatch):
    monkeypatch.setattr(
        svc.pricing_engine, 'calculate_renewal_price',
        AsyncMock(return_value=SimpleNamespace(final_total=45000)),
    )
    value = await svc.convert_gift_to_days(
        AsyncMock(), subscription=_sub(), user=SimpleNamespace(id=2),
        amount_kopeks=1000, preferred_period_days=30,
    )
    assert value.days == 0
    assert value.remainder_kopeks == 1000


@pytest.mark.asyncio
async def test_free_recipient_falls_back_to_bought_period(monkeypatch):
    """100% скидка у получателя — делить на ноль нельзя."""
    monkeypatch.setattr(
        svc.pricing_engine, 'calculate_renewal_price',
        AsyncMock(return_value=SimpleNamespace(final_total=0)),
    )
    value = await svc.convert_gift_to_days(
        AsyncMock(), subscription=_sub(), user=SimpleNamespace(id=2),
        amount_kopeks=20000, preferred_period_days=30,
    )
    assert value.days == 30
    assert value.remainder_kopeks == 0


@pytest.mark.asyncio
async def test_basis_period_falls_back_to_smallest_available(monkeypatch):
    """У тарифа получателя нет периода подарка — берём наименьший доступный."""
    captured = {}

    async def fake_price(db, subscription, period_days, *, user=None):
        captured['period'] = period_days
        return SimpleNamespace(final_total=10000)

    monkeypatch.setattr(svc.pricing_engine, 'calculate_renewal_price', fake_price)
    value = await svc.convert_gift_to_days(
        AsyncMock(), subscription=_sub({'90': 30000, '180': 50000}),
        user=SimpleNamespace(id=2), amount_kopeks=20000, preferred_period_days=30,
    )
    assert captured['period'] == 90
    assert value.basis_period_days == 90


def test_available_periods_ignores_inactive_tariff():
    sub = SimpleNamespace(tariff_id=7, tariff=SimpleNamespace(is_active=False, period_prices={'30': 1}))
    assert svc.available_periods_for(sub) == []
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/bin/python -m pytest tests/services/test_gift_value_conversion.py -q`
Expected: FAIL — `ModuleNotFoundError: app.services.gift_value_service`

- [ ] **Step 3: Реализовать сервис**

```python
"""Перевод суммы подарка в дни подписки по цене получателя.

Допустройства тарифицируются за каждый период (pricing_engine: extra_devices ×
device_price × months), поэтому «месяц подписки» стоит у разных людей по-разному.
Подарок по коду покупается, когда получатель ещё неизвестен, — единственный
честный способ применить его к конкретному человеку это перевести уплаченную
сумму в дни по ЕГО цене. Иначе либо он получает чужие устройства даром, либо у
него отбирают оплаченные.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services.pricing_engine import pricing_engine


logger = structlog.get_logger(__name__)


@dataclass
class GiftValue:
    days: int
    remainder_kopeks: int
    basis_period_days: int
    price_per_period: int


def available_periods_for(subscription) -> list[int]:
    """Периоды, которые получатель реально может купить.

    Зеркалит выбор периодов в renewal.py: у тарифного режима это ключи
    period_prices активного тарифа, у классического — настройки бота.
    """
    tariff = getattr(subscription, 'tariff', None)
    if getattr(subscription, 'tariff_id', None) and tariff and tariff.is_active and tariff.period_prices:
        return sorted(int(p) for p in tariff.period_prices)
    return []


async def convert_gift_to_days(
    db: AsyncSession,
    *,
    subscription,
    user,
    amount_kopeks: int,
    preferred_period_days: int,
) -> GiftValue:
    """Сколько дней даст сумма подарка этому получателю.

    Округляем вниз: остаток возвращается копейками на баланс, чтобы ничего не
    испарялось и не приходилось объяснять, куда делись деньги.
    """
    periods = available_periods_for(subscription)
    if preferred_period_days in periods or not periods:
        basis = preferred_period_days
    else:
        # Периода подарка у тарифа получателя нет — движок цен по нему откажет.
        basis = periods[0]

    pricing = await pricing_engine.calculate_renewal_price(db, subscription, basis, user=user)
    price = max(0, int(pricing.final_total))

    if price <= 0:
        # У получателя 100% скидка: делить не на что, отдаём купленный период.
        return GiftValue(
            days=preferred_period_days,
            remainder_kopeks=0,
            basis_period_days=basis,
            price_per_period=0,
        )

    days = (amount_kopeks * basis) // price
    spent = (days * price) // basis
    return GiftValue(
        days=int(days),
        remainder_kopeks=max(0, amount_kopeks - spent),
        basis_period_days=basis,
        price_per_period=price,
    )
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/python -m pytest tests/services/test_gift_value_conversion.py -q`
Expected: PASS (7 тестов)

- [ ] **Step 5: Коммит**

```bash
python3 -m py_compile app/services/gift_value_service.py
git add app/services/gift_value_service.py tests/services/test_gift_value_conversion.py
git commit -F - <<'EOF'
feat(gift): перевод суммы подарка в дни по цене получателя

Допустройства тарифицируются за каждый период, поэтому «месяц подписки» стоит у
разных людей по-разному. Подарок по коду покупается, когда получатель ещё
неизвестен, и применить его к конкретному человеку честно можно лишь одним
способом: перевести уплаченную сумму в дни по ЕГО цене.

Сервис пока не подключён — используется в следующей задаче.
EOF
```

---

### Task B2: Активация подарка добавляет дни, не трогая тариф и устройства

**Files:**
- Modify: `app/services/guest_purchase_service.py:1424-1545`
- Test: `tests/services/test_gift_activation_preserves_devices.py`

**Interfaces:**
- Consumes: `convert_gift_to_days(...) -> GiftValue` из Task B1; `extend_subscription(db, subscription, days, *, commit=False)`; `add_user_balance(db, user, amount_kopeks, description, commit=False)`.
- Produces: поведение `activate_purchase` для получателя с существующей подпиской.

- [ ] **Step 1: Написать падающий тест**

```python
"""Подарок не должен обнулять получателю устройства.

Прод-баг: человеку с десятью устройствами дарят подписку — device_limit падает
до тарифного (обычно 1), а все HWID-привязки стираются, и слот занимает то
устройство, которое первым переподключится.
"""
import inspect

from app.services import guest_purchase_service as svc


def test_activation_no_longer_overrides_device_limit():
    source = inspect.getsource(svc.activate_purchase)
    assert 'device_limit=tariff.device_limit' not in source, (
        'подарок больше не подменяет лимит устройств получателя'
    )


def test_activation_no_longer_resets_hwid():
    source = inspect.getsource(svc.activate_purchase)
    assert 'reset_user_devices' not in source, 'сбрасывать привязки больше нечего'


def test_activation_converts_amount_to_days():
    source = inspect.getsource(svc.activate_purchase)
    assert 'convert_gift_to_days' in source
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/bin/python -m pytest tests/services/test_gift_activation_preserves_devices.py -q`
Expected: FAIL — все три, текущий код содержит `device_limit=tariff.device_limit` и `reset_user_devices`.

- [ ] **Step 3: Переписать ветки существующей подписки**

В `activate_purchase` заменить ветки «подписка есть» (и мульти-тариф, и однотарифный режим) на добавление дней. Ветка «подписки нет» (`create_paid_subscription`) остаётся без изменений — конвертировать нечего.

```python
        from app.services.gift_value_service import convert_gift_to_days

        existing = (
            await get_subscription_by_user_and_tariff(db, user.id, tariff.id)
            if settings.is_multi_tariff_enabled()
            else await get_subscription_by_user_id(db, user.id)
        )

        if existing is not None:
            # Подарок = стоимость, а не пакет: тариф, трафик, сквады и лимит
            # устройств получателя остаются его собственными.
            value = await convert_gift_to_days(
                db,
                subscription=existing,
                user=user,
                amount_kopeks=purchase.amount_kopeks,
                preferred_period_days=purchase.period_days,
            )
            if value.days > 0:
                subscription = await extend_subscription(db, existing, value.days, commit=False)
            else:
                subscription = existing
            if value.remainder_kopeks > 0:
                await add_user_balance(
                    db,
                    user,
                    value.remainder_kopeks,
                    'Остаток подарка',
                    commit=False,
                )
            logger.info(
                'Подарок зачтён днями по цене получателя',
                purchase_id=purchase.id,
                days=value.days,
                remainder_kopeks=value.remainder_kopeks,
                price_per_period=value.price_per_period,
            )
        else:
            subscription = await create_paid_subscription(
                db=db,
                user_id=user.id,
                duration_days=purchase.period_days,
                traffic_limit_gb=tariff.traffic_limit_gb,
                device_limit=tariff.device_limit,
                connected_squads=squads,
                tariff_id=tariff.id,
                update_server_counters=True,
                commit=False,
            )
```

Блок `if purchase.is_gift:` со сбросом HWID (строки ~1524–1545) удалить целиком вместе с комментарием.

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `.venv/bin/python -m pytest tests/services/test_gift_activation_preserves_devices.py tests/services/test_gift_value_conversion.py -q`
Expected: PASS

- [ ] **Step 5: Прогнать соседей**

Run: `.venv/bin/python -m pytest tests/services tests/cabinet -q`
Expected: не появилось новых падений сверх предсуществующих.

- [ ] **Step 6: Коммит**

```bash
python3 -m py_compile app/services/guest_purchase_service.py
git add app/services/guest_purchase_service.py tests/services/test_gift_activation_preserves_devices.py
git commit -F - <<'EOF'
fix(gift): подарок обнулял получателю лимит устройств

Человеку с десятью устройствами дарили подписку — device_limit падал до
тарифного (обычно 1), все HWID-привязки стирались, и единственный слот занимало
то устройство, которое первым переподключится.

Активация подарка больше не подменяет получателю тариф, трафик, сквады и лимит
устройств. Сумма подарка переводится в дни по его цене, остаток от округления
идёт на баланс. Сброс привязок убран: понижать стало нечего.

Получателю без подписки подарок применяется по-прежнему — по тарифу подарка.
EOF
```

---

### Task B3: Предупреждение дарителю о конвертации

**Files:**
- Modify: `app/cabinet/routes/gift.py` (ответ `get_gift_config`)
- Modify: `src/locales/ru.json`, `src/locales/en.json` (репозиторий `bedolaga-cabinet`)
- Test: `tests/cabinet/test_gift_config_conversion_notice.py`

**Interfaces:**
- Consumes: существующий ответ `GET /cabinet/gift/config`.
- Produces: поле `value_conversion_notice: str` в ответе конфига подарка.

- [ ] **Step 1: Написать падающий тест**

```python
"""Даритель должен заранее знать, что подарок зачтётся по цене получателя.

Без этой строки в поддержку приходит «я дарил месяц, а пришло 13 дней».
"""
import pytest
from unittest.mock import AsyncMock

from app.cabinet.routes import gift


@pytest.mark.asyncio
async def test_config_explains_value_conversion(monkeypatch):
    monkeypatch.setattr(gift, '_is_gift_enabled', AsyncMock(return_value=True))
    response = await gift.get_gift_config(db=AsyncMock())

    assert response.value_conversion_notice
    assert 'тариф' in response.value_conversion_notice.lower()
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `.venv/bin/python -m pytest tests/cabinet/test_gift_config_conversion_notice.py -q`
Expected: FAIL — у схемы нет поля `value_conversion_notice`.

- [ ] **Step 3: Добавить поле в схему и ответ**

В `app/cabinet/schemas/gift.py` в модель конфига добавить:

```python
    value_conversion_notice: str = (
        'Получателю зачтётся сумма подарка по его тарифу: если у него дороже, дней будет меньше, '
        'если дешевле — больше. Остаток зачислится ему на баланс.'
    )
```

- [ ] **Step 4: Убедиться, что тест проходит**

Run: `.venv/bin/python -m pytest tests/cabinet/test_gift_config_conversion_notice.py -q`
Expected: PASS

- [ ] **Step 5: Показать строку во фронте**

В `bedolaga-cabinet` на экране покупки подарка вывести `value_conversion_notice` под выбором тарифа. Ключи локалей не нужны — текст приходит с бэкенда.

Run: `npx tsc --noEmit && npx vitest run`

- [ ] **Step 6: Коммит (два репозитория)**

```bash
git add app/cabinet/schemas/gift.py tests/cabinet/test_gift_config_conversion_notice.py
git commit -m "feat(gift): предупреждаем дарителя, что подарок зачтётся по цене получателя"
```

---

## Часть A. Оплата за другого

### Task A1: Резолвер получателя

**Files:**
- Create: `app/services/recipient_lookup.py`
- Test: `tests/services/test_recipient_lookup.py`

**Interfaces:**
- Produces: `async def resolve_recipient(db, query: str, *, payer_id: int) -> User | None`

- [ ] **Step 1: Написать падающий тест** — форматы ввода (`@vasya`, `vasya`, `123456`, `a@b.ru`), регистр ника, отказ для заблокированного / удалённого / самого плательщика (во всех случаях `None`).
- [ ] **Step 2: Убедиться, что падает.**
- [ ] **Step 3: Реализовать разбор строки:** начинается с `@` или буквы → `users.username` (ILIKE); только цифры → `telegram_id`; содержит `@` и `.` → `email`. Отфильтровать `status != ACTIVE`, чёрный список, `id == payer_id`.
- [ ] **Step 4: Тесты зелёные.**
- [ ] **Step 5: Коммит.**

### Task A2: Таблица `sponsored_payments`

**Files:**
- Modify: `app/database/models.py`
- Create: `migrations/alembic/versions/9028_sponsored_payments.py`
- Test: `tests/database/test_sponsored_payment_model.py`

Колонки — по спеке. `down_revision = '9027'`. Индексы: `status`, `payer_user_id`, уникальный `payment_id`.

### Task A3: Разделение ролей в `finalize`

**Files:**
- Modify: `app/services/subscription_renewal_service.py`
- Test: `tests/services/test_renewal_payer_split.py`

Добавить необязательный `payer: User | None = None`. Списание — у `payer or user`; `mark_as_paid_subscription` и промо-оффер — у получателя. Тест обязан проверять, что флаг `has_had_paid_subscription` **не** ставится плательщику.

### Task A4: Сервис оплаты за другого

**Files:**
- Create: `app/services/sponsored_payment_service.py`
- Test: `tests/services/test_sponsored_payment_service.py`

`quote(db, recipient, period_days)` → цена; `pay_from_balance(...)`; `apply(payment_id)` для вебхука (идемпотентно). Реферальные не начисляются.

### Task A5: Эндпоинты кабинета

**Files:**
- Create: `app/cabinet/routes/sponsored.py`
- Modify: `app/cabinet/routes/__init__.py`
- Test: `tests/cabinet/test_sponsored_routes.py`

`POST /cabinet/sponsored/lookup` (рейт-лимит 10/мин, отдаёт только имя и цены), `POST /cabinet/sponsored/pay`.

### Task A6: Точка входа в боте

**Files:**
- Create: `app/handlers/sponsored_payment.py`
- Modify: регистрация роутера
- Test: `tests/handlers/test_sponsored_payment_handler.py`

FSM: ввод получателя → карточка → период → оплата. Плюс показ собственного «кода для оплаты» в профиле.

### Task A7: Экран в кабинете

Репозиторий `bedolaga-cabinet`: форма поиска, карточка получателя, выбор периода, оплата.

---

## Self-Review

- Спека покрыта: конвертация подарка — B1/B2, предупреждение дарителя — B3, резолвер — A1, таблица — A2, разделение ролей и флаг `has_had_paid_subscription` — A3, котировка/оплата/идемпотентность — A4, приватность и рейт-лимит — A5, бот — A6, фронт — A7. Устройства закрыты B2 (подарок) и тем, что продление их не трогает.
- Плейсхолдеров нет; в части A шаги описаны короче, чем в B, — детализируются перед выполнением задачи, когда B уже закроет прод-баг.
- Имена согласованы: `convert_gift_to_days`, `GiftValue`, `available_periods_for`, `resolve_recipient`, `SponsoredPayment`.
