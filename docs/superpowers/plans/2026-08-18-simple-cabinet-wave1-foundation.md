# Простой режим кабинета — волна 1: каркас

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать пользователю переключатель «простой / полный интерфейс», который переживает смену устройства, и свернуть навигацию простого режима до четырёх вкладок.

**Architecture:** Два уровня. Персональный выбор хранится в новой колонке `User.cabinet_ui_mode` (nullable: `NULL` — не выбирал). Глобальный дефолт — существующий системный флаг `CABINET_LITE_MODE_ENABLED`, у которого уже есть эндпоинты в `branding.py`, но нет ни одного потребителя. Эффективный режим = персональный выбор, иначе глобальный дефолт. Фронтенд читает режим одним хуком с кэшем в localStorage, чтобы навигация не прыгала на холодном старте.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.x, Alembic; React 19, TypeScript, React Query (`@tanstack/react-query`), Zustand.

**Spec:** `docs/superpowers/specs/2026-08-18-simple-cabinet-mode-design.md`

## Global Constraints

- Репозиторий бота: `/Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot`, венв `.venv`, тесты `.venv/bin/pytest`.
- Репозиторий кабинета: `/Users/mihail/Desktop/Serv/bedolaga-cabinet`, проверка типов `npx tsc --noEmit`, тесты `npm test`.
- **Запрещено** использовать `git stash`, `git checkout`, `git restore`, `git reset`, `git clean` — в репозиториях бывают незакоммиченные правки других участников.
- Проект — форк. Имя файла миграции обязано попадать под маску `^(0\d{3}_|9\d{3}_)` из `app/database/migrations.py:129`, иначе мигратор форка её не увидит. Используем префикс `9029_`.
- Базовая линия тестов бота: **42 падения**, существуют независимо от этой работы. Модуль `tests/unit/test_price_calculation_parity.py` не собирается (импортирует несуществующую `calculate_subscription_total_cost`) — запускать с `--ignore=tests/unit/test_price_calculation_parity.py`. Новых падений быть не должно.
- Значения режима — ровно две строки: `'simple'` и `'advanced'`. Никаких других вариантов, никакого `NULL` в API-ответе поля `mode`.
- Все комментарии и докстрринги — на русском, как в окружающем коде. Commit-сообщения на русском, заголовок плюс тело.
- **Запрещено** добавлять trailer `Co-Authored-By` в commit-сообщения.
- Ничего не пушить. Только локальные коммиты.

---

### Task 1: Колонка `cabinet_ui_mode` и миграция

**Files:**
- Modify: `app/database/models.py` (класс `User`, рядом с `language`)
- Create: `migrations/alembic/versions/9029_user_cabinet_ui_mode.py`
- Test: `tests/database/test_cabinet_ui_mode_column.py`

**Interfaces:**
- Consumes: ничего
- Produces: `User.cabinet_ui_mode: str | None` — колонка `String(16)`, nullable, без server_default.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/database/test_cabinet_ui_mode_column.py`:

```python
"""Колонка персонального выбора интерфейса.

NULL — осознанное состояние «человек не выбирал», а не отсутствие данных:
такие пользователи слушают глобальный дефолт и подхватывают его смену.
Поэтому у колонки нет server_default и она nullable.
"""

from sqlalchemy import String

from app.database.models import User


def test_user_has_cabinet_ui_mode_column():
    column = User.__table__.columns['cabinet_ui_mode']
    assert isinstance(column.type, String)
    assert column.type.length == 16
    assert column.nullable is True
    assert column.server_default is None
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `.venv/bin/pytest tests/database/test_cabinet_ui_mode_column.py -q`
Expected: FAIL с `KeyError: 'cabinet_ui_mode'`

- [ ] **Step 3: Добавить колонку в модель**

В `app/database/models.py`, в классе `User`, сразу после строки `language = Column(String(5), default='ru')`:

```python
    # Персональный выбор интерфейса кабинета: 'simple' | 'advanced' | NULL.
    # NULL означает «не выбирал» — такой пользователь слушает глобальный флаг
    # CABINET_LITE_MODE_ENABLED и подхватит его смену. Явный выбор человека
    # глобальный флаг перебить не может.
    cabinet_ui_mode = Column(String(16), nullable=True)
```

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `.venv/bin/pytest tests/database/test_cabinet_ui_mode_column.py -q`
Expected: PASS

- [ ] **Step 5: Написать миграцию**

Создать `migrations/alembic/versions/9029_user_cabinet_ui_mode.py`:

```python
"""колонка users.cabinet_ui_mode — персональный выбор интерфейса кабинета

Хранит 'simple' или 'advanced'. NULL — «человек не выбирал»: он слушает
глобальный флаг CABINET_LITE_MODE_ENABLED и подхватит его смену. Поэтому
бэкфилла нет и server_default не ставится: проставить всем 'advanced' значило
бы навсегда отрезать существующую базу от глобального переключателя.

Revision ID: 9029
Revises: 9028
Create Date: 2026-08-18

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '9029'
down_revision: Union[str, Sequence[str], None] = '9028'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('cabinet_ui_mode', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'cabinet_ui_mode')
```

