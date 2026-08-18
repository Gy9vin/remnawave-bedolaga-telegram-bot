# Простой режим кабинета — волна 3: экраны

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собрать сами экраны простого режима: главная со всеми состояниями подписки, один экран покупки и продления, устройства с лимитом, пополнение, рефералы, история, профиль.

**Architecture:** Отдельные компоненты представления при общем слое данных. Простые экраны живут в `src/components/simple/`, переиспользуют существующие хуки, клиенты API, примитивы интерфейса и тему. Страницы-роуты переключаются наверху: `if (isSimple) return <SimpleX/>`. Расходится только вёрстка — получение данных, форматирование денег, обработка ошибок остаются едиными.

**Tech Stack:** React 19, TypeScript, React Query, framer-motion, Tailwind с токенами через CSS-переменные, i18next, vitest.

**Spec:** `docs/superpowers/specs/2026-08-18-simple-cabinet-mode-design.md`
**Визуальный эталон:** `docs/superpowers/specs/2026-08-18-simple-cabinet-mockup.html` — статичный HTML-макет, тринадцать экранов. Каждый экран подписан в блоке `<div class="slot-name">`. Открывать как текст и читать разметку: она показывает состав, порядок и приоритет элементов. **Пиксельно копировать не надо** — переносить смысл, иерархию и тексты, используя компоненты и токены проекта.

## Global Constraints

- Репозиторий: `/Users/mihail/Desktop/Serv/bedolaga-cabinet`. Проверка типов `npx tsc --noEmit`, тесты `npm test` (vitest), линт `npm run lint`.
- Базовая линия тестов: **31 файл / 178 тестов, все проходят**. Новых падений быть не должно.
- **Запрещено** `git stash`, `git checkout`, `git restore`, `git reset`, `git clean`.
- **Запрещено** добавлять trailer `Co-Authored-By`. Commit-сообщения на русском: заголовок плюс тело.
- Комментарии на русском. Комментарий объясняет **почему**, а не пересказывает код.
- **Никаких новых цветов и хардкода.** Только Tailwind-классы проекта поверх токенов: `text-dark-50`, `text-dark-100`, `text-dark-400`, `text-dark-50/30`, `bg-dark-900/70`, `border-dark-700/40`, `text-accent-400`, `bg-accent-400/10`, `text-success-*`, `text-warning-*`, `text-error-*`. Приглушённый текст — через модификатор прозрачности (`text-dark-50/40`), как в существующем коде.
- **Никаких новых зависимостей.**
- Переиспользовать, а не переписывать: `Button` и `BentoCard` и `Switch` и `Sheet` и `Skeleton` из `src/components/primitives` и `src/components/ui`; анимации `staggerContainer`/`staggerItem` из `@/components/motion/transitions`; деньги через `formatPrice(kopeks)` из `src/utils/format.ts`; даты через `formatShortDate`.
- Все тексты — через `t('...')` с ключами во **всех четырёх** локалях: `ru.json`, `en.json`, `zh.json`, `fa.json`. Частичный перевод даёт сырые ключи на экране у части пользователей. Ключи класть в секцию `simple` верхнего уровня.
- Тесты компонентов — vitest с докблоком `// @vitest-environment jsdom` первой строкой файла, рендер через `@testing-library/react`, обёртка в `I18nextProvider` с `i18n` из `src/i18n` (образец — `src/components/PriceBreakdown.test.tsx`).
- Ничего не пушить, только локальные коммиты.

## Данные: что уже есть и что использовать

| Нужно | Откуда брать |
|---|---|
| Подписка | `subscriptionApi.getSubscription()`, ключ `['subscription']` |
| Список подписок и флаг мультирежима | `subscriptionApi.getSubscriptions()`, ключ `['subscriptions-list']` |
| Устройства | `subscriptionApi.getDevices()`, ключ `['devices']` |
| Ссылка подключения | `subscriptionApi.getConnectionLink()`, ключ `['connectionLink']`; резолв URL — `resolveConnectionUrlForUi` из `src/utils/connectionLink.ts` |
| Баланс | `balanceApi.getBalance()`, ключ `['balance']` |
| Способы оплаты | `balanceApi.getPaymentMethods()`, ключ `['payment-methods']` |
| Варианты продления | `subscriptionApi.getRenewalOptions()` |
| Варианты покупки, превью, покупка | `subscriptionApi.getPurchaseOptions()`, `previewPurchase()`, `submitPurchase()` |
| Рефералы | `referralApi.getReferralInfo()`, `getReferralList()`, `getReferralEarnings()`, `getWithdrawalBalance()` |
| Таймлайн | `subscriptionApi.getTimeline()`, готовый компонент `src/components/subscription/SubscriptionTimeline.tsx` |
| Режим интерфейса | `useUiMode()` из `@/hooks/useUiMode` |
| Цена места и возврат | `subscriptionApi.getDevicePrice()`, `getDeviceReductionInfo()` — в ответе последнего поле `refund_kopeks_per_slot` |

