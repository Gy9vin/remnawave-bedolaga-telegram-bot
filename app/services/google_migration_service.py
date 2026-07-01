"""One-off migration: email Google-linked users a personal, long-lived
set-password link before Google login is disabled (RF law, 2026-07-07)."""

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import structlog

from app.cabinet.auth.email_verification import (
    generate_password_reset_token,
    get_google_migration_token_expires_at,
)
from app.cabinet.services.email_service import email_service
from app.config import settings
from app.database.crud.user import get_google_linked_users
from app.database.database import AsyncSessionLocal

logger = structlog.get_logger(__name__)

EMAIL_RATE_LIMIT = 8  # emails per second (matches EmailBroadcastService)
_SEND_INTERVAL = 1.0 / EMAIL_RATE_LIMIT

DEFAULT_SUBJECT = '⚠️ Важно: вход через Google скоро отключится — задайте пароль'

_DEFAULT_HTML = """\
<div style="font-family:Arial,sans-serif;font-size:15px;line-height:1.6;color:#222;max-width:560px;margin:0 auto">
  <p>⚠️ <b>Друзья, важное — не пропустите!</b></p>
  <p>С 7 июля вход через <b>Google ID</b> и <b>Apple ID</b> больше не будет работать — такие теперь ограничения 😔</p>
  <p>Чтобы не остаться без доступа к своему кабинету — привяжите другой способ входа уже сейчас 👇</p>
  <p>✅ Telegram&nbsp;&nbsp;✅ Яндекс ID&nbsp;&nbsp;✅ Обычная почта</p>
  <p>🔐 А можно просто задать пароль для своей почты — и спокойно входить по логину и паролю.</p>
  <p style="text-align:center;margin:28px 0 12px">
    <a href="{{set_password_url}}" style="background:#4f46e5;color:#fff;text-decoration:none;padding:14px 28px;border-radius:10px;font-weight:bold;display:inline-block">🔐 Задать пароль</a>
  </p>
  <p style="text-align:center;color:#555;margin:0 0 20px">🔑 Ваш логин для входа — этот email: <b>{{login_email}}</b></p>
  <p>👉 Также всё можно сделать в личном кабинете → раздел «Профиль». Займёт меньше минуты!</p>
  <p>❗️ Если вы входите только через Google или Apple — не тяните! Без привязки после 7 июля войти не получится 🙏</p>
  <p>Позаботьтесь о доступе заранее — и всё будет работать без сбоев! 💪</p>
  <p>— EasyTunel ❤️</p>
</div>"""


def build_invite_email(set_password_url: str, login_email: str) -> tuple[str, str]:
    """Render (subject, html) for the set-password invite.

    login_email is shown in the body so the user knows exactly what to type as
    their login (it's their Google account email, already stored on the account).
    """
    html = _DEFAULT_HTML.replace('{{set_password_url}}', set_password_url).replace('{{login_email}}', login_email or '')
    return DEFAULT_SUBJECT, html


@dataclass
class _Status:
    running: bool = False
    total: int = 0
    sent: int = 0
    failed: int = 0
    started_at: str | None = None
    finished_at: str | None = None


class GoogleMigrationService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._status = _Status()
        self._task: asyncio.Task | None = None

    async def start(self) -> bool:
        """Kick off the background send. Returns False if already running."""
        async with self._lock:
            if self._status.running:
                return False
            self._status = _Status(running=True, started_at=datetime.now(UTC).isoformat())
            self._task = asyncio.create_task(self._run(), name='google-migration')
            return True

    def get_status(self) -> dict:
        return asdict(self._status)

    async def send_test_to_email(self, email: str) -> dict:
        """Send ONE invite to a single user by email (pre-campaign test).

        Reuses the exact per-user logic (long-lived token, email_verified=True,
        invite email). Works for any existing user with that email — so an admin
        can test on their own account. Skips non-ACTIVE accounts (deleted/blocked
        can't complete email login anyway). Returns {'found', 'sent', 'status'}.
        """
        from sqlalchemy import func, select

        from app.database.models import User, UserStatus

        normalized = (email or '').strip().lower()
        if not normalized:
            return {'found': False, 'sent': False, 'status': None}

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User).where(func.lower(User.email) == normalized))
            user = result.scalar_one_or_none()
            if user is None:
                return {'found': False, 'sent': False, 'status': None}
            if user.status != UserStatus.ACTIVE.value:
                logger.info('Google-migration test skipped: account not active', email=normalized, status=user.status)
                return {'found': True, 'sent': False, 'status': user.status}
            user_id = user.id

        sent = await self._process_user(user_id)
        logger.info('Google-migration test invite sent', email=normalized, sent=sent)
        return {'found': True, 'sent': bool(sent), 'status': 'active'}

    async def _run(self) -> None:
        try:
            async with AsyncSessionLocal() as session:
                users = await get_google_linked_users(session)
                user_ids = [u.id for u in users]
            self._status.total = len(user_ids)
            for i, user_id in enumerate(user_ids):
                ok = await self._process_user(user_id)
                if ok:
                    self._status.sent += 1
                else:
                    self._status.failed += 1
                if i < len(user_ids) - 1:
                    await asyncio.sleep(_SEND_INTERVAL)
        except Exception as exc:  # never leave status stuck as running
            logger.exception('Google migration run crashed', exc=exc)
        finally:
            self._status.running = False
            self._status.finished_at = datetime.now(UTC).isoformat()

    async def _process_user(self, user_id: int) -> bool:
        from app.database.models import User

        try:
            async with AsyncSessionLocal() as session:
                user = await session.get(User, user_id)
                if user is None or not user.email:
                    return False
                token = generate_password_reset_token()
                user.password_reset_token = token
                user.password_reset_expires = get_google_migration_token_expires_at()
                user.email_verified = True
                user.email_verified_at = datetime.now(UTC)
                await session.commit()
                email = user.email
            set_password_url = f'{settings.CABINET_URL}/reset-password?token={token}'
            subject, html = build_invite_email(set_password_url, email)
            return bool(await asyncio.to_thread(email_service.send_email, email, subject, html))
        except Exception as exc:
            logger.warning('Failed to send Google-migration invite', user_id=user_id, exc=exc)
            return False


google_migration_service = GoogleMigrationService()