- [ ] **Step 6: Проверить, что миграция видна мигратору форка и синтаксически цела**

Run:
```bash
.venv/bin/python -m py_compile migrations/alembic/versions/9029_user_cabinet_ui_mode.py
.venv/bin/python -c "
import re
p = re.compile(r'^(0\d{3}_|9\d{3}_)')
assert p.match('9029_user_cabinet_ui_mode.py'), 'миграция не попадает под маску форка'
print('маска ок')
"
```
Expected: обе команды без ошибок, печатается «маска ок»

- [ ] **Step 7: Прогнать полный набор тестов**

Run: `.venv/bin/pytest tests/ -q --ignore=tests/unit/test_price_calculation_parity.py`
Expected: 42 падения, ровно те же, что в базовой линии

- [ ] **Step 8: Коммит**

```bash
git add app/database/models.py migrations/alembic/versions/9029_user_cabinet_ui_mode.py tests/database/test_cabinet_ui_mode_column.py
git commit -F - <<'EOF'
feat(cabinet): колонка cabinet_ui_mode — персональный выбор интерфейса

Хранит 'simple' или 'advanced'. NULL означает «человек не выбирал»: такой
пользователь слушает глобальный флаг CABINET_LITE_MODE_ENABLED и подхватит
его смену.

Бэкфилла нет намеренно. Проставить всей базе 'advanced' значило бы навсегда
отрезать существующих пользователей от глобального переключателя — включить
простой режим по умолчанию стало бы невозможно.
EOF
```

---

### Task 2: Резолвинг эффективного режима

**Files:**
- Create: `app/utils/ui_mode.py`
- Test: `tests/utils/test_ui_mode.py`

**Interfaces:**
- Consumes: `User.cabinet_ui_mode` из Task 1
- Produces:
  - `UI_MODE_SIMPLE: str = 'simple'`, `UI_MODE_ADVANCED: str = 'advanced'`, `UI_MODES: tuple[str, str]`
  - `normalize_ui_mode(value: object) -> str | None` — приводит вход к допустимому значению или `None`
  - `resolve_ui_mode(user_choice: str | None, *, lite_mode_enabled: bool) -> str` — всегда возвращает `'simple'` или `'advanced'`

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/utils/test_ui_mode.py`:

```python
"""Резолвинг эффективного режима кабинета.

Правило: явный выбор человека сильнее глобального дефолта. Глобальный флаг
действует только на тех, кто не выбирал (choice is None).
"""

import pytest

from app.utils.ui_mode import (
    UI_MODE_ADVANCED,
    UI_MODE_SIMPLE,
    normalize_ui_mode,
    resolve_ui_mode,
)


@pytest.mark.parametrize('lite_enabled', [True, False])
def test_explicit_choice_wins_over_global_default(lite_enabled):
    assert resolve_ui_mode(UI_MODE_SIMPLE, lite_mode_enabled=lite_enabled) == UI_MODE_SIMPLE
    assert resolve_ui_mode(UI_MODE_ADVANCED, lite_mode_enabled=lite_enabled) == UI_MODE_ADVANCED


def test_no_choice_follows_global_default():
    assert resolve_ui_mode(None, lite_mode_enabled=True) == UI_MODE_SIMPLE
    assert resolve_ui_mode(None, lite_mode_enabled=False) == UI_MODE_ADVANCED


@pytest.mark.parametrize('garbage', ['', '  ', 'lite', 'SIMPLE_MODE', 'null', 0, 1, [], {}])
def test_garbage_choice_falls_back_to_global_default(garbage):
    """Мусор в колонке не должен ронять кабинет и не должен молча значить 'simple'."""
    assert resolve_ui_mode(garbage, lite_mode_enabled=False) == UI_MODE_ADVANCED
    assert resolve_ui_mode(garbage, lite_mode_enabled=True) == UI_MODE_SIMPLE


@pytest.mark.parametrize(
    'raw, expected',
    [
        ('simple', UI_MODE_SIMPLE),
        ('advanced', UI_MODE_ADVANCED),
        ('  simple  ', UI_MODE_SIMPLE),
        ('SIMPLE', UI_MODE_SIMPLE),
        ('Advanced', UI_MODE_ADVANCED),
        (None, None),
        ('', None),
        ('lite', None),
        (5, None),
    ],
)
def test_normalize_ui_mode(raw, expected):
    assert normalize_ui_mode(raw) == expected
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `.venv/bin/pytest tests/utils/test_ui_mode.py -q`
Expected: FAIL с `ModuleNotFoundError: No module named 'app.utils.ui_mode'`