**Новое в API из волны 2, чего может не быть в типах фронта:**
- у элемента `devices[]` появилось поле `client: string | null` — имя программы
- `POST /cabinet/subscription/devices/delete-batch`, тело `{hwids: string[]}`, ответ `{success, deleted_count, failed_hwids, timed_out?}`
- `POST /cabinet/subscription/devices/reduce` принимает необязательное `hwids_to_remove: string[]`, в ответе `refund_kopeks`
- `GET /cabinet/balance/topup-presets` → `{presets: [{amount_kopeks, label_days}], sales_mode}`

Типы и клиентов для нового дописать в соответствующие файлы `src/api/`.

## Навигация

`SIMPLE_NAV_PATHS` из волны 1 фильтрует **меню**, а не маршрутизацию. Маршруты `/connection`, `/balance`, `/subscriptions/:id` остаются рабочими и достижимыми переходами из экранов. Меню их не показывает — это осознанно.

---

### Task 1: Каркас и общие части простых экранов

**Files:**
- Create: `src/components/simple/SimpleScreen.tsx`, `src/components/simple/SimpleRow.tsx`, `src/components/simple/SimpleStat.tsx`
- Create: `src/components/simple/SimpleScreen.test.tsx`
- Modify: `src/locales/ru.json`, `en.json`, `zh.json`, `fa.json` — добавить пустую секцию `simple` с общими ключами

**Interfaces:**
- Produces:
  - `<SimpleScreen title?: string; brand?: boolean; children>` — обёртка экрана: заголовок либо бренд-строка, вертикальный ритм через `staggerContainer`
  - `<SimpleRow title: string; subtitle?: string; value?: ReactNode; onClick?: () => void; chevron?: boolean; danger?: boolean>` — строка списка
  - `<SimpleStat label: string; value: ReactNode; sub?: string>` — плитка показателя

**Зачем отдельные примитивы:** во всех тринадцати экранах повторяются три формы — строка с заголовком, подписью и значением; плитка показателя; обёртка экрана с ритмом. Без них каждый экран обрастёт своей вёрсткой, и разъедутся отступы.

- [ ] **Step 1: Написать падающий тест**

Создать `src/components/simple/SimpleScreen.test.tsx`:

```tsx
// @vitest-environment jsdom
/**
 * Примитивы простого режима. Проверяем ровно то, ради чего они заведены:
 * единообразие структуры. Строка без обработчика не должна притворяться
 * кликабельной, а строка с обработчиком — обязана быть доступна с клавиатуры.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import i18n from '../../i18n';
import SimpleScreen from './SimpleScreen';
import SimpleRow from './SimpleRow';
import SimpleStat from './SimpleStat';

function r(ui: React.ReactElement) {
  return render(<I18nextProvider i18n={i18n}>{ui}</I18nextProvider>);
}

afterEach(() => cleanup());

describe('SimpleScreen', () => {
  it('рисует заголовок и содержимое', () => {
    r(<SimpleScreen title="Подписка"><p>внутри</p></SimpleScreen>);
    expect(screen.getByText('Подписка')).toBeTruthy();
    expect(screen.getByText('внутри')).toBeTruthy();
  });

  it('без заголовка не рисует пустой заголовок', () => {
    const { container } = r(<SimpleScreen><p>внутри</p></SimpleScreen>);
    expect(container.querySelector('h1')).toBeNull();
  });
});

describe('SimpleRow', () => {
  it('строка без обработчика не кликабельна', () => {
    r(<SimpleRow title="Баланс" value="340 ₽" />);
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('строка с обработчиком доступна как кнопка и срабатывает', () => {
    const onClick = vi.fn();
    r(<SimpleRow title="Баланс" onClick={onClick} />);
    const btn = screen.getByRole('button');
    fireEvent.click(btn);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('подпись и значение выводятся, когда переданы', () => {
    r(<SimpleRow title="Лимит" subtitle="Добавить место" value="5" />);
    expect(screen.getByText('Лимит')).toBeTruthy();
    expect(screen.getByText('Добавить место')).toBeTruthy();
    expect(screen.getByText('5')).toBeTruthy();
  });
});

describe('SimpleStat', () => {
  it('рисует подпись и значение', () => {
    r(<SimpleStat label="Пришли" value={7} />);
    expect(screen.getByText('Пришли')).toBeTruthy();
    expect(screen.getByText('7')).toBeTruthy();
  });
});
```

