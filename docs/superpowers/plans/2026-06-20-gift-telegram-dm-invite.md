# Gift Telegram DM Invite — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a gift purchase is paid and the recipient's Telegram username resolves to an already-registered bot user, send them a DM with Accept/Decline buttons; the gift applies only after they press Accept.

**Architecture:** Two new code units: (1) a read-only `_resolve_existing_telegram_user` helper in the existing service that locates — but never creates — a registered user; (2) a new handler file `app/handlers/gift_claim_telegram.py` with `handle_gift_accept` / `handle_gift_decline` callbacks. A single branch is added inside `notify_gift_claim_available` to send the invite DM. The existing email path is untouched.

**Tech Stack:** Python 3.12, Aiogram 3, SQLAlchemy async (AsyncSession), structlog, asyncio.wait_for.

## Global Constraints

- After every `.py` edit: `python3 -m py_compile <file>` must exit 0.
- Import-test: `python3 -c "from app.handlers.gift_claim_telegram import register_handlers"` must exit 0.
- No `Co-Authored-By:` trailer in commit messages.
- Never create a new user or commit inside `_resolve_existing_telegram_user`.
- Never break the existing email notification logic in `notify_gift_claim_available`.
- Match surrounding code style: inline lazy imports for cabinet/bot services, structlog, HTML escaping via `html.escape`.
- `fulfill_purchase` is called with `pre_resolved_telegram_id=callback.from_user.id` so it finds the existing user without creating a new one.
- The `buyer` relationship on `GuestPurchase` is `lazy='selectin'` so it is always loaded when a purchase is loaded with selectin options.

---

### Task 1: `_resolve_existing_telegram_user` helper

**Files:**
- Modify: `app/services/guest_purchase_service.py` — insert after line 849 (after `_find_or_create_user` returns, before `_get_recipient_contact`)

**Interfaces:**
- Produces: `async def _resolve_existing_telegram_user(db: AsyncSession, username: str) -> User | None`

- [ ] **Step 1: Read the file around the insertion point to get exact surrounding context**

  ```bash
  # Already read during planning — insertion is after the closing `return user, False` of
  # _find_or_create_user (line 849) and before `_get_recipient_contact` (line 852).
  # We need to insert between lines 849 and 852.
  ```

- [ ] **Step 2: Insert the helper function**

  Open `app/services/guest_purchase_service.py`. After line 849 (`    return user, False`) and before line 852 (`def _get_recipient_contact`), insert:

  ```python
  
  
  async def _resolve_existing_telegram_user(db: AsyncSession, username: str) -> 'User | None':
      """Read-only lookup: return an already-registered User for a Telegram username, or None.
  
      Does NOT create users, does NOT commit. Safe to call from notification paths.
      Resolution order mirrors _find_or_create_user:
        1. bot.get_chat('@username')  → telegram_id  (timeout 5 s, best-effort)
        2. SELECT by telegram_id
        3. SELECT by username (case-insensitive)
      Returns None if the resolved user has no telegram_id (cannot DM them).
      """
      username = username.lstrip('@')
      if not _TELEGRAM_USERNAME_RE.match(username):
          return None
      normalized = username.lower()
  
      resolved_telegram_id: int | None = None
      try:
          from app.bot_factory import create_bot
  
          async with create_bot() as bot:
              chat = await asyncio.wait_for(
                  bot.get_chat(chat_id=f'@{username}'),
                  timeout=5.0,
              )
              resolved_telegram_id = chat.id
      except Exception as exc:
          logger.debug(
              'Could not resolve telegram_id for gift recipient (read-only lookup)',
              username=username,
              error=str(exc),
          )
  
      user: User | None = None
      if resolved_telegram_id:
          result = await db.execute(select(User).where(User.telegram_id == resolved_telegram_id))
          user = result.scalars().first()
  
      if not user:
          result = await db.execute(select(User).where(func.lower(User.username) == normalized))
          user = result.scalars().first()
  
      # We can only DM the user if we have their telegram_id
      if user and not user.telegram_id:
          return None
      return user
  ```