- [ ] **Step 3: Написать модуль**

Создать `app/utils/ui_mode.py`:

```python
"""Эффективный режим интерфейса кабинета.

Два уровня. Персональный выбор человека (`User.cabinet_ui_mode`) сильнее
глобального дефолта (системный флаг CABINET_LITE_MODE_ENABLED). Глобальный флаг
действует только на тех, кто ничего не выбирал, — благодаря этому простой режим
можно включить всей базе одним тумблером и так же откатить, не затерев выбор
тех, кто осознанно вернулся на полный интерфейс.
"""

from __future__ import annotations

UI_MODE_SIMPLE = 'simple'
UI_MODE_ADVANCED = 'advanced'
UI_MODES: tuple[str, str] = (UI_MODE_SIMPLE, UI_MODE_ADVANCED)


def normalize_ui_mode(value: object) -> str | None:
    """Привести значение к допустимому режиму или к None.

    None возвращается и для «не выбирал», и для мусора: снаружи оба случая
    означают одно — слушать глобальный дефолт. Мусор в колонке не должен
    ронять кабинет и не должен молча трактоваться как 'simple'.
    """
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in UI_MODES else None


def resolve_ui_mode(user_choice: object, *, lite_mode_enabled: bool) -> str:
    """Вернуть режим, в котором кабинет должен отрисоваться прямо сейчас.

    Возвращает всегда одну из двух строк — наружу None не выходит, чтобы
    потребителям не приходилось повторять правило дефолта у себя.
    """
    normalized = normalize_ui_mode(user_choice)
    if normalized is not None:
        return normalized
    return UI_MODE_SIMPLE if lite_mode_enabled else UI_MODE_ADVANCED
```

- [ ] **Step 4: Запустить тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/utils/test_ui_mode.py -q`
Expected: PASS, 22 теста

- [ ] **Step 5: Коммит**

```bash
git add app/utils/ui_mode.py tests/utils/test_ui_mode.py
git commit -F - <<'EOF'
feat(cabinet): резолвинг эффективного режима интерфейса

Персональный выбор сильнее глобального дефолта, глобальный флаг действует
только на тех, кто не выбирал. Мусор в колонке трактуется как «не выбирал»,
а не как 'simple': кабинет из-за испорченного значения не должен молча
переключать человеку интерфейс.
EOF
```

---

### Task 3: Эндпоинты режима и поле в профиле

**Files:**
- Modify: `app/cabinet/routes/info.py` (после `update_user_language`, около строки 380)
- Modify: `app/cabinet/routes/auth.py:118-131` (сборка профиля)
- Modify: `app/cabinet/schemas/auth.py:140-155` (схема профиля)
- Test: `tests/cabinet/test_ui_mode_endpoints.py`

**Interfaces:**
- Consumes: `resolve_ui_mode`, `normalize_ui_mode`, `UI_MODES` из Task 2; `User.cabinet_ui_mode` из Task 1
- Produces:
  - `GET /cabinet/info/user/ui-mode` → `{"mode": "simple"|"advanced", "choice": "simple"|"advanced"|null, "global_default": "simple"|"advanced"}`
  - `PATCH /cabinet/info/user/ui-mode` с телом `{"mode": "simple"|"advanced"|null}` → тот же объект
  - поле `cabinet_ui_mode: str | None` в ответе профиля

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/cabinet/test_ui_mode_endpoints.py`:

```python
"""Эндпоинты персонального выбора интерфейса.

Ответ всегда несёт три величины: mode — что рисовать сейчас, choice — что
человек выбрал явно (null, если не выбирал), global_default — куда его
отправит глобальный флаг, если он сбросит выбор. Без choice фронт не сможет
отличить «выбрал полный» от «не выбирал при выключенном флаге».
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import info as info_routes


def _user(choice=None):
    return SimpleNamespace(id=1, telegram_id=782789067, cabinet_ui_mode=choice)


def _db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_get_returns_global_default_when_user_did_not_choose(monkeypatch):
    monkeypatch.setattr(info_routes, '_read_lite_mode_enabled', AsyncMock(return_value=True))
    result = await info_routes.get_user_ui_mode(user=_user(None), db=_db())
    assert result == {'mode': 'simple', 'choice': None, 'global_default': 'simple'}


@pytest.mark.asyncio
async def test_get_explicit_choice_beats_global_default(monkeypatch):
    monkeypatch.setattr(info_routes, '_read_lite_mode_enabled', AsyncMock(return_value=True))
    result = await info_routes.get_user_ui_mode(user=_user('advanced'), db=_db())
    assert result == {'mode': 'advanced', 'choice': 'advanced', 'global_default': 'simple'}


@pytest.mark.asyncio
async def test_patch_saves_choice(monkeypatch):
    monkeypatch.setattr(info_routes, '_read_lite_mode_enabled', AsyncMock(return_value=False))
    user = _user(None)
    db = _db()
    result = await info_routes.update_user_ui_mode({'mode': 'simple'}, user=user, db=db)
    assert user.cabinet_ui_mode == 'simple'
    assert result == {'mode': 'simple', 'choice': 'simple', 'global_default': 'advanced'}
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_patch_null_resets_choice_to_global_default(monkeypatch):
    """Сброс выбора возвращает человека под глобальный флаг, а не в 'advanced'."""
    monkeypatch.setattr(info_routes, '_read_lite_mode_enabled', AsyncMock(return_value=True))
    user = _user('advanced')
    db = _db()
    result = await info_routes.update_user_ui_mode({'mode': None}, user=user, db=db)
    assert user.cabinet_ui_mode is None
    assert result == {'mode': 'simple', 'choice': None, 'global_default': 'simple'}


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['lite', '', 'SIMPLE_MODE', 5])
async def test_patch_rejects_invalid_mode(monkeypatch, bad):
    monkeypatch.setattr(info_routes, '_read_lite_mode_enabled', AsyncMock(return_value=False))
    user = _user(None)
    with pytest.raises(HTTPException) as exc:
        await info_routes.update_user_ui_mode({'mode': bad}, user=user, db=_db())
    assert exc.value.status_code == 400
    assert user.cabinet_ui_mode is None
```

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