- [ ] **Step 2: Запустить тест и убедиться, что он падает**

Run: `cd /Users/mihail/Desktop/Serv/bedolaga-cabinet && npx vitest run src/components/simple/SimpleScreen.test.tsx`
Expected: FAIL, модули не найдены

- [ ] **Step 3: Написать три примитива**

`src/components/simple/SimpleScreen.tsx`:

```tsx
import type { ReactNode } from 'react';
import { motion } from 'framer-motion';
import { staggerContainer } from '@/components/motion/transitions';

interface SimpleScreenProps {
  /** Заголовок экрана. Не задан — заголовка не будет вовсе, а не пустая строка. */
  title?: string;
  /** Бренд-строка вместо заголовка: так устроена главная в макете. */
  brand?: string;
  children: ReactNode;
}

/**
 * Обёртка экрана простого режима: единый вертикальный ритм и одинаковое
 * появление блоков. Без неё каждый экран заводит свои отступы, и они разъезжаются.
 */
export default function SimpleScreen({ title, brand, children }: SimpleScreenProps) {
  return (
    <motion.div
      className="flex flex-col gap-4"
      variants={staggerContainer}
      initial="initial"
      animate="animate"
    >
      {brand && (
        <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-dark-50/40">
          {brand}
        </p>
      )}
      {title && <h1 className="text-2xl font-bold tracking-tight text-dark-50">{title}</h1>}
      {children}
    </motion.div>
  );
}
```

`src/components/simple/SimpleRow.tsx`:

```tsx
import type { ReactNode } from 'react';
import { ChevronRightIcon } from '@/components/icons';

interface SimpleRowProps {
  title: string;
  subtitle?: string;
  value?: ReactNode;
  /** Задан — строка становится кнопкой и доступна с клавиатуры. */
  onClick?: () => void;
  chevron?: boolean;
  danger?: boolean;
}

/**
 * Строка списка простого режима.
 *
 * Строка без обработчика остаётся обычным блоком: превращать её в кнопку
 * «на всякий случай» значит обещать нажатие, которого не будет, и ломать
 * навигацию с клавиатуры.
 */
export default function SimpleRow({
  title,
  subtitle,
  value,
  onClick,
  chevron,
  danger,
}: SimpleRowProps) {
  const content = (
    <>
      <div className="min-w-0 flex-1">
        <p className={`font-medium ${danger ? 'text-error-400' : 'text-dark-100'}`}>{title}</p>
        {subtitle && <p className="mt-0.5 text-sm text-dark-400">{subtitle}</p>}
      </div>
      {value !== undefined && (
        <span className="shrink-0 font-semibold tabular-nums text-dark-50">{value}</span>
      )}
      {chevron && <ChevronRightIcon className="size-4 shrink-0 text-dark-50/30" />}
    </>
  );

  const className = 'flex w-full items-center gap-3 py-3 text-left';

  if (!onClick) {
    return <div className={className}>{content}</div>;
  }

  return (
    <button type="button" onClick={onClick} className={className}>
      {content}
    </button>
  );
}
```

`src/components/simple/SimpleStat.tsx`:

```tsx
import type { ReactNode } from 'react';

interface SimpleStatProps {
  label: string;
  value: ReactNode;
  sub?: string;
}

/** Плитка показателя: подпись сверху мелким, значение крупным. */
export default function SimpleStat({ label, value, sub }: SimpleStatProps) {
  return (
    <div className="rounded-2xl border border-dark-700/40 bg-dark-900/70 p-3.5">
      <p className="text-[10px] font-semibold uppercase tracking-[0.1em] text-dark-50/40">
        {label}
      </p>
      <p className="mt-1 text-lg font-bold tabular-nums tracking-tight text-dark-50">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-dark-400">{sub}</p>}
    </div>
  );
}
```

Если имя иконки `ChevronRightIcon` в `@/components/icons` отличается — посмотреть фактический экспорт и использовать его, в `Dashboard.tsx` он уже импортируется.

- [ ] **Step 4: Запустить тест и убедиться, что он проходит**

