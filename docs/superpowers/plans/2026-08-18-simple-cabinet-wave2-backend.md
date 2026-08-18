# Простой режим кабинета — волна 2: доработки в боте

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать API всё, чего не хватает экранам простого режима: название клиентского приложения у устройства, выборочное массовое отключение, уменьшение лимита с выбором устройств и пропорциональным возвратом, готовые суммы пополнения из цен тарифов.

**Architecture:** Правки локальные, новых подсистем не заводим. Название клиента берётся из истории запросов подписки панели и приклеивается к устройствам по hwid. Возврат считается той же формулой, что и покупка, и начисляется существующей `add_user_balance` с типом `REFUND`, чтобы деньги оставляли след в истории операций. Идемпотентность уменьшения обеспечивается блокировкой строки подписки и проверкой факта изменения лимита внутри той же транзакции.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x, aiohttp-клиент панели RemnaWave.

**Spec:** `docs/superpowers/specs/2026-08-18-simple-cabinet-mode-design.md`, разделы «Устройства», «Лимит устройств», «Пополнение баланса».

## Global Constraints

- Репозиторий: `/Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot`, венв `.venv`, тесты `.venv/bin/pytest`.
- **Запрещено** `git stash`, `git checkout`, `git restore`, `git reset`, `git clean` — в репозитории бывают незакоммиченные правки других участников.
- Базовая линия тестов: **42 падения**, существуют независимо от этой работы. Модуль `tests/unit/test_price_calculation_parity.py` не собирается — запускать с `--ignore=tests/unit/test_price_calculation_parity.py`. Новых падений быть не должно.
- **Запрещено** добавлять trailer `Co-Authored-By`. Commit-сообщения на русском: заголовок плюс тело.
- Комментарии и докстринги на русском.
- `device_limit == 0` означает БЕЗЛИМИТ. Ноль не подменять единицей, арифметику от нуля не считать.
- Деньги на баланс начисляются только через `add_user_balance` с `create_transaction=True` — прямая мутация `balance_kopeks` запрещена, иначе у человека в истории появятся деньги из ниоткуда.
- Тесты в `tests/cabinet/` пишутся с локальными фикстурами, отдельного `conftest.py` там нет. Образец мока панели — `tests/cabinet/test_admin_devices_lazy_panel_identity.py`.
- Ничего не пушить, только локальные коммиты.

---

### Task 1: Название клиентского приложения в списке устройств

**Files:**
- Modify: `app/cabinet/routes/subscription_modules/devices.py:976-1015` (сборка `formatted_devices` в `GET /devices`)
- Test: `tests/cabinet/test_device_client_name.py`

**Interfaces:**
- Consumes: поле `userAgent` объекта устройства из `api.get_user_devices_all` — панель хранит его прямо в записи HWID (`prisma/schema.prisma:350`, `HwidUserDevices.userAgent`, nullable)
- Produces: `extract_client_name(user_agent: object) -> str | None`; поле `client` в каждом элементе `devices` ответа `GET /devices`

**Зачем:** hwid выдаётся на установку приложения, а не на физическое устройство. Happ и INCY на одном телефоне занимают два места, и без названия клиента список выглядит как перечень чужих телефонов. Панель название знает и отдаёт его вместе с устройством — кабинет его просто не читает: маппинг собирает только hwid, платформу, модель и дату.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/cabinet/test_device_client_name.py`:

```python
"""Название клиентского приложения в списке устройств.

hwid выдаётся на установку приложения, а не на физическое устройство: Happ и
INCY на одном телефоне занимают два места. Без названия клиента человек видит
список, где устройств больше, чем у него есть, и идёт в поддержку.

Панель хранит агент прямо в записи устройства (HwidUserDevices.userAgent),
поэтому дополнительных запросов не нужно — поле просто не читалось.
"""

import pytest

from app.cabinet.routes.subscription_modules.devices import extract_client_name


@pytest.mark.parametrize(
    'user_agent, expected',
    [
        ('Happ/2.1.0 (iPhone; iOS 17.4)', 'Happ'),
        ('Streisand/1.2.3 (iPad; iPadOS 17)', 'Streisand'),
        ('INCY/3.0', 'INCY'),
        ('Hiddify', 'Hiddify'),
        ('  Happ/2.1  ', 'Happ'),
    ],
)
def test_client_name_is_trimmed_of_version_and_platform(user_agent, expected):
    assert extract_client_name(user_agent) == expected


@pytest.mark.parametrize('empty', ['', '   ', None])
def test_missing_agent_gives_none(empty):
    """Пустое поле честнее выдуманного «Unknown» — фронт покажет платформу."""
    assert extract_client_name(empty) is None


@pytest.mark.parametrize('garbage', [42, [], {}, True])
def test_non_string_agent_is_safe(garbage):
    assert extract_client_name(garbage) is None


def test_agent_without_name_part_gives_none():
    """Агент из одних разделителей не даёт имени."""
    assert extract_client_name('/1.0') is None
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/cabinet/test_device_client_name.py -q`
Expected: FAIL с `ImportError: cannot import name 'extract_client_name'`

- [ ] **Step 3: Добавить хелпер**

В `app/cabinet/routes/subscription_modules/devices.py`, на уровне модуля, рядом с другими хелперами и до определений роутов:

```python
def extract_client_name(user_agent: object) -> str | None:
    """Достать читаемое имя программы из user-agent устройства.

    Панель отдаёт агент целиком — «Happ/2.1.0 (iPhone; iOS 17.4)». Человеку
    нужно только имя: версия ему ничего не говорит, а платформа и так показана
    отдельным полем. Берём часть до первого слэша или пробела.

    Неразборчивый агент даёт None, а не строку «Unknown»: пустое место в
    интерфейсе честнее выдуманного имени, и фронт в этом случае показывает
    платформу с моделью.
    """
    if not isinstance(user_agent, str):
        return None
    name = user_agent.strip().split('/', 1)[0].split(' ', 1)[0].strip()
    return name or None
```

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/cabinet/test_device_client_name.py -q`
Expected: PASS, 13 тестов