Run: `.venv/bin/pytest tests/cabinet/test_ui_mode_endpoints.py -q`
Expected: FAIL с `AttributeError: module 'app.cabinet.routes.info' has no attribute 'get_user_ui_mode'`

- [ ] **Step 3: Добавить эндпоинты**

В `app/cabinet/routes/info.py`, сразу после функции `update_user_language`, добавить:

```python
async def _read_lite_mode_enabled(db: AsyncSession) -> bool:
    """Глобальный дефолт интерфейса из системных настроек.

    Ключ тот же, что у существующих branding-эндпоинтов CABINET_LITE_MODE_ENABLED,
    чтобы не заводить второй конкурирующий флаг. Недоступность базы трактуем как
    «выключен»: из-за сбоя настроек нельзя молча переключить всем интерфейс.
    """
    from app.cabinet.routes.branding import LITE_MODE_ENABLED_KEY
    from app.database.crud.system_setting import get_setting_value

    try:
        raw = await get_setting_value(db, LITE_MODE_ENABLED_KEY)
    except Exception:
        return False
    return str(raw).strip().lower() == 'true' if raw is not None else False


def _ui_mode_payload(user: User, lite_mode_enabled: bool) -> dict[str, str | None]:
    choice = normalize_ui_mode(getattr(user, 'cabinet_ui_mode', None))
    return {
        'mode': resolve_ui_mode(choice, lite_mode_enabled=lite_mode_enabled),
        'choice': choice,
        'global_default': UI_MODE_SIMPLE if lite_mode_enabled else UI_MODE_ADVANCED,
    }


@router.get('/user/ui-mode')
async def get_user_ui_mode(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Текущий режим интерфейса кабинета."""
    return _ui_mode_payload(user, await _read_lite_mode_enabled(db))


@router.patch('/user/ui-mode')
async def update_user_ui_mode(
    request: dict,
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Сохранить персональный выбор интерфейса.

    `mode: null` сбрасывает выбор — человек снова слушает глобальный дефолт.
    Это не то же самое, что выбрать 'advanced': сброшенный выбор подхватит
    смену глобального флага, явный — нет.
    """
    raw_mode = request.get('mode')
    if raw_mode is None:
        user.cabinet_ui_mode = None
    else:
        normalized = normalize_ui_mode(raw_mode)
        if normalized is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Invalid mode. Supported: {", ".join(UI_MODES)}',
            )
        user.cabinet_ui_mode = normalized

    await db.commit()
    await db.refresh(user)

    return _ui_mode_payload(user, await _read_lite_mode_enabled(db))
```

В шапку `app/cabinet/routes/info.py` добавить импорт:

```python
from app.utils.ui_mode import UI_MODE_SIMPLE, UI_MODES, normalize_ui_mode, resolve_ui_mode
```

- [ ] **Step 4: Запустить тесты и убедиться, что они проходят**

Run: `.venv/bin/pytest tests/cabinet/test_ui_mode_endpoints.py -q`
Expected: PASS, 8 тестов

- [ ] **Step 5: Добавить поле в схему профиля**

В `app/cabinet/schemas/auth.py`, в класс профиля, сразу после `language: str = 'ru'`:

```python
    cabinet_ui_mode: str | None = None  # 'simple' | 'advanced' | None (не выбирал)
```

- [ ] **Step 6: Отдавать поле в профиле**

В `app/cabinet/routes/auth.py`, в сборке ответа профиля, сразу после `language=user.language,`:

```python
        cabinet_ui_mode=getattr(user, 'cabinet_ui_mode', None),
```