Run: `npx vitest run src/components/simple/SimpleScreen.test.tsx`
Expected: PASS, 6 тестов

- [ ] **Step 5: Проверка типов и полный прогон**

Run: `npx tsc --noEmit && npm test`
Expected: типы чистые; базовая линия без новых падений

- [ ] **Step 6: Коммит**

```bash
git add src/components/simple/ && git commit -F - <<'EOF'
feat(simple): примитивы экранов простого режима

Во всех экранах повторяются три формы: строка с заголовком, подписью и
значением, плитка показателя и обёртка экрана с общим вертикальным ритмом.
Без общих примитивов каждый экран обрастает своей вёрсткой, и отступы
разъезжаются между разделами.

Строка без обработчика остаётся обычным блоком, а не кнопкой: превращать её в
кнопку «на всякий случай» значит обещать нажатие, которого не будет, и ломать
навигацию с клавиатуры.
EOF
```

---

### Task 2: Главная простого режима

**Files:**
- Create: `src/components/simple/SimpleDashboard.tsx`, `src/components/simple/SimpleDashboard.test.tsx`
- Modify: `src/pages/Dashboard.tsx` — переключение наверху компонента
- Modify: четыре файла локалей

**Interfaces:**
- Consumes: `SimpleScreen`, `SimpleRow`, `SimpleStat` из Task 1; `useUiMode`; `subscriptionApi.getSubscription`, `getDevices`, `getConnectionLink`; `balanceApi.getBalance`
- Produces: `<SimpleDashboard />`

**Визуальный эталон:** в макете фреймы «Главная», «Нет подписки · триал бесплатный», «Нет подписки · триал платный», «Готово».

**Состояния, все обязательны:**

1. **Подписка активна.** Зелёная точка и слово «Подключено», крупно «Осталось N дней», дата окончания, полоса срока. Главное действие — «Подключить устройство», ведёт на `/connection`, самый крупный элемент экрана. Под ним пара кнопок «Показать QR-код» (`/connection/qr`) и «Скопировать ссылку» (копирует `resolveConnectionUrlForUi`). Две плитки: Трафик и Устройства; плитка устройств кликабельна и ведёт на экран устройств. Строка «Баланс» ведёт на пополнение.
2. **Подписки нет, пробный период доступен и бесплатен.** Серая точка «Подписки нет», заголовок-призыв, кнопка «Попробовать N дней бесплатно», ниже «Выбрать тариф» (`/subscription/purchase`), состав пробного периода списком.
3. **Подписки нет, пробный период платный.** То же, но цена стоит **в самой кнопке**: «Попробовать N дней за X». Узнать о списании после нажатия — худший сценарий для новичка.
4. **Подписки нет, пробный период недоступен.** Кнопки пробного периода нет вовсе, «Выбрать тариф» становится главной, про пробный период на экране не упоминается.
5. **Безлимит устройств.** Когда `device_limit === 0`, плитка устройств показывает «∞», без полосы заполнения и без намёка на исчерпание.

Признаки доступности и цены пробного периода брать из ответа подписки и опций покупки — посмотреть, что уже использует существующий `TrialOfferCard.tsx`, и переиспользовать те же поля.

- [ ] **Step 1: Написать падающие тесты**

Создать `src/components/simple/SimpleDashboard.test.tsx`. Покрыть все пять состояний. Данные подавать через мок модулей API (`vi.mock('@/api/subscription', ...)`) либо через обёртку `QueryClientProvider` с предзаполненным кэшем — выбрать способ по образцу существующих тестов проекта. Обязательные проверки:

- при активной подписке видно «Подключить устройство» и число оставшихся дней;
- при отсутствии подписки и бесплатном пробном периоде кнопка не содержит цены;
- при платном пробном периоде цена присутствует в тексте кнопки;
- при недоступном пробном периоде кнопки пробного периода нет, а «Выбрать тариф» есть;
- при `device_limit === 0` в плитке устройств есть «∞» и нет полосы заполнения.

- [ ] **Step 2: Запустить тесты и убедиться, что они падают**

- [ ] **Step 3: Написать компонент**

Собрать по эталону из макета, используя примитивы Task 1, `Button` из примитивов проекта, `BentoCard` для карточек, `formatPrice` для сумм. Никаких новых цветов.

- [ ] **Step 4: Подключить в `Dashboard.tsx`**

В начале компонента, после получения хуков и до тяжёлых запросов:

```tsx
  const { isSimple } = useUiMode();
```