- [ ] **Step 3: Verify syntax**

  ```bash
  python3 -m py_compile app/services/guest_purchase_service.py && echo OK
  ```
  Expected output: `OK`

- [ ] **Step 4: Commit**

  ```bash
  git add app/services/guest_purchase_service.py
  git commit -m "feat(gift): _resolve_existing_telegram_user — read-only lookup helper"
  ```

---

### Task 2: DM invite branch in `notify_gift_claim_available`

**Files:**
- Modify: `app/services/guest_purchase_service.py` — insert a `telegram` branch inside `notify_gift_claim_available`, between lines 1112 (end of the `if purchase.is_gift` guard) and 1113 (the existing `if purchase.gift_recipient_type == 'email'` block).

**Interfaces:**
- Consumes: `_resolve_existing_telegram_user(db, username) -> User | None`  (Task 1)
- Consumes: `notify_gift_claim_available(purchase, *, tariff_name, period_days, language)` — existing signature, unchanged.

- [ ] **Step 1: Read the exact lines to be modified**

  Re-read `app/services/guest_purchase_service.py` lines 1082–1165 to confirm current line numbers haven't shifted after Task 1's insert.

- [ ] **Step 2: Insert the `telegram` branch**

  Inside `notify_gift_claim_available`, after the `claim_url` assignment and before the `if purchase.gift_recipient_type == 'email'` block, add:

  ```python
      # Recipient via Telegram: send a DM invite if the user is already registered.
      # Anti-spoof: we require an existing bot user — spoofed usernames simply get nothing.
      if purchase.gift_recipient_type == 'telegram' and purchase.gift_recipient_value:
          try:
              import html as html_mod

              from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

              from app.bot_factory import create_bot
              from app.database.database import AsyncSessionLocal

              async with AsyncSessionLocal() as db:
                  recipient = await _resolve_existing_telegram_user(db, purchase.gift_recipient_value)

              if recipient and recipient.telegram_id:
                  # Build "from" line — prefer buyer's display name or contact
                  gift_from = ''
                  if purchase.buyer and purchase.buyer.username:
                      safe_from = html_mod.escape(f'@{purchase.buyer.username}')
                      gift_from = f'\nОт: {safe_from}'
                  elif purchase.contact_value:
                      safe_from = html_mod.escape(purchase.contact_value)
                      gift_from = f'\nОт: {safe_from}'

                  gift_msg = ''
                  if purchase.gift_message:
                      safe_msg = html_mod.escape(purchase.gift_message)
                      gift_msg = f'\n\n"{safe_msg}"'

                  _tariff_name = tariff_name or ''
                  safe_tariff = html_mod.escape(_tariff_name) if _tariff_name else ''
                  _period = period_days if period_days is not None else purchase.period_days
                  period_text = f'{_period} дн.' if _period else ''
                  tariff_text = f'{safe_tariff} — {period_text}' if safe_tariff else period_text

                  text = (
                      f'🎁 <b>Вам подарили VPN подписку!</b>\n{tariff_text}'
                      f'{gift_from}{gift_msg}'
                      f'\n\nНажмите «✅ Принять», чтобы активировать подарок,'
                      f' или «❌ Отклонить», если он вам не нужен.'
                  )
                  keyboard = InlineKeyboardMarkup(
                      inline_keyboard=[
                          [
                              InlineKeyboardButton(
                                  text='✅ Принять',
                                  callback_data=f'gift_accept:{purchase.id}',
                              ),
                              InlineKeyboardButton(
                                  text='❌ Отклонить',
                                  callback_data=f'gift_decline:{purchase.id}',
                              ),
                          ]
                      ]
                  )
                  async with create_bot() as bot:
                      await bot.send_message(
                          chat_id=recipient.telegram_id,
                          text=text,
                          reply_markup=keyboard,
                          parse_mode='HTML',
                      )
                  logger.info(
                      'Gift DM invite sent to Telegram recipient',
                      purchase_id=purchase.id,
                      recipient_telegram_id=recipient.telegram_id,
                  )
          except Exception:
              logger.warning(
                  'Failed to send gift DM invite to Telegram recipient',
                  purchase_id=purchase.id,
                  exc_info=True,
              )

  ```

  Note: `AsyncSessionLocal` is imported inline here (same pattern as `email_service` import at line 1107). Confirm that `from app.database.database import AsyncSessionLocal` is not already a top-level import (it isn't — the service uses it inline in a few places).

- [ ] **Step 3: Verify syntax**

  ```bash
  python3 -m py_compile app/services/guest_purchase_service.py && echo OK
  ```
  Expected: `OK`

- [ ] **Step 4: Commit**

  ```bash
  git add app/services/guest_purchase_service.py
  git commit -m "feat(gift): notify_gift_claim_available — DM invite branch for telegram recipients"
  ```

---

### Task 3: `gift_claim_telegram.py` — Accept/Decline handlers

**Files:**
- Create: `app/handlers/gift_claim_telegram.py`

**Interfaces:**
- Consumes: `fulfill_purchase(db, purchase_token, pre_resolved_telegram_id=int) -> GuestPurchase | None` from `app.services.guest_purchase_service`
- Consumes: `_resolve_existing_telegram_user(db, username) -> User | None` from `app.services.guest_purchase_service`
- Consumes: `GuestPurchase`, `GuestPurchaseStatus` from `app.database.models`
- Consumes: `AsyncSessionLocal` from `app.database.database`
- Produces: `register_handlers(dp: Dispatcher) -> None`

- [ ] **Step 1: Create the file**

  Create `/Users/mihail/Desktop/Serv/remnawave-bedolaga-telegram-bot/app/handlers/gift_claim_telegram.py` with this content:

  ```python
  """Handlers for gift subscription Accept / Decline callbacks (Telegram DM invite flow)."""

  import html as html_mod

  import structlog
  from aiogram import Dispatcher, F, types
  from aiogram.types import InaccessibleMessage
  from sqlalchemy import select
  from sqlalchemy.orm import selectinload

  from app.database.database import AsyncSessionLocal
  from app.database.models import GuestPurchase, GuestPurchaseStatus


  logger = structlog.get_logger(__name__)

  _GIFT_NOT_FOUND = 'Подарок не найден или недоступен.'
  _ALREADY_ACTIVATED = '✅ Подарок уже активирован.'
  _NOT_FOR_YOU = 'Это приглашение предназначено не для вас.'
  _SELF_ACCEPT = 'Нельзя принять собственный подарок.'


  async def handle_gift_accept(callback: types.CallbackQuery) -> None:
      """Handle gift_accept:{purchase_id} — verify identity then fulfill the purchase."""
      if isinstance(callback.message, InaccessibleMessage):
          await callback.answer('Сообщение устарело. Попробуйте /start.', show_alert=True)
          return

      if not callback.data:
          return

      parts = callback.data.split(':', 1)
      if len(parts) != 2:
          await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
          return

      try:
          purchase_id = int(parts[1])
      except ValueError:
          await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
          return

      # Lazy imports to avoid circular imports (same pattern as rest of codebase)
      from app.services.guest_purchase_service import (
          GuestPurchaseError,
          _resolve_existing_telegram_user,
          fulfill_purchase,
      )

      async with AsyncSessionLocal() as db:
          result = await db.execute(
              select(GuestPurchase)
              .options(selectinload(GuestPurchase.user), selectinload(GuestPurchase.tariff))
              .where(GuestPurchase.id == purchase_id)
          )
          purchase = result.scalars().first()

          if not purchase or not purchase.is_gift:
              await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
              return

          # Idempotency: already delivered
          if purchase.status == GuestPurchaseStatus.DELIVERED.value:
              await callback.answer(_ALREADY_ACTIVATED, show_alert=True)
              if not isinstance(callback.message, InaccessibleMessage):
                  await callback.message.edit_text('✅ Подарок уже активирован.', parse_mode=None)
              return

          if purchase.status != GuestPurchaseStatus.PAID.value:
              await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
              return

          # Anti-self: buyer cannot accept their own gift
          if purchase.buyer_user_id is not None:
              from app.database.models import User

              buyer_result = await db.execute(
                  select(User).where(User.id == purchase.buyer_user_id)
              )
              buyer = buyer_result.scalars().first()
              if buyer and buyer.telegram_id == callback.from_user.id:
                  await callback.answer(_SELF_ACCEPT, show_alert=True)
                  return

          # Defense-in-depth: re-resolve recipient and verify it matches callback sender
          if not purchase.gift_recipient_value:
              await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
              return

          resolved = await _resolve_existing_telegram_user(db, purchase.gift_recipient_value)
          if not resolved or resolved.telegram_id != callback.from_user.id:
              await callback.answer(_NOT_FOR_YOU, show_alert=True)
              return

          # All checks passed — answer quickly, edit message, then fulfill
          await callback.answer()
          if not isinstance(callback.message, InaccessibleMessage):
              await callback.message.edit_text('⏳ Активируем подарок...', parse_mode=None)

          tariff_name = ''
          if purchase.tariff and purchase.tariff.name:
              tariff_name = html_mod.escape(purchase.tariff.name)
          period_days = purchase.period_days

          try:
              await fulfill_purchase(db, purchase.token, pre_resolved_telegram_id=callback.from_user.id)
          except GuestPurchaseError as exc:
              logger.warning(
                  'Gift accept via DM callback failed',
                  purchase_id=purchase_id,
                  telegram_id=callback.from_user.id,
                  error=exc.message,
              )
              if not isinstance(callback.message, InaccessibleMessage):
                  if exc.status_code >= 500:
                      await callback.message.edit_text(
                          'Произошла ошибка при активации. Попробуйте позже.', parse_mode=None
                      )
                  else:
                      await callback.message.edit_text(
                          f'Не удалось активировать подарок: {html_mod.escape(exc.message)}',
                          parse_mode=None,
                      )
              return
          except Exception:
              logger.exception(
                  'Unexpected error during gift DM accept',
                  purchase_id=purchase_id,
                  telegram_id=callback.from_user.id,
              )
              if not isinstance(callback.message, InaccessibleMessage):
                  await callback.message.edit_text(
                      'Произошла ошибка при активации. Попробуйте позже.', parse_mode=None
                  )
              return

      period_text = f'{period_days} дн.' if period_days else ''
      tariff_text = f'{tariff_name} — {period_text}' if tariff_name else period_text

      if not isinstance(callback.message, InaccessibleMessage):
          await callback.message.edit_text(
              f'✅ <b>Подарок активирован!</b>\n{tariff_text}\n\nВаша подписка обновлена.',
          )


  async def handle_gift_decline(callback: types.CallbackQuery) -> None:
      """Handle gift_decline:{purchase_id} — leave purchase as PAID, remove buttons."""
      if isinstance(callback.message, InaccessibleMessage):
          await callback.answer('Сообщение устарело.', show_alert=True)
          return

      if not callback.data:
          return

      parts = callback.data.split(':', 1)
      if len(parts) != 2:
          await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
          return

      try:
          purchase_id = int(parts[1])
      except ValueError:
          await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
          return

      # Verify the purchase exists (no need for DB write — status stays PAID)
      async with AsyncSessionLocal() as db:
          result = await db.execute(
              select(GuestPurchase).where(GuestPurchase.id == purchase_id)
          )
          purchase = result.scalars().first()

          if not purchase or not purchase.is_gift:
              await callback.answer(_GIFT_NOT_FOUND, show_alert=True)
              return

      await callback.answer()
      if not isinstance(callback.message, InaccessibleMessage):
          await callback.message.edit_text(
              'Вы отклонили подарок. Ссылку на активацию можно запросить у отправителя.',
              parse_mode=None,
          )

      logger.info(
          'Gift DM invite declined',
          purchase_id=purchase_id,
          telegram_id=callback.from_user.id,
      )


  def register_handlers(dp: Dispatcher) -> None:
      dp.callback_query.register(handle_gift_accept, F.data.startswith('gift_accept:'))
      dp.callback_query.register(handle_gift_decline, F.data.startswith('gift_decline:'))
  ```

- [ ] **Step 2: Verify syntax**

  ```bash
  python3 -m py_compile app/handlers/gift_claim_telegram.py && echo OK
  ```
  Expected: `OK`

- [ ] **Step 3: Commit**

  ```bash
  git add app/handlers/gift_claim_telegram.py
  git commit -m "feat(gift): gift_claim_telegram — Accept/Decline DM callback handlers"
  ```

---

### Task 4: Wire handlers into `app/bot.py`

**Files:**
- Modify: `app/bot.py` — add import at line ~70 and registration call at line ~244

**Interfaces:**
- Consumes: `register_handlers` from `app.handlers.gift_claim_telegram`

- [ ] **Step 1: Add import**

  In `app/bot.py`, after line 70 (`from app.handlers.gift_activation import register_handlers as register_gift_activation_handlers`), add:

  ```python
  from app.handlers.gift_claim_telegram import register_handlers as register_gift_claim_telegram_handlers
  ```

- [ ] **Step 2: Add registration call**

  In `app/bot.py`, after line 244 (`register_gift_activation_handlers(dp)`), add:

  ```python
      register_gift_claim_telegram_handlers(dp)
  ```

- [ ] **Step 3: Verify syntax**

  ```bash
  python3 -m py_compile app/bot.py && echo OK
  ```
  Expected: `OK`

- [ ] **Step 4: Import-test**

  ```bash
  python3 -c "from app.handlers.gift_claim_telegram import register_handlers; print('OK')"
  ```
  Expected: `OK`

- [ ] **Step 5: Commit**

  ```bash
  git add app/bot.py
  git commit -m "feat(gift): wire gift_claim_telegram handlers into bot dispatcher"
  ```

---

## Self-Review

### Spec coverage

| Spec requirement | Task |
|---|---|
| `_resolve_existing_telegram_user` — read-only, no create | Task 1 |
| DM invite sent only to registered users | Task 2 |
| Invite text: from, tariff, period, gift_message | Task 2 |
| Keyboard: ✅ Принять / ❌ Отклонить with purchase_id | Task 2 |
| try/except around DM send — never breaks payment | Task 2 |
| `handle_gift_accept` parses purchase_id | Task 3 |
| Idempotency: DELIVERED → answer "already activated" | Task 3 |
| Anti-self: buyer cannot accept | Task 3 |
| Defense-in-depth: re-resolve recipient, compare telegram_id | Task 3 |
| `fulfill_purchase` called with `pre_resolved_telegram_id` | Task 3 |
| Edit message to activated / remove buttons | Task 3 |
| `handle_gift_decline` — keep PAID, edit message | Task 3 |
| `register_handlers` in new file | Task 3 |
| Wire into `app/bot.py` | Task 4 |
| Email path untouched | Task 2 (email block not modified) |
| py_compile after every .py change | All tasks |
| Import-test | Task 4 |

### Placeholder scan

No TBD / TODO / placeholder patterns found.

### Type consistency

- `_resolve_existing_telegram_user(db: AsyncSession, username: str) -> User | None` — defined Task 1, consumed Task 2 (notify) and Task 3 (accept handler). Signatures match.
- `fulfill_purchase(db, purchase.token, pre_resolved_telegram_id=callback.from_user.id)` — signature confirmed from `guest_purchase_service.py:290`. Matches.
- `GuestPurchaseStatus.PAID.value` / `GuestPurchaseStatus.DELIVERED.value` — both confirmed from `models.py:4089-4092`.
- `purchase.buyer` relationship is `lazy='selectin'` (models.py:4149) — loads automatically with purchase, no extra join needed; but in Task 3 we load GuestPurchase with `selectinload(GuestPurchase.user)` and `selectinload(GuestPurchase.tariff)`. The `buyer` relationship also loads via selectin (it's defined that way on the model). No lazy-load issue.
- `purchase.id` used as `int` for callback_data — confirmed Column(Integer).