- [ ] **Step 7: Проверить компиляцию и полный набор тестов**

Run:
```bash
.venv/bin/python -m py_compile app/cabinet/routes/info.py app/cabinet/routes/auth.py app/cabinet/schemas/auth.py app/utils/ui_mode.py
.venv/bin/pytest tests/ -q --ignore=tests/unit/test_price_calculation_parity.py
```
Expected: компиляция чистая; 42 падения, ровно те же, что в базовой линии

- [ ] **Step 8: Коммит**

```bash
git add app/cabinet/routes/info.py app/cabinet/routes/auth.py app/cabinet/schemas/auth.py tests/cabinet/test_ui_mode_endpoints.py
git commit -F - <<'EOF'
feat(cabinet): эндпоинты выбора интерфейса и поле в профиле

GET и PATCH /cabinet/info/user/ui-mode по образцу уже существующего
/cabinet/info/user/language. Ответ несёт три величины: mode — что рисовать
сейчас, choice — явный выбор человека, global_default — куда его отправит
глобальный флаг при сбросе выбора.

Без choice фронт не отличил бы «выбрал полный интерфейс» от «не выбирал при
выключенном глобальном флаге», а это разные состояния: первое переживает
включение флага, второе — нет.

Глобальный дефолт читается из существующего CABINET_LITE_MODE_ENABLED, чтобы
не плодить второй конкурирующий флаг.
EOF
```

---

### Task 4: Клиент и хук `useUiMode` в кабинете

**Files:**
- Modify: `/Users/mihail/Desktop/Serv/bedolaga-cabinet/src/api/info.ts` (рядом с методами языка, около строки 98-115)
- Create: `/Users/mihail/Desktop/Serv/bedolaga-cabinet/src/hooks/useUiMode.ts`
- Test: `/Users/mihail/Desktop/Serv/bedolaga-cabinet/src/hooks/useUiMode.test.ts`

**Interfaces:**
- Consumes: `GET`/`PATCH /cabinet/info/user/ui-mode` из Task 3
- Produces:
  - `export type UiMode = 'simple' | 'advanced'`
  - `export interface UiModeResponse { mode: UiMode; choice: UiMode | null; global_default: UiMode }`
  - `infoApi.getUiMode(): Promise<UiModeResponse>`, `infoApi.updateUiMode(mode: UiMode | null): Promise<UiModeResponse>`
  - `useUiMode(): { mode: UiMode; choice: UiMode | null; globalDefault: UiMode; isSimple: boolean; setMode: (mode: UiMode | null) => void; isSaving: boolean }`

- [ ] **Step 1: Добавить методы в API-клиент**

В `src/api/info.ts` добавить типы и методы рядом с методами языка:

```ts
export type UiMode = 'simple' | 'advanced';

export interface UiModeResponse {
  mode: UiMode;
  choice: UiMode | null;
  global_default: UiMode;
}
```

и внутрь объекта `infoApi`, сразу после `updateUserLanguage`:

```ts
  // Get effective cabinet UI mode
  getUiMode: async (): Promise<UiModeResponse> => {
    const response = await apiClient.get<UiModeResponse>('/cabinet/info/user/ui-mode');
    return response.data;
  },

  // Save personal UI mode choice. `null` resets to the global default.
  updateUiMode: async (mode: UiMode | null): Promise<UiModeResponse> => {
    const response = await apiClient.patch<UiModeResponse>('/cabinet/info/user/ui-mode', { mode });
    return response.data;
  },
```

- [ ] **Step 2: Написать падающий тест хука**

Создать `src/hooks/useUiMode.test.ts`:

```ts
import { describe, expect, it, beforeEach } from 'vitest';
import { readUiModeCache, writeUiModeCache, UI_MODE_CACHE_KEY } from './useUiMode';

describe('кэш режима интерфейса', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('без записи отдаёт advanced — полный кабинет как безопасный дефолт', () => {
    expect(readUiModeCache()).toBe('advanced');
  });

  it('переживает перезагрузку страницы', () => {
    writeUiModeCache('simple');
    expect(readUiModeCache()).toBe('simple');
  });

  it('игнорирует испорченное значение вместо того, чтобы верить ему', () => {
    localStorage.setItem(UI_MODE_CACHE_KEY, 'lite');
    expect(readUiModeCache()).toBe('advanced');
  });
});
```

- [ ] **Step 3: Запустить тест и убедиться, что он падает**

Run: `cd /Users/mihail/Desktop/Serv/bedolaga-cabinet && npx vitest run src/hooks/useUiMode.test.ts`
Expected: FAIL, модуль `./useUiMode` не найден

- [ ] **Step 4: Написать хук**

Создать `src/hooks/useUiMode.ts`:

```ts
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuthStore } from '@/store/auth';
import { infoApi, type UiMode } from '@/api/info';

// Последний известный режим. Пока запрос в полёте, режим был бы неизвестен, и
// кабинет успевал бы отрисовать полную навигацию, а затем схлопнуть её до
// четырёх вкладок — заметный прыжок на каждом холодном старте. Тот же приём
// уже применён в useFeatureFlags по той же причине.
export const UI_MODE_CACHE_KEY = 'cabinet-ui-mode';

export function readUiModeCache(): UiMode {
  try {
    const raw = localStorage.getItem(UI_MODE_CACHE_KEY);
    return raw === 'simple' || raw === 'advanced' ? raw : 'advanced';
  } catch {
    return 'advanced';
  }
}

export function writeUiModeCache(mode: UiMode): void {
  try {
    localStorage.setItem(UI_MODE_CACHE_KEY, mode);
  } catch {
    /* sandboxed / private */
  }
}

export function useUiMode() {
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const queryClient = useQueryClient();
  const cached = readUiModeCache();

  const { data } = useQuery({
    queryKey: ['ui-mode'],
    queryFn: infoApi.getUiMode,
    enabled: isAuthenticated,
    staleTime: 60000,
  });

  const mutation = useMutation({
    mutationFn: (mode: UiMode | null) => infoApi.updateUiMode(mode),
    onSuccess: (result) => {
      writeUiModeCache(result.mode);
      queryClient.setQueryData(['ui-mode'], result);
    },
  });

  const mode: UiMode = data?.mode ?? cached;
  if (data?.mode && data.mode !== cached) {
    writeUiModeCache(data.mode);
  }

  return {
    mode,
    choice: data?.choice ?? null,
    globalDefault: data?.global_default ?? 'advanced',
    isSimple: mode === 'simple',
    setMode: mutation.mutate,
    isSaving: mutation.isPending,
  };
}
```

- [ ] **Step 5: Запустить тест и проверку типов**

Run:
```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx vitest run src/hooks/useUiMode.test.ts
npx tsc --noEmit
```
Expected: тест PASS (3 кейса), типы чистые

- [ ] **Step 6: Коммит**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
git add src/api/info.ts src/hooks/useUiMode.ts src/hooks/useUiMode.test.ts
git commit -F - <<'EOF'
feat(ui-mode): хук режима интерфейса с кэшем в localStorage

Кэш нужен не для скорости, а против прыжка навигации: пока запрос режима в
полёте, кабинет успевал бы отрисовать полное меню и затем схлопнуть его до
четырёх вкладок. Тот же приём уже применён в useFeatureFlags по той же причине.

Безопасный дефолт — полный кабинет: показать человеку лишнее менее вредно,
чем спрятать нужное из-за неизвестного режима.
EOF
```

---

### Task 5: Четыре вкладки в простом режиме

**Files:**
- Modify: `/Users/mihail/Desktop/Serv/bedolaga-cabinet/src/components/layout/AppShell/AppShell.tsx:118-126` (десктопная капсула)
- Modify: `/Users/mihail/Desktop/Serv/bedolaga-cabinet/src/components/layout/AppShell/MobileBottomNav.tsx:38-46` (мобильный таббар)
- Modify: `/Users/mihail/Desktop/Serv/bedolaga-cabinet/src/components/layout/AppShell/AppHeader.tsx:163-172` (гамбургер-меню)
- Test: `/Users/mihail/Desktop/Serv/bedolaga-cabinet/src/components/layout/AppShell/simpleNavItems.test.ts`
- Create: `/Users/mihail/Desktop/Serv/bedolaga-cabinet/src/components/layout/AppShell/simpleNavItems.ts`

**Interfaces:**
- Consumes: `useUiMode()` из Task 4
- Produces: `SIMPLE_NAV_PATHS: readonly string[]`, `filterNavForSimpleMode<T extends { path: string }>(items: T[]): T[]`

**Форма данных (проверено в коде):** пункты меню — это объекты `{ path, label, icon }`, поле `key` у них отсутствует (`AppShell.tsx:117-126`, `MobileBottomNav.tsx:43-48`). Фильтровать надо по `path`. Пути простого режима: `/`, `/subscriptions`, `/referral`, `/profile`.

- [ ] **Step 1: Написать падающий тест**

Создать `src/components/layout/AppShell/simpleNavItems.test.ts`:

```ts
import { describe, expect, it } from 'vitest';
import { SIMPLE_NAV_PATHS, filterNavForSimpleMode } from './simpleNavItems';

const ALL = [
  { path: '/' },
  { path: '/subscriptions' },
  { path: '/balance' },
  { path: '/referral' },
  { path: '/support' },
  { path: '/contests' },
  { path: '/polls' },
  { path: '/wheel' },
  { path: '/gift' },
  { path: '/info' },
  { path: '/profile' },
];