и сразу после объявления всех хуков (правила хуков нарушать нельзя — вызовы должны идти безусловно):

```tsx
  if (isSimple) {
    return <SimpleDashboard />;
  }
```

Важно: `return` ставить **после** всех вызовов хуков в компоненте, иначе при переключении режима порядок хуков изменится и React упадёт.

- [ ] **Step 5: Тесты, типы, полный прогон**

- [ ] **Step 6: Коммит**

---

### Task 3: Экран подписки — покупка и продление одним экраном

**Files:**
- Create: `src/components/simple/SimpleSubscription.tsx` и тест
- Modify: `src/pages/Subscriptions.tsx` (или страница, отвечающая маршруту `/subscriptions`) — переключение наверху
- Modify: локали

**Визуальный эталон:** фрейм «Подписка».

**Состав экрана, в этом порядке:**

1. Карточка текущей подписки: «Активна», дата окончания, тариф и параметры. Для новичка без подписки карточки нет, заголовок экрана меняется на покупку.
2. Выбор периода — карточки с ценой, выгодой и ценой за месяц, один предвыбран.
3. Устройства — степпер с подписью, сколько включено в тариф и сколько стоит следующее.
4. **Автопродление — тумблер сразу после устройств, до денег.** Под ним обязательна приписка: списываем только с баланса кабинета, карту и банк не трогаем, держите сумму на балансе к дате списания. Это отдельный механизм от провайдерского рекуррента, и человек, только что заплативший картой, иначе решит, что спишется снова с карты.
5. Способ оплаты — список из `balanceApi.getPaymentMethods()`, у каждого свой минимум.
6. Разбивка суммы: стоимость, «спишем с баланса», «пополнить» — только недостающая часть.
7. Кнопка, называющая происходящее: «Пополнить X и включить» либо «Оплатить X», если баланса хватает.
8. Строка «История подписки» — открывает экран истории.
9. Ссылка «Изменить трафик и страны» — уводит в расширенный режим.

**Оплата встроена в шаг покупки.** Уходить в баланс и возвращаться не нужно.

- [ ] **Step 1: Тесты на разбивку суммы и текст кнопки**

Обязательные проверки: при достаточном балансе кнопка говорит «Оплатить», строки «Пополнить» нет; при недостаточном — кнопка говорит про пополнение и сумма равна **разнице**, а не полной стоимости; приписка про списание с баланса присутствует рядом с тумблером автопродления.

- [ ] **Step 2-6: падение, реализация, подключение, проверки, коммит**

---

### Task 4: Устройства и лимит устройств

**Files:**
- Create: `src/components/simple/SimpleDevices.tsx`, `src/components/simple/SimpleDeviceLimit.tsx` и тесты
- Modify: `src/api/subscription.ts` — типы и клиенты для `delete-batch` и `hwids_to_remove`
- Modify: страница `/connection` или новый маршрут для экрана устройств — решить по факту и описать в отчёте
- Modify: локали

**Визуальный эталон:** фреймы «Устройства», «Устройства · выбрано несколько», «Лимит устройств».

**Экран устройств:**
- сверху занятость мест; при `device_limit === 0` — «∞», без полосы;
- **объяснение до списка, обязательно:** одно приложение — одно место; место занимает не телефон, а программа; поставили на один телефон Happ, INCY или другие — каждая займёт своё место;
- строка устройства: имя, затем клиент, платформа и дата последнего запроса подписки;
- имя не задано — показываем модель из панели, **никогда** заглушку «Без названия»;
- переименование прямо в строке;
- режим выбора с чекбоксами, «Выбрать все», кнопка с количеством и предупреждением, что доступ пропадёт сразу; отключение через `delete-batch`, неудачные hwid показать человеку;
- строка «Лимит устройств» ведёт на экран лимита.

**Экран лимита:**
- степпер; при безлимите экран недоступен;
- состав мест: даёт тариф, докуплено сверх, освобождаете;
- при уменьшении — расчёт возврата построчно: возврат за одно место, число мест, итого на баланс. Сумму брать из `refund_kopeks_per_slot`;
- **выбор отключаемых устройств чекбоксами** с датой последнего запроса у каждого;
- **количество отключаемых и количество освобождаемых мест — разные величины.** Мест освобождается «старый лимит минус новый», устройств отключать «сколько подключено минус новый лимит», и это бывает ноль. Считает экран, человек этого не делает;
- на пробной подписке лимит менять нельзя.

- [ ] **Step 1: Тесты**