- [ ] **Step 5: Отдавать клиента в `GET /devices`**

В том же файле, в цикле сборки `formatted_devices` обработчика `GET /devices`, добавить чтение агента и поле `client`. Итоговый цикл:

```python
            formatted_devices = []
            for device in devices_list:
                hwid = device.get('hwid') or device.get('deviceId') or device.get('id')
                platform = device.get('platform') or device.get('platformType') or 'Unknown'
                model = device.get('deviceModel') or device.get('model') or device.get('name') or 'Unknown'
                created_at = device.get('updatedAt') or device.get('lastSeen') or device.get('createdAt')

                formatted_devices.append(
                    {
                        'hwid': hwid,
                        'platform': platform,
                        'device_model': model,
                        'created_at': created_at,
                        # Имя программы: Happ, INCY и т.д. None — агент не разобрался,
                        # тогда фронт показывает платформу и модель.
                        'client': extract_client_name(device.get('userAgent')),
                        # Локальное имя, заданное юзером. None — алиаса нет,
                        # фронт фоллбэчит на platform/device_model.
                        'local_name': aliases.get(hwid) or None,
                    }
                )
```

Больше в этом обработчике менять нечего: дополнительных запросов к панели не нужно, поле приходит вместе с устройством.

- [ ] **Step 6: Проверить компиляцию и полный набор тестов**

Run:
```bash
.venv/bin/python -m py_compile app/cabinet/routes/subscription_modules/devices.py
.venv/bin/pytest tests/ -q --ignore=tests/unit/test_price_calculation_parity.py
```
Expected: компиляция чистая; 42 падения, ровно те же, что в базовой линии

- [ ] **Step 7: Коммит**

```bash
git add app/cabinet/routes/subscription_modules/devices.py tests/cabinet/test_device_client_name.py
git commit -F - <<'EOF'
feat(devices): название клиентского приложения в списке устройств

hwid выдаётся на установку приложения, а не на физическое устройство: Happ и
INCY на одном телефоне занимают два места. Без названия клиента человек видит
список, где устройств больше, чем у него есть, и идёт в поддержку.

Панель хранит user-agent прямо в записи устройства и отдаёт его вместе со
списком — кабинет просто не читал это поле, маппинг собирал только hwid,
платформу, модель и дату. Дополнительных запросов не потребовалось.

Из агента берём имя программы: версия человеку ничего не говорит, а платформа и
так показана отдельно. Неразборчивый агент даёт пустое поле, а не «Unknown» —
пустое место честнее выдуманного имени.
EOF
```

---

### Task 2: Выборочное массовое отключение устройств

**Files:**
- Modify: `app/cabinet/schemas/subscription.py` (рядом с `ReduceDevicesRequest`, около строки 172)
- Modify: `app/cabinet/routes/subscription_modules/devices.py` (новый роут рядом с `DELETE /devices`, около строки 1145)
- Test: `tests/cabinet/test_devices_bulk_delete.py`

**Interfaces:**
- Consumes: `api.remove_device(panel_user_id, hwid) -> bool` из `app/external/remnawave_api.py:1840`
- Produces: `POST /cabinet/subscription/devices/delete-batch` с телом `{"hwids": ["...", "..."]}` → `{"success": bool, "deleted_count": int, "failed_hwids": [str]}`

**Зачем:** сейчас есть только удаление по одному и «снести все». Экран устройств простого режима даёт отметить несколько галочками, и без этого эндпоинта фронту пришлось бы слать N запросов подряд, оставляя человека в непонятном состоянии при отказе на середине.