describe('навигация простого режима', () => {
  it('оставляет ровно четыре раздела', () => {
    expect(filterNavForSimpleMode(ALL).map((i) => i.path)).toEqual([
      '/',
      '/subscriptions',
      '/referral',
      '/profile',
    ]);
  });

  it('сохраняет порядок исходного списка, а не порядок путей', () => {
    const shuffled = [{ path: '/profile' }, { path: '/' }, { path: '/wheel' }];
    expect(filterNavForSimpleMode(shuffled).map((i) => i.path)).toEqual(['/profile', '/']);
  });

  it('не падает на пустом списке', () => {
    expect(filterNavForSimpleMode([])).toEqual([]);
  });

  it('не выдумывает разделы, которых нет во входном списке', () => {
    expect(filterNavForSimpleMode([{ path: '/' }]).map((i) => i.path)).toEqual(['/']);
  });

  it('баланс в простом режиме не отдельный раздел', () => {
    expect(SIMPLE_NAV_PATHS).not.toContain('/balance');
  });

  it('не путает /subscriptions с вложенным маршрутом', () => {
    expect(filterNavForSimpleMode([{ path: '/subscriptions/42' }])).toEqual([]);
  });
});
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `cd /Users/mihail/Desktop/Serv/bedolaga-cabinet && npx vitest run src/components/layout/AppShell/simpleNavItems.test.ts`
Expected: FAIL, модуль `./simpleNavItems` не найден

- [ ] **Step 3: Написать фильтр**

Создать `src/components/layout/AppShell/simpleNavItems.ts`:

```ts
// Четыре раздела простого режима. Каждый соответствует задаче человека, а не
// разделу системы: посмотреть подписку и подключиться, купить или продлить,
// пригласить и вывести, управлять входом.
//
// Баланса в списке нет намеренно: в простом режиме деньги вносятся в момент
// покупки, а не «про запас», поэтому пополнение открывается строкой с главной.
export const SIMPLE_NAV_PATHS = ['/', '/subscriptions', '/referral', '/profile'] as const;

const SIMPLE_NAV_SET: ReadonlySet<string> = new Set(SIMPLE_NAV_PATHS);

// Сравнение строгое, по полному пути. Префиксное совпадение отобрало бы и
// вложенные маршруты вроде /subscriptions/42, которых в меню быть не должно.
export function filterNavForSimpleMode<T extends { path: string }>(items: T[]): T[] {
  return items.filter((item) => SIMPLE_NAV_SET.has(item.path));
}
```

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `cd /Users/mihail/Desktop/Serv/bedolaga-cabinet && npx vitest run src/components/layout/AppShell/simpleNavItems.test.ts`
Expected: PASS, 5 кейсов

- [ ] **Step 5: Применить фильтр в трёх местах навигации**

В каждом из трёх файлов добавить импорт:

```ts
import { useUiMode } from '@/hooks/useUiMode';
import { filterNavForSimpleMode } from './simpleNavItems';
```

и там, где сейчас формируется массив пунктов меню, обернуть результат:

```ts
const { isSimple } = useUiMode();
const visibleNavItems = isSimple ? filterNavForSimpleMode(navItems) : navItems;
```

Дальше рендерить `visibleNavItems` вместо `navItems`. Имена локальных переменных в файлах отличаются — сохранить существующие, менять только источник данных для рендера.

**Важно для `MobileBottomNav.tsx`:** в коде на строках 27-35 есть комментарий, что «Поддержка» обязана оставаться в таббаре, а не уезжать в гамбургер. В простом режиме «Поддержка» уходит из таббара в Профиль — это осознанное изменение, комментарий надо обновить, а не молча нарушить.

**Важно для `AppHeader.tsx`:** админский пункт и «Выйти» рендерятся отдельно от основного списка (около строк 397-412) — их фильтр не касается, вход в админку остаётся доступен админу и в простом режиме.

- [ ] **Step 6: Проверить типы и прогнать тесты**

Run:
```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx tsc --noEmit
npm test
```
Expected: типы чистые; все тесты проходят, новых падений нет

- [ ] **Step 7: Коммит**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
git add src/components/layout/AppShell/
git commit -F - <<'EOF'
feat(ui-mode): четыре вкладки в простом режиме

Сейчас в меню восемь пунктов на десктопе и одиннадцать в мобильном гамбургере.
В простом режиме остаются четыре, и каждый соответствует задаче человека, а не
разделу системы: подписка и подключение, покупка и продление, приглашения и
вывод, управление входом.

Баланс перестаёт быть отдельным разделом: в простом режиме деньги вносятся в
момент покупки, а не «про запас». Пополнение открывается строкой с главной.