Обязательно: расхождение величин (лимит 5, подключено 2, новый лимит 3 — освобождается 2 места, отключать нечего, выбор устройств не показывается); безлимит скрывает уменьшение и докупку; заглушки «Без названия» нет ни при каких данных.

- [ ] **Step 2-6**

---

### Task 5: Пополнение баланса

**Files:**
- Create: `src/components/simple/SimpleTopUp.tsx` и тест
- Modify: `src/api/balance.ts` — клиент `getTopupPresets()`
- Modify: локали

**Визуальный эталон:** фрейм «Баланс · пополнение».

- сверху сколько есть и сколько станет;
- крупное поле своей суммы;
- готовые суммы из `GET /cabinet/balance/topup-presets` с подписью периода;
- способы оплаты с указанием минимума у каждого; **способ, который не примет введённую сумму, гаснет сразу**, а не после перехода к провайдеру;
- кнопка называет сумму.

- [ ] **Step 1: Тесты** — минимум гасит способ; готовые суммы подставляются в поле; пустой список готовых сумм не ломает экран.
- [ ] **Step 2-6**

---

### Task 6: Рефералы

**Files:**
- Create: `src/components/simple/SimpleReferral.tsx` и тест
- Modify: `src/pages/Referral.tsx` — переключение наверху
- Modify: локали

**Визуальный эталон:** фрейм «Рефералы».

- две ссылки, **кабинетная сверху**: «Регистрация через почту и другие сервисы» и «Регистрация через Телеграм», у каждой кнопка копирования;
- «Отправить другу» через `navigator.share` с запасным вариантом на share Telegram, и «QR-код»;
- три показателя: пришли, заработано, доступно; строка про процент комиссии;
- списки приглашённых и начислений **слиты в один**;
- блок вывода показывает прогресс к порогу: минимум 1 000 ₽, и если не хватает — насколько. Сейчас человек узнаёт о пороге, только упёршись в отказ;
- **анкета партнёра в простом режиме не показывается** — для вывода она не нужна.

- [ ] **Step 1: Тесты** — при сумме ниже порога кнопка вывода неактивна и виден остаток до порога; при достаточной — активна.
- [ ] **Step 2-6**

---

### Task 7: История подписки

**Files:**
- Create: `src/components/simple/SimpleHistory.tsx` и тест
- Modify: локали

**Визуальный эталон:** фрейм «История подписки».

Компонент `src/components/subscription/SubscriptionTimeline.tsx` **уже существует** и принимает `{events, since, isDark}`. Данные — `subscriptionApi.getTimeline()`. Задача сводится к экрану-обёртке со сводкой сверху: с какой даты клиент, всего оплачено, дней с подпиской.

Показывать перерывы (`downtime_seconds`) и перенесённый остаток (`carried_seconds`) — эндпоинт их уже считает, и именно они объясняют дыры в датах, из-за которых приходят вопросы «куда делись мои дни». Если существующий компонент их уже рисует — не дублировать.

- [ ] **Step 1-6**

---

### Task 8: Профиль простого режима

**Files:**
- Create: `src/components/simple/SimpleProfile.tsx` и тест
- Modify: `src/pages/Profile.tsx` — переключение наверху, **сохранив** уже работающий тумблер режима
- Modify: локали

**Визуальный эталон:** фрейм «Профиль».

- карточка пользователя;
- способы входа списком с состоянием привязки, переход к управлению — на `/profile/accounts`;
- настройки: тумблер «Простой интерфейс», **один** тумблер уведомлений вместо пяти с числовыми порогами, язык;
- **админ-панель — только администраторам, и она обязана остаться доступной в простом режиме**;
- поддержка, выход.

**Инвариант:** тумблер режима не должен зависеть от загрузки чего-либо ещё. В волне 1 он уже пострадал от этого — попал внутрь ветки, зависящей от настроек уведомлений, и пропадал вместе с ними, запирая человека в простом режиме. Не повторять.

- [ ] **Step 1: Тест** — тумблер режима рендерится, даже когда запрос настроек уведомлений упал.
- [ ] **Step 2-6**

---

## Проверка после всей волны

```bash
cd /Users/mihail/Desktop/Serv/bedolaga-cabinet
npx tsc --noEmit && npm run lint && npm test
```

Ручная проверка на стенде: включить простой режим в Профиле и пройти путь целиком — главная, подключение, покупка при нехватке баланса, устройства, уменьшение лимита с возвратом, рефералы, история, возврат в полный режим.