Метод HTTP — `POST`, а не `DELETE`: тело у `DELETE` поддерживается не всеми прокси и клиентами, а список hwid в query-строку не помещается.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/cabinet/test_devices_bulk_delete.py`:

```python
"""Выборочное отключение нескольких устройств одним запросом.

Экран устройств даёт отметить несколько галочками. Без этого эндпоинта фронту
пришлось бы слать N запросов подряд: при отказе на середине человек остаётся в
состоянии, которого не выбирал, и не понимает, что отключилось, а что нет.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import devices as devices_routes


class _Api:
    def __init__(self, failing: set[str] | None = None):
        self.removed: list[str] = []
        self.failing = failing or set()

    async def remove_device(self, panel_user_id, hwid):
        if hwid in self.failing:
            return False
        self.removed.append(hwid)
        return True


def _service_with(api):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=api)
    client.__aexit__ = AsyncMock(return_value=False)
    service = MagicMock()
    service.get_api_client = MagicMock(return_value=client)
    return MagicMock(return_value=service)


@pytest.fixture
def patched(monkeypatch):
    def _apply(api, subscription=None):
        monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', _service_with(api))
        sub = subscription or SimpleNamespace(id=1, user_id=1, remnawave_id=5, device_limit=5)
        monkeypatch.setattr(
            devices_routes, 'resolve_subscription', AsyncMock(return_value=sub)
        )
        monkeypatch.setattr(
            devices_routes, '_ensure_panel_user_id', AsyncMock(return_value=5)
        )
    return _apply


@pytest.mark.asyncio
async def test_deletes_every_requested_device(patched):
    api = _Api()
    patched(api)
    result = await devices_routes.delete_devices_batch(
        devices_routes.DeleteDevicesBatchRequest(hwids=['a', 'b', 'c']),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert result['deleted_count'] == 3
    assert result['failed_hwids'] == []
    assert api.removed == ['a', 'b', 'c']


@pytest.mark.asyncio
async def test_partial_failure_is_reported_not_hidden(patched):
    """Отказ по одному устройству не должен молча выглядеть успехом."""
    api = _Api(failing={'b'})
    patched(api)
    result = await devices_routes.delete_devices_batch(
        devices_routes.DeleteDevicesBatchRequest(hwids=['a', 'b', 'c']),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert result['success'] is False
    assert result['deleted_count'] == 2
    assert result['failed_hwids'] == ['b']


@pytest.mark.asyncio
async def test_failure_on_one_device_does_not_stop_the_rest(patched):
    api = _Api(failing={'a'})
    patched(api)
    result = await devices_routes.delete_devices_batch(
        devices_routes.DeleteDevicesBatchRequest(hwids=['a', 'b']),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert api.removed == ['b']
    assert result['deleted_count'] == 1


@pytest.mark.asyncio
async def test_duplicates_are_collapsed(patched):
    api = _Api()
    patched(api)
    result = await devices_routes.delete_devices_batch(
        devices_routes.DeleteDevicesBatchRequest(hwids=['a', 'a', 'b']),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert result['deleted_count'] == 2
    assert api.removed == ['a', 'b']


@pytest.mark.asyncio
async def test_missing_subscription_gives_404(monkeypatch):
    monkeypatch.setattr(devices_routes, 'resolve_subscription', AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await devices_routes.delete_devices_batch(
            devices_routes.DeleteDevicesBatchRequest(hwids=['a']),
            subscription_id=None,
            user=SimpleNamespace(id=1),
            db=AsyncMock(),
        )
    assert exc.value.status_code == 404
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/cabinet/test_devices_bulk_delete.py -q`
Expected: FAIL — `delete_devices_batch` и `DeleteDevicesBatchRequest` не существуют

- [ ] **Step 3: Добавить схему запроса**

В `app/cabinet/schemas/subscription.py`, сразу после `ReduceDevicesRequest`:

```python
class DeleteDevicesBatchRequest(BaseModel):
    """Список устройств на отключение одним запросом."""

    # Верхняя граница защищает от запроса, который будет минуты долбить панель
    # по одному устройству. Пятьдесят — заведомо больше любого разумного лимита.
    hwids: list[str] = Field(min_length=1, max_length=50)
```

- [ ] **Step 4: Добавить эндпоинт**

В `app/cabinet/routes/subscription_modules/devices.py`, рядом с `delete_all_devices`:

```python
@router.post('/devices/delete-batch')
async def delete_devices_batch(
    request: DeleteDevicesBatchRequest,
    subscription_id: int | None = QueryParam(None, description='Subscription ID for multi-tariff'),
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Отключить несколько выбранных устройств одним запросом.

    Метод POST, а не DELETE: тело у DELETE поддерживается не всеми прокси, а
    список hwid в query-строку не помещается.

    Отказ по одному устройству не останавливает остальные и не выдаётся за
    успех: неудачные hwid возвращаются списком, чтобы фронт показал, что именно
    не отключилось, и не соврал человеку.
    """
    from app.services.remnawave_service import RemnaWaveService

    subscription = await resolve_subscription(db, user, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No subscription found')

    # Дубликаты схлопываем, порядок сохраняем — иначе один и тот же hwid
    # ушёл бы в панель дважды и второй раз вернул бы отказ.
    unique_hwids: list[str] = []
    seen: set[str] = set()
    for hwid in request.hwids:
        if hwid and hwid not in seen:
            seen.add(hwid)
            unique_hwids.append(hwid)

    deleted_count = 0
    failed_hwids: list[str] = []

    service = RemnaWaveService()
    async with service.get_api_client() as api:
        _panel_user_id = await _ensure_panel_user_id(db, subscription, user, api)
        if not _panel_user_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Panel user not found')

        for hwid in unique_hwids:
            try:
                if await api.remove_device(_panel_user_id, hwid):
                    deleted_count += 1
                else:
                    failed_hwids.append(hwid)
            except Exception as device_error:
                logger.error(
                    'Failed to remove device in batch',
                    user_id=user.id,
                    hwid=hwid,
                    error=str(device_error)[:200],
                )
                failed_hwids.append(hwid)

    logger.info(
        'Batch device removal finished',
        user_id=user.id,
        requested=len(unique_hwids),
        deleted=deleted_count,
        failed=len(failed_hwids),
    )

    return {
        'success': not failed_hwids,
        'deleted_count': deleted_count,
        'failed_hwids': failed_hwids,
    }
```

В шапке файла добавить `DeleteDevicesBatchRequest` в импорт схем рядом с `ReduceDevicesRequest`.

- [ ] **Step 5: Запустить тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/cabinet/test_devices_bulk_delete.py -q`
Expected: PASS, 5 тестов

- [ ] **Step 6: Проверить компиляцию и полный набор тестов**

Run:
```bash
.venv/bin/python -m py_compile app/cabinet/routes/subscription_modules/devices.py app/cabinet/schemas/subscription.py
.venv/bin/pytest tests/ -q --ignore=tests/unit/test_price_calculation_parity.py
```
Expected: компиляция чистая; 42 падения, те же, что в базовой линии

- [ ] **Step 7: Коммит**

```bash
git add app/cabinet/routes/subscription_modules/devices.py app/cabinet/schemas/subscription.py tests/cabinet/test_devices_bulk_delete.py
git commit -F - <<'EOF'
feat(devices): выборочное отключение нескольких устройств одним запросом

Раньше было только удаление по одному и «снести все». Экран устройств даёт
отметить несколько галочками, и без этого эндпоинта фронту пришлось бы слать N
запросов подряд: при отказе на середине человек остаётся в состоянии, которого
не выбирал, и не понимает, что отключилось, а что нет.

Отказ по одному устройству не останавливает остальные и не выдаётся за успех —
неудачные hwid возвращаются списком. Дубликаты схлопываются: один и тот же hwid
ушёл бы в панель дважды и второй раз вернул бы отказ.

Метод POST, а не DELETE: тело у DELETE поддерживается не всеми прокси, а список
hwid в query-строку не помещается.
EOF
```

---

### Task 3: Уменьшение лимита с выбором устройств и пропорциональным возвратом

**Files:**
- Modify: `app/cabinet/schemas/subscription.py:172-175` (`ReduceDevicesRequest`)
- Modify: `app/cabinet/routes/subscription_modules/devices.py:1314-1471` (`reduce_devices`) и `get_device_reduction_info`
- Create: `app/utils/device_refund.py`
- Test: `tests/utils/test_device_refund.py`, `tests/cabinet/test_reduce_devices_refund.py`

**Interfaces:**
- Consumes: `add_user_balance` из `app/database/crud/user.py:554`, `TransactionType.REFUND`, `resolve_min_device_limit` из `app/utils/subscription_utils.py`
- Produces:
  - `calculate_device_refund_kopeks(device_price_kopeks: int, slots: int, days_left: int) -> int`
  - `ReduceDevicesRequest` с полем `hwids_to_remove: list[str] | None`
  - `POST /devices/reduce` возвращает дополнительно `refund_kopeks: int`

**Зачем два изменения сразу:** и возврат, и выбор устройств меняют одну и ту же транзакцию уменьшения. Разделять их значило бы дважды переписывать один обработчик и дважды его ревьюить.

**Текущее поведение, которое чиним:** обработчик сортирует устройства по времени последней активности и удаляет **последние** — то есть самые свежие. Отключается то, чем человек только что пользовался.

**Формула возврата.** Истории покупок устройств в базе нет: `Transaction.description` — свободный текст без структуры. Поэтому возврат считается от **текущей** цены места на оставшийся срок, той же формулой, что и покупка: `цена_места × слоты × дни_до_конца / 30`. Купил место на 30 дней за 60 ₽, отказался через день — вернём 58 ₽. Разница в цену одного дня, в пользу клиента.

**Возврат положен только за места сверх тарифа.** Ниже тарифного минимума опуститься нельзя, значит освобождаются только докупленные слоты.

- [ ] **Step 1: Написать падающий тест формулы**

Создать `tests/utils/test_device_refund.py`:

```python
"""Пропорциональный возврат за отключаемое место.

Истории покупок устройств в базе нет — Transaction.description это свободный
текст. Поэтому возврат считаем от ТЕКУЩЕЙ цены места на оставшийся срок, той же
формулой, что и покупку. Купил на 30 дней за 60 рублей, отказался через день —
вернём 58. Разница в цену одного дня, в пользу клиента.
"""

import pytest

from app.utils.device_refund import calculate_device_refund_kopeks


def test_full_period_refunds_full_price():
    assert calculate_device_refund_kopeks(6000, slots=1, days_left=30) == 6000


def test_half_period_refunds_half():
    assert calculate_device_refund_kopeks(6000, slots=1, days_left=15) == 3000


def test_several_slots_multiply():
    assert calculate_device_refund_kopeks(6000, slots=2, days_left=24) == 9600


def test_longer_than_month_is_not_capped():
    """Годовая подписка — место оплачено на весь срок, возврат тоже за весь."""
    assert calculate_device_refund_kopeks(6000, slots=1, days_left=90) == 18000


@pytest.mark.parametrize('days_left', [0, -1, -100])
def test_expired_subscription_refunds_nothing(days_left):
    assert calculate_device_refund_kopeks(6000, slots=1, days_left=days_left) == 0


@pytest.mark.parametrize('slots', [0, -1])
def test_no_slots_refunds_nothing(slots):
    assert calculate_device_refund_kopeks(6000, slots=slots, days_left=30) == 0


def test_zero_device_price_refunds_nothing():
    """Место бесплатно — возвращать нечего, а не «минимум один рубль»."""
    assert calculate_device_refund_kopeks(0, slots=2, days_left=30) == 0


def test_result_is_rounded_down_never_up():
    """Округление в пользу сервиса на копейки, чтобы возврат не превысил уплаченное."""
    assert calculate_device_refund_kopeks(6000, slots=1, days_left=7) == 1400


def test_garbage_price_is_treated_as_zero():
    assert calculate_device_refund_kopeks(None, slots=1, days_left=30) == 0
    assert calculate_device_refund_kopeks(-500, slots=1, days_left=30) == 0
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/utils/test_device_refund.py -q`
Expected: FAIL с `ModuleNotFoundError: No module named 'app.utils.device_refund'`

- [ ] **Step 3: Написать модуль формулы**

Создать `app/utils/device_refund.py`:

```python
"""Возврат за отключаемое место устройства.

Зеркало формулы покупки из `get_device_price`: цена места пропорциональна
оставшимся дням подписки при базе в 30 дней. Держать её отдельной функцией
важно, чтобы списание и возврат не разъехались — две независимые арифметики на
одних и тех же деньгах рано или поздно дают расхождение, которое всплывает
жалобой.
"""

from __future__ import annotations

_BASE_PERIOD_DAYS = 30


def calculate_device_refund_kopeks(device_price_kopeks: object, slots: int, days_left: int) -> int:
    """Сколько вернуть за `slots` освобождённых мест при `days_left` до конца.

    Округление вниз: возврат не должен превысить уплаченное из-за копеек.
    Ноль возвращается честно — если место бесплатно или срок вышел, возвращать
    нечего, и подставлять минимальную сумму было бы выдумыванием долга.
    """
    if not isinstance(device_price_kopeks, int) or isinstance(device_price_kopeks, bool):
        return 0
    if device_price_kopeks <= 0 or slots <= 0 or days_left <= 0:
        return 0
    return device_price_kopeks * slots * days_left // _BASE_PERIOD_DAYS
```

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/utils/test_device_refund.py -q`
Expected: PASS, 12 тестов

- [ ] **Step 5: Расширить схему запроса**

В `app/cabinet/schemas/subscription.py` заменить `ReduceDevicesRequest` на:

```python
class ReduceDevicesRequest(BaseModel):
    """Request to reduce device limit."""

    new_device_limit: int = Field(ge=1, le=100)
    # Какие именно устройства отключить. None означает «решай сам» и оставлен
    # для совместимости со старыми клиентами: раньше обработчик всегда выбирал
    # сам, причём удалял самые свежие по активности.
    hwids_to_remove: list[str] | None = Field(default=None, max_length=50)
```

- [ ] **Step 6: Написать падающие тесты обработчика**

Создать `tests/cabinet/test_reduce_devices_refund.py`:

```python
"""Уменьшение лимита: выбор отключаемых устройств и возврат денег.

Два изменения в одной транзакции. Раньше обработчик сам выбирал жертв, сортируя
по активности и удаляя САМЫЕ СВЕЖИЕ, и не возвращал денег вовсе.

Ключевой инвариант: количество отключаемых устройств и количество освобождаемых
мест — разные величины. Мест освобождается «старый лимит минус новый».
Устройств отключать надо «сколько подключено минус новый лимит», и это число
бывает меньше, а бывает нулём.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.subscription_modules import devices as devices_routes


class _Api:
    def __init__(self, devices):
        self.devices = devices
        self.removed: list[str] = []

    async def get_user_devices_all(self, panel_user_id):
        return {'devices': self.devices, 'total': len(self.devices)}

    async def remove_device(self, panel_user_id, hwid):
        self.removed.append(hwid)
        return True


def _patch_common(monkeypatch, api, subscription, added_balance):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=api)
    client.__aexit__ = AsyncMock(return_value=False)
    service = MagicMock()
    service.get_api_client = MagicMock(return_value=client)
    monkeypatch.setattr('app.services.remnawave_service.RemnaWaveService', MagicMock(return_value=service))

    monkeypatch.setattr(devices_routes, 'resolve_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(devices_routes, '_ensure_panel_user_id', AsyncMock(return_value=5))

    async def _fake_add_balance(**kwargs):
        added_balance.append(kwargs)
        return True

    monkeypatch.setattr(devices_routes, 'add_user_balance', _fake_add_balance)


@pytest.mark.asyncio
async def test_removes_exactly_chosen_devices(monkeypatch, reduce_env):
    """Отключается то, что выбрал человек, а не то, что выбрал алгоритм."""
    api, subscription, added = reduce_env(devices=[{'hwid': 'a'}, {'hwid': 'b'}, {'hwid': 'c'}])
    await devices_routes.reduce_devices(
        devices_routes.ReduceDevicesRequest(new_device_limit=2, hwids_to_remove=['a']),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert api.removed == ['a']


@pytest.mark.asyncio
async def test_refund_is_credited_with_transaction(reduce_env):
    """Возврат обязан оставить след в истории операций, иначе деньги из ниоткуда."""
    api, subscription, added = reduce_env(devices=[{'hwid': 'a'}, {'hwid': 'b'}, {'hwid': 'c'}])
    result = await devices_routes.reduce_devices(
        devices_routes.ReduceDevicesRequest(new_device_limit=2, hwids_to_remove=['a']),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert result['refund_kopeks'] > 0
    assert len(added) == 1
    assert added[0]['create_transaction'] is True


@pytest.mark.asyncio
async def test_no_devices_to_remove_when_under_limit(reduce_env):
    """Подключено меньше нового лимита — отключать нечего, но места освобождаются."""
    api, subscription, added = reduce_env(devices=[{'hwid': 'a'}])
    result = await devices_routes.reduce_devices(
        devices_routes.ReduceDevicesRequest(new_device_limit=2, hwids_to_remove=None),
        subscription_id=None,
        user=SimpleNamespace(id=1),
        db=AsyncMock(),
    )
    assert api.removed == []
    assert result['refund_kopeks'] > 0


@pytest.mark.asyncio
async def test_wrong_number_of_hwids_is_rejected(reduce_env):
    """Прислали не тех и не столько — отказываем, а не досоображаем за человека."""
    api, subscription, added = reduce_env(devices=[{'hwid': 'a'}, {'hwid': 'b'}, {'hwid': 'c'}])
    with pytest.raises(HTTPException) as exc:
        await devices_routes.reduce_devices(
            devices_routes.ReduceDevicesRequest(new_device_limit=1, hwids_to_remove=['a']),
            subscription_id=None,
            user=SimpleNamespace(id=1),
            db=AsyncMock(),
        )
    assert exc.value.status_code == 400
    assert added == []


@pytest.mark.asyncio
async def test_unknown_hwid_is_rejected(reduce_env):
    api, subscription, added = reduce_env(devices=[{'hwid': 'a'}, {'hwid': 'b'}, {'hwid': 'c'}])
    with pytest.raises(HTTPException) as exc:
        await devices_routes.reduce_devices(
            devices_routes.ReduceDevicesRequest(new_device_limit=2, hwids_to_remove=['zzz']),
            subscription_id=None,
            user=SimpleNamespace(id=1),
            db=AsyncMock(),
        )
    assert exc.value.status_code == 400
    assert api.removed == []


@pytest.mark.asyncio
async def test_no_refund_when_limit_did_not_change(reduce_env):
    """Повтор запроса не должен вернуть деньги дважды."""
    api, subscription, added = reduce_env(devices=[{'hwid': 'a'}], device_limit=2)
    with pytest.raises(HTTPException):
        await devices_routes.reduce_devices(
            devices_routes.ReduceDevicesRequest(new_device_limit=2, hwids_to_remove=None),
            subscription_id=None,
            user=SimpleNamespace(id=1),
            db=AsyncMock(),
        )
    assert added == []
```

Фикстуру `reduce_env` определить в том же файле:

```python
@pytest.fixture
def reduce_env(monkeypatch):
    added: list[dict] = []

    def _build(devices, device_limit=3, min_limit=1, device_price=6000, days_left=30):
        from datetime import UTC, datetime, timedelta

        api = _Api(devices)
        subscription = SimpleNamespace(
            id=1,
            user_id=1,
            remnawave_id=5,
            device_limit=device_limit,
            is_trial=False,
            tariff_id=None,
            end_date=datetime.now(UTC) + timedelta(days=days_left),
            updated_at=None,
        )
        _patch_common(monkeypatch, api, subscription, added)
        monkeypatch.setattr(devices_routes, 'resolve_min_device_limit', lambda tariff: min_limit)
        monkeypatch.setattr(
            devices_routes, '_resolve_device_price_kopeks', AsyncMock(return_value=device_price)
        )

        class _SubService:
            async def update_remnawave_user(self, db, subscription):
                return True

        monkeypatch.setattr(devices_routes, 'SubscriptionService', lambda: _SubService())
        return api, subscription, added

    return _build
```

- [ ] **Step 7: Запустить тесты и убедиться, что они падают**

Run: `.venv/bin/pytest tests/cabinet/test_reduce_devices_refund.py -q`
Expected: FAIL — у `ReduceDevicesRequest` нет `hwids_to_remove`, в ответе нет `refund_kopeks`

- [ ] **Step 8: Поднять импорты на уровень модуля**

Тесты монкейпатчат `devices_routes.add_user_balance` и
`devices_routes.resolve_min_device_limit`. Подмена работает только для имён,
разрешаемых в модуле, поэтому оба импорта обязаны быть в шапке файла, а не
внутри функций.

В `app/cabinet/routes/subscription_modules/devices.py`, в шапку, добавить:

```python
from app.database.crud.user import add_user_balance
from app.utils.device_refund import calculate_device_refund_kopeks
from app.utils.subscription_utils import resolve_min_device_limit
```

и удалить локальный `from app.utils.subscription_utils import resolve_min_device_limit`
внутри `get_device_reduction_info` и `reduce_devices` — иначе локальное имя
перекроет подменённое модульное, и тесты будут зелёными вхолостую.

Также добавить в импорт схем `DeleteDevicesBatchRequest`, если Task 2 ещё не
выполнен, и убедиться, что `ReduceDevicesRequest` импортируется оттуда же.

- [ ] **Step 9: Добавить хелпер цены места**

В том же файле, рядом с другими хелперами модуля:

```python
async def _resolve_device_price_kopeks(db: AsyncSession, subscription: Subscription) -> int:
    """Цена одного места устройства в копейках за базовый период в 30 дней.

    Тарифная цена важнее глобальной: на разных тарифах место стоит по-разному,
    и брать общую настройку значило бы вернуть человеку не те деньги.
    """
    if subscription.tariff_id:
        tariff = await get_tariff_by_id(db, subscription.tariff_id)
        tariff_price = getattr(tariff, 'device_price_kopeks', None) if tariff else None
        if isinstance(tariff_price, int) and not isinstance(tariff_price, bool) and tariff_price > 0:
            return tariff_price
    fallback = getattr(settings, 'PRICE_PER_DEVICE', 0)
    return fallback if isinstance(fallback, int) and fallback > 0 else 0
```

- [ ] **Step 10: Переписать выбор устройств и добавить возврат в `reduce_devices`**

Заменить в `reduce_devices` всё от строки `connected_devices_count = 0` до
строки `old_device_limit = current_device_limit` на код ниже. Остальная часть
обработчика — установка лимита, вызов `update_remnawave_user`, откат при отказе
панели и возврат ответа — остаётся как есть, кроме двух добавлений, указанных
после блока.

```python
    # Сколько мест освобождается и сколько устройств надо отключить — РАЗНЫЕ
    # величины. Мест освобождается «старый лимит минус новый». Устройств
    # отключать надо «сколько подключено минус новый лимит», и это число бывает
    # меньше, а бывает нулём, если человек не выбрал весь лимит.
    freed_slots = current_device_limit - new_device_limit

    connected_devices_count = 0
    devices_removed_count = 0
    devices_list: list[dict[str, Any]] = []

    service = RemnaWaveService()
    async with service.get_api_client() as api:
        _panel_user_id = await _ensure_panel_user_id(db, subscription, user, api)
        if _panel_user_id:
            response = await api.get_user_devices_all(_panel_user_id)
            devices_list = (response or {}).get('devices', []) or []
            connected_devices_count = len(devices_list)

        devices_to_remove_count = max(0, connected_devices_count - new_device_limit)
        known_hwids = {d.get('hwid') for d in devices_list if d.get('hwid')}

        if request.hwids_to_remove is not None:
            chosen: list[str] = []
            seen: set[str] = set()
            for hwid in request.hwids_to_remove:
                if hwid and hwid not in seen:
                    seen.add(hwid)
                    chosen.append(hwid)

            unknown = [hwid for hwid in chosen if hwid not in known_hwids]
            if unknown:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f'Неизвестные устройства: {", ".join(unknown)}',
                )
            if len(chosen) != devices_to_remove_count:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f'Нужно отключить ровно {devices_to_remove_count} устройств, '
                        f'выбрано {len(chosen)}'
                    ),
                )
            devices_to_delete = [d for d in devices_list if d.get('hwid') in seen]
        else:
            # Совместимость со старыми клиентами: выбираем сами. Сортировка по
            # возрастанию активности, срез С НАЧАЛА — отключаем то, чем давно не
            # пользовались. Прежний код срезал с конца и удалял самые свежие
            # устройства, то есть ровно те, что были нужны человеку.
            sorted_devices = sorted(
                devices_list,
                key=lambda d: d.get('updatedAt') or d.get('createdAt') or '\xff',
            )
            devices_to_delete = sorted_devices[:devices_to_remove_count]

        for device in devices_to_delete:
            device_hwid = device.get('hwid')
            if not device_hwid:
                continue
            try:
                if await api.remove_device(_panel_user_id, device_hwid):
                    devices_removed_count += 1
            except Exception as del_error:
                logger.error(
                    'Error removing device during limit reduction',
                    device_hwid=device_hwid,
                    user_id=user.id,
                    error=str(del_error)[:200],
                )

    # Возврат считаем ДО изменения лимита: после присваивания freed_slots уже не
    # восстановить. Формула зеркальна покупке — цена места на оставшийся срок.
    now = datetime.now(UTC)
    end_date = subscription.end_date
    if end_date is not None and end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=UTC)
    days_left = max(0, math.ceil((end_date - now).total_seconds() / 86400)) if end_date else 0
    device_price_kopeks = await _resolve_device_price_kopeks(db, subscription)
    refund_kopeks = calculate_device_refund_kopeks(
        device_price_kopeks, slots=freed_slots, days_left=days_left
    )
```

После этого блока, сразу после существующей строки
`subscription.updated_at = datetime.now(UTC)`, добавить начисление возврата:

```python
    if refund_kopeks > 0:
        # commit=False: деньги и лимит меняются одной транзакцией. Если панель
        # откажет и мы откатимся, возврат откатится вместе с лимитом, иначе у
        # человека остались бы деньги за места, которые он не потерял.
        await add_user_balance(
            db=db,
            user=user,
            amount_kopeks=refund_kopeks,
            description=f'Возврат за {freed_slots} освобождённых мест устройств',
            create_transaction=True,
            transaction_type=TransactionType.REFUND,
            commit=False,
        )
```

и в словарь ответа добавить строку:

```python
        'refund_kopeks': refund_kopeks,
```

**Идемпотентность.** Строка подписки уже блокируется `with_for_update()`, а
проверка `new_device_limit >= current_device_limit` стоит внутри блокировки и
даёт 400. Значит повторный запрос с тем же лимитом не дойдёт до начисления и
деньги не вернутся дважды. Отдельного флага не нужно — проверить, что порядок
именно такой, и не переносить проверку выше блокировки.

- [ ] **Step 11: Показать сумму возврата до нажатия**

В `get_device_reduction_info`, в успешный ответ (там, где отдаются
`current_device_limit`, `min_device_limit`, `can_reduce`), добавить поле:

```python
        'refund_kopeks_per_slot': calculate_device_refund_kopeks(
            await _resolve_device_price_kopeks(db, subscription), slots=1, days_left=days_left
        ),
```

где `days_left` вычисляется тем же способом, что и в `reduce_devices`. Экран
умножает это на число освобождаемых мест и показывает сумму до нажатия — иначе
человек узнаёт размер возврата только постфактум.

- [ ] **Step 12: Запустить тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/cabinet/test_reduce_devices_refund.py tests/utils/test_device_refund.py -q`
Expected: PASS

- [ ] **Step 13: Проверить компиляцию и полный набор тестов**

Run:
```bash
.venv/bin/python -m py_compile app/cabinet/routes/subscription_modules/devices.py app/cabinet/schemas/subscription.py app/utils/device_refund.py
.venv/bin/pytest tests/ -q --ignore=tests/unit/test_price_calculation_parity.py
```
Expected: компиляция чистая; 42 падения, те же, что в базовой линии

- [ ] **Step 14: Коммит**

```bash
git add app/cabinet/routes/subscription_modules/devices.py app/cabinet/schemas/subscription.py app/utils/device_refund.py tests/utils/test_device_refund.py tests/cabinet/test_reduce_devices_refund.py
git commit -F - <<'EOF'
feat(devices): уменьшение лимита с выбором устройств и возвратом денег

Две правки в одной транзакции, потому что обе меняют один обработчик.

Возврат. Раньше уменьшение лимита не возвращало ничего, хотя покупка места
пропорциональна оставшимся дням: человек доплачивал за место на остаток месяца,
через день передумывал и терял всё. Теперь возврат считается ЗЕРКАЛЬНОЙ
формулой — цена места на оставшийся срок — и начисляется на баланс через
add_user_balance с типом REFUND, то есть оставляет след в истории операций.

Истории покупок устройств в базе нет (Transaction.description — свободный
текст), поэтому считаем от текущей цены, а не от уплаченного. Разница — цена
одного дня использования, в пользу клиента.

Выбор устройств. Раньше обработчик выбирал жертв сам и, из-за среза с конца
отсортированного по активности списка, удалял САМЫЕ СВЕЖИЕ устройства — то,
чем человек только что пользовался. Теперь список hwid приходит от него, а
несовпадение количества или неизвестный hwid дают отказ до похода в панель и до
начисления денег.

Идемпотентность. Возврат и смена лимита идут одной транзакцией под блокировкой
строки подписки, а повторная проверка внутри блокировки не даёт начислить
деньги дважды при повторе запроса.
EOF
```

---

### Task 4: Готовые суммы пополнения из цен тарифов

**Files:**
- Create: `app/cabinet/routes/topup_presets.py`
- Modify: `app/cabinet/routes/__init__.py` (подключение роутера)
- Test: `tests/cabinet/test_topup_presets.py`

**Interfaces:**
- Consumes: `get_tariffs_for_user` из `app/database/crud/tariff.py:131`, `Tariff.period_prices` (JSON `{"30": 50000}`, копейки), `settings.CLASSIC_PERIOD_PRICES` из `app/config.py:4152`
- Produces: `GET /cabinet/balance/topup-presets` → `{"presets": [{"amount_kopeks": int, "label_days": int}], "sales_mode": "classic"|"tariffs"}`

**Зачем:** экран пополнения предлагает готовые суммы, равные цене тарифов, — пополнил ровно на тариф и купил без остатка на балансе. Готовых сумм в конфиге нет, поэтому их надо вывести из действующих цен.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/cabinet/test_topup_presets.py`:

```python
"""Готовые суммы пополнения выводятся из цен действующих тарифов.

Смысл в том, чтобы человек пополнил ровно на тариф и купил без остатка на
балансе. Поэтому суммы — это реальные цены периодов, а не круглые числа.
"""

from app.cabinet.routes.topup_presets import build_presets


def test_presets_are_sorted_and_deduplicated():
    presets = build_presets({30: 24900, 90: 64900, 180: 119000})
    assert [p['amount_kopeks'] for p in presets] == [24900, 64900, 119000]
    assert [p['label_days'] for p in presets] == [30, 90, 180]


def test_identical_prices_collapse_keeping_shortest_period():
    """Две одинаковые цены — одна кнопка, и подписана коротким периодом."""
    presets = build_presets({30: 24900, 60: 24900})
    assert presets == [{'amount_kopeks': 24900, 'label_days': 30}]


def test_zero_and_negative_prices_are_dropped():
    presets = build_presets({30: 0, 90: -100, 180: 119000})
    assert presets == [{'amount_kopeks': 119000, 'label_days': 180}]


def test_empty_input_gives_empty_list():
    assert build_presets({}) == []
    assert build_presets(None) == []


def test_string_keys_from_json_are_accepted():
    """period_prices приходит из JSON-колонки, где ключи строковые."""
    presets = build_presets({'30': 24900, '90': 64900})
    assert [p['label_days'] for p in presets] == [30, 90]


def test_garbage_entries_are_ignored_not_fatal():
    presets = build_presets({'abc': 1000, 30: 'free', 90: 64900})
    assert presets == [{'amount_kopeks': 64900, 'label_days': 90}]


def test_more_than_four_presets_are_trimmed():
    """Больше четырёх кнопок в ряд не помещается — берём самые ходовые периоды."""
    presets = build_presets({30: 100, 90: 200, 180: 300, 360: 400, 720: 500})
    assert len(presets) == 4
    assert [p['label_days'] for p in presets] == [30, 90, 180, 360]
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/cabinet/test_topup_presets.py -q`
Expected: FAIL с `ModuleNotFoundError`

- [ ] **Step 3: Написать модуль и эндпоинт**

Создать `app/cabinet/routes/topup_presets.py`:

```python
"""Готовые суммы пополнения баланса.

Суммы равны ценам действующих периодов, чтобы человек пополнил ровно на тариф и
купил без остатка на балансе. Готовых сумм в конфиге нет — выводим из цен.
"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.cabinet.dependencies import get_cabinet_db, get_current_cabinet_user
from app.config import settings
from app.database.models import User

logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/balance', tags=['balance'])

# Больше четырёх кнопок в ряд на телефоне не помещается.
_MAX_PRESETS = 4


def build_presets(period_prices: object) -> list[dict[str, int]]:
    """Собрать список готовых сумм из карты «дни → цена в копейках».

    Ключи бывают строковыми: period_prices хранится в JSON-колонке. Мусор
    пропускаем молча — из-за одной кривой записи экран пополнения не должен
    падать, он и без готовых сумм работоспособен.
    """
    if not isinstance(period_prices, dict):
        return []

    by_amount: dict[int, int] = {}
    for raw_days, raw_price in period_prices.items():
        try:
            days = int(raw_days)
            price = int(raw_price)
        except (TypeError, ValueError):
            continue
        if isinstance(raw_price, bool) or price <= 0 or days <= 0:
            continue
        # Одинаковые цены схлопываем, оставляя короткий период: две кнопки с
        # одной суммой выглядят как ошибка интерфейса.
        if price not in by_amount or days < by_amount[price]:
            by_amount[price] = days

    presets = [
        {'amount_kopeks': amount, 'label_days': days}
        for amount, days in sorted(by_amount.items(), key=lambda item: item[0])
    ]
    return presets[:_MAX_PRESETS]


@router.get('/topup-presets')
async def get_topup_presets(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
) -> dict[str, Any]:
    """Готовые суммы пополнения для текущего пользователя."""
    sales_mode = settings.get_sales_mode()

    period_prices: dict[Any, Any] = {}
    if sales_mode == 'tariffs':
        from app.database.crud.tariff import get_tariffs_for_user

        try:
            tariffs = await get_tariffs_for_user(db, promo_group_id=getattr(user, 'promo_group_id', None))
        except Exception as tariff_error:
            logger.warning(
                'Failed to load tariffs for topup presets',
                user_id=user.id,
                error=str(tariff_error)[:200],
            )
            tariffs = []
        for tariff in tariffs:
            prices = getattr(tariff, 'period_prices', None)
            if isinstance(prices, dict):
                for days, price in prices.items():
                    period_prices.setdefault(days, price)
    else:
        period_prices = dict(getattr(settings, 'CLASSIC_PERIOD_PRICES', {}) or {})

    return {'presets': build_presets(period_prices), 'sales_mode': sales_mode}
```

Подключить роутер в `app/cabinet/routes/__init__.py` рядом с остальными: импорт `from .topup_presets import router as topup_presets_router` и `router.include_router(topup_presets_router)`.

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/cabinet/test_topup_presets.py -q`
Expected: PASS, 7 тестов

- [ ] **Step 5: Проверить компиляцию и полный набор тестов**

Run:
```bash
.venv/bin/python -m py_compile app/cabinet/routes/topup_presets.py app/cabinet/routes/__init__.py
.venv/bin/pytest tests/ -q --ignore=tests/unit/test_price_calculation_parity.py
```
Expected: компиляция чистая; 42 падения, те же, что в базовой линии

- [ ] **Step 6: Коммит**

```bash
git add app/cabinet/routes/topup_presets.py app/cabinet/routes/__init__.py tests/cabinet/test_topup_presets.py
git commit -F - <<'EOF'
feat(balance): готовые суммы пополнения из цен действующих тарифов

Экран пополнения в простом режиме предлагает готовые суммы, равные ценам
периодов: пополнил ровно на тариф и купил без остатка на балансе. Круглые числа
для этого не годятся — они оставляют сдачу, о которой человек не просил.

Готовых сумм в конфиге нет, поэтому выводим их из действующих цен: в тарифном
режиме из period_prices доступных пользователю тарифов, в классическом — из
CLASSIC_PERIOD_PRICES.

Одинаковые цены схлопываются в одну кнопку, подписанную коротким периодом: две
кнопки с одной суммой выглядят как ошибка. Больше четырёх кнопок в ряд на
телефоне не помещается, поэтому список подрезается.
EOF
```

---

## Проверка после всей волны

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
.venv/bin/pytest tests/ -q --ignore=tests/unit/test_price_calculation_parity.py
```

Ручная проверка на стенде:

```bash
# клиент в списке устройств
curl -s -H "Authorization: Bearer <токен>" https://<кабинет>/api/cabinet/subscription/devices | jq '.devices[0]'
# готовые суммы пополнения
curl -s -H "Authorization: Bearer <токен>" https://<кабинет>/api/cabinet/balance/topup-presets
```

## Что не входит

- Экраны кабинета — волна 3.
- Тумблер `CABINET_LITE_MODE_ENABLED` в админке — отдельная задача, долг волны 1.
- Хранение истории покупок устройств ради возврата «от уплаченного» — сознательно
  отклонено: отдельная таблица и учёт при каждой покупке ради разницы в цену
  одного дня.