Вход в админ-панель фильтром не затронут — он рендерится отдельно от основного
списка и остаётся доступен админу в обоих режимах.
EOF
```

---

### Task 6: Тумблер режима в Профиле

**Files:**
- Modify: `/Users/mihail/Desktop/Serv/bedolaga-cabinet/src/pages/Profile.tsx`
- Modify: `/Users/mihail/Desktop/Serv/bedolaga-cabinet/src/locales/ru.json`, `en.json`, `zh.json`, `fa.json`

**Interfaces:**
- Consumes: `useUiMode()` из Task 4
- Produces: ничего для последующих задач

- [ ] **Step 1: Добавить ключи локализации**

В `src/locales/ru.json`, в секцию профиля:

```json
"uiMode": {
  "title": "Простой интерфейс",
  "description": "Выключите, чтобы открыть все разделы: колесо, конкурсы, подарки и новости",
  "saving": "Сохраняем…"
}
```

В `en.json`:

```json
"uiMode": {
  "title": "Simple interface",
  "description": "Turn off to open all sections: wheel, contests, gifts and news",
  "saving": "Saving…"
}
```

В `zh.json`:

```json
"uiMode": {
  "title": "简洁界面",
  "description": "关闭后可显示全部板块：转盘、活动、赠送和新闻",
  "saving": "保存中…"
}
```

В `fa.json`:

```json
"uiMode": {
  "title": "رابط ساده",
  "description": "برای باز کردن همه بخش‌ها خاموش کنید: گردونه، مسابقات، هدایا و اخبار",
  "saving": "در حال ذخیره…"
}
```

- [ ] **Step 2: Добавить тумблер в Профиль**

В `src/pages/Profile.tsx`, в секцию настроек рядом с существующими переключателями уведомлений, добавить строку. Взять разметку соседнего переключателя в этом же файле, чтобы визуально не выбиваться, и подключить обработчик:

```tsx
const { isSimple, setMode, isSaving } = useUiMode();
```

Разметка — точная копия строки переключателя уведомлений из этого же файла (`Profile.tsx:613-628`), с подставленными ключами. Компонент называется `Switch`, обработчик — `onCheckedChange`, он уже импортирован в файле:

```tsx
<div className="flex items-center justify-between">
  <div>
    <p className="font-medium text-dark-100">{t('profile.uiMode.title')}</p>
    <p className="text-sm text-dark-400">
      {isSaving ? t('profile.uiMode.saving') : t('profile.uiMode.description')}
    </p>
  </div>
  <Switch
    checked={isSimple}
    disabled={isSaving}
    onCheckedChange={(checked) => setMode(checked ? 'simple' : 'advanced')}
  />
</div>
```

Импорт добавить в шапку файла:

```tsx
import { useUiMode } from '@/hooks/useUiMode';
```

- [ ] **Step 3: Проверить типы и тесты**

Run:
```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx tsc --noEmit
npm test
```
Expected: типы чистые; тесты проходят

- [ ] **Step 4: Проверить целостность локалей**

Run: `cd /Users/mihail/Desktop/Serv/bedolaga-cabinet && npx tsc --noEmit && node -e "
const files = ['ru','en','zh','fa'];
const keys = files.map(f => {
  const j = require('./src/locales/' + f + '.json');
  return Object.keys(j.profile?.uiMode ?? {}).sort().join(',');
});
if (new Set(keys).size !== 1) { console.error('Набор ключей uiMode различается между локалями:', keys); process.exit(1); }
console.log('локали согласованы:', keys[0]);
"`
Expected: печатается «локали согласованы: description,saving,title»

- [ ] **Step 5: Коммит**

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
git add src/pages/Profile.tsx src/locales/
git commit -F - <<'EOF'
feat(ui-mode): переключатель интерфейса в профиле

Выключил простой режим — записывается явный 'advanced', и дальше человеку
всегда грузится полный кабинет, независимо от того, что администратор включит
глобально. Это и есть обещание «вернуться назад можно в любой момент».

Ключи добавлены во все четыре локали сразу: в этом проекте частичный перевод
приводит к сырым ключам на экране у части пользователей.
EOF
```

---

## Что дальше

Эта волна даёт работающий переключатель и свёрнутую навигацию, но экраны внутри
разделов остаются прежними. Дальше идут отдельные планы:

- **Волна 2 — доработки в боте.** Название клиента в списке устройств,
  пропорциональный возврат за место, выбор отключаемых устройств,
  bulk-удаление, готовые суммы пополнения из цен тарифов. Не зависит от волны 1
  и может выполняться параллельно.
- **Волна 3 — экраны простого режима.** Главная, подписка, устройства и лимит,
  пополнение, рефералы, история, профиль. Зависит и от волны 1 (каркас), и от
  волны 2 (данные, которых сейчас нет в API).

## Проверка после всей волны

```bash
cd /Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot
.venv/bin/pytest tests/ -q --ignore=tests/unit/test_price_calculation_parity.py

cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx tsc --noEmit && npm test
```

Ручная проверка на стенде: у администратора в Профиле переключить «Простой
интерфейс», убедиться, что меню схлопнулось до четырёх вкладок, вход в
админ-панель остался на месте, а после перезагрузки страницы навигация не
прыгает с полной на короткую.
