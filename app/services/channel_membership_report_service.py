"""Service: отчёт о членстве активных подписчиков в обязательном канале.

Идея: берём всех активных telegram-подписчиков, для каждого вызываем
`bot.get_chat_member(channel_id, user_id)` с троттлингом ~20 rps и строим
список тех, кого в канале нет. Попутно обновляем таблицу
UserChannelSubscription и Redis-кеш.
"""

from __future__ import annotations

import asyncio
import csv
import io
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

from app.database.crud.channel_report import get_active_telegram_subscribers_for_report
from app.database.crud.required_channel import get_channel_by_id, upsert_user_channel_sub
from app.database.database import AsyncSessionLocal
from app.services.channel_subscription_service import channel_subscription_service
from app.utils.cache import ChannelSubCache


logger = structlog.get_logger(__name__)


# TTL готового отчёта в памяти
_REPORT_TTL = timedelta(hours=2)

# Батч апдейтов UserChannelSubscription перед commit
_DB_BATCH = 100

# Rate limit: ~20 запросов/сек к Telegram API (тот же лимит, что в channel_subscription_service)
_API_DELAY_SEC = 0.05
_API_CONCURRENCY = 20

# Как часто пересчитывать прогресс (раз в N обработанных)
_PROGRESS_TICK = 25

_GOOD_STATUSES = (
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
)


@dataclass
class ReportProgress:
    report_id: str
    status: str  # pending | running | completed | failed | cancelled
    channel_db_id: int
    channel_id: str
    channel_title: str | None
    total: int = 0
    processed: int = 0
    in_channel: int = 0
    not_in_channel: int = 0
    errors: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            'report_id': self.report_id,
            'status': self.status,
            'channel_db_id': self.channel_db_id,
            'channel_id': self.channel_id,
            'channel_title': self.channel_title,
            'total': self.total,
            'processed': self.processed,
            'in_channel': self.in_channel,
            'not_in_channel': self.not_in_channel,
            'errors': self.errors,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
            'error_message': self.error_message,
            'has_csv': self.status == 'completed' and self.not_in_channel > 0,
        }


@dataclass
class _ReportEntry:
    progress: ReportProgress
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    csv_bytes: bytes | None = None
    filename: str | None = None
    non_members: list[dict[str, Any]] = field(default_factory=list)


class ReportAlreadyRunning(Exception):
    """Попытка запустить новый отчёт, когда уже есть активный."""


class ReportNotFound(Exception):
    """Отчёт с таким id не найден или уже протух."""


class ChannelMembershipReportService:
    """Управляет фоновыми отчётами по членству в обязательных каналах."""

    def __init__(self) -> None:
        self._reports: dict[str, _ReportEntry] = {}
        self._lock = asyncio.Lock()

    # -- Public API ---------------------------------------------------------------

    async def start_report(
        self,
        channel_db_id: int,
        admin_telegram_id: int | None = None,
    ) -> str:
        """Запускает отчёт в фоне. Возвращает report_id.

        Если уже есть активный отчёт — бросает ReportAlreadyRunning.
        """
        async with self._lock:
            self._gc_expired_locked()
            for entry in self._reports.values():
                if entry.progress.status in ('pending', 'running'):
                    raise ReportAlreadyRunning(
                        f'Уже выполняется отчёт {entry.progress.report_id} по каналу #{entry.progress.channel_db_id}'
                    )

            async with AsyncSessionLocal() as db:
                channel = await get_channel_by_id(db, channel_db_id)
                if channel is None:
                    raise ValueError(f'Канал #{channel_db_id} не найден')

            report_id = uuid.uuid4().hex
            entry = _ReportEntry(
                progress=ReportProgress(
                    report_id=report_id,
                    status='pending',
                    channel_db_id=channel.id,
                    channel_id=channel.channel_id,
                    channel_title=channel.title,
                    started_at=datetime.now(UTC),
                ),
            )
            self._reports[report_id] = entry

            entry.task = asyncio.create_task(
                self._run_report(report_id, channel.channel_id, admin_telegram_id),
                name=f'channel-report-{report_id}',
            )
            return report_id

    def get_status(self, report_id: str) -> dict[str, Any]:
        entry = self._reports.get(report_id)
        if entry is None:
            raise ReportNotFound(report_id)
        return entry.progress.to_dict()

    def get_csv(self, report_id: str) -> tuple[bytes, str]:
        entry = self._reports.get(report_id)
        if entry is None:
            raise ReportNotFound(report_id)
        if entry.progress.status != 'completed' or entry.csv_bytes is None or entry.filename is None:
            raise ValueError('CSV ещё не готов')
        return entry.csv_bytes, entry.filename

    async def cancel(self, report_id: str) -> bool:
        entry = self._reports.get(report_id)
        if entry is None:
            raise ReportNotFound(report_id)
        if entry.progress.status in ('completed', 'failed', 'cancelled'):
            return False
        entry.cancel_event.set()
        return True

    # -- Internals ---------------------------------------------------------------

    def _gc_expired_locked(self) -> None:
        """Удаляет завершённые отчёты старше _REPORT_TTL. Вызывать под self._lock."""
        now = datetime.now(UTC)
        stale = [
            rid
            for rid, entry in self._reports.items()
            if entry.progress.finished_at is not None and (now - entry.progress.finished_at) > _REPORT_TTL
        ]
        for rid in stale:
            self._reports.pop(rid, None)

    async def _resolve_bot(self) -> tuple[Bot, bool]:
        """Возвращает (bot, owned). owned=True если bot создан нами и его надо закрыть."""
        if channel_subscription_service.bot is not None:
            return channel_subscription_service.bot, False
        from app.bot_factory import create_bot

        bot = create_bot()
        return bot, True

    async def _run_report(
        self,
        report_id: str,
        channel_id: str,
        admin_telegram_id: int | None,
    ) -> None:
        entry = self._reports[report_id]
        entry.progress.status = 'running'

        bot: Bot | None = None
        owned_bot = False
        try:
            bot, owned_bot = await self._resolve_bot()

            async with AsyncSessionLocal() as db:
                subscribers = await get_active_telegram_subscribers_for_report(db)

            entry.progress.total = len(subscribers)

            if entry.cancel_event.is_set():
                entry.progress.status = 'cancelled'
                return

            semaphore = asyncio.Semaphore(_API_CONCURRENCY)
            batch: list[tuple[int, bool]] = []

            async def check_one(subscriber: dict) -> None:
                if entry.cancel_event.is_set():
                    return

                async with semaphore:
                    is_member = await self._check_member(bot, subscriber['telegram_id'], channel_id)
                    await asyncio.sleep(_API_DELAY_SEC)

                entry.progress.processed += 1
                if is_member:
                    entry.progress.in_channel += 1
                else:
                    entry.progress.not_in_channel += 1
                    entry.non_members.append(
                        {
                            'user_id': subscriber['user_id'],
                            'telegram_id': subscriber['telegram_id'],
                            'username': subscriber['username'] or '',
                            'first_name': subscriber['first_name'] or '',
                            'last_name': subscriber['last_name'] or '',
                            'subscription_end_date': subscriber['subscription_end_date'],
                        }
                    )

                batch.append((subscriber['telegram_id'], is_member))
                if len(batch) >= _DB_BATCH:
                    await self._flush_db_batch(batch, channel_id)
                    batch.clear()

            # Последовательно (с internal concurrency через semaphore) — нам нужна
            # атомарность обновления прогресс-счётчиков и батча БД
            for subscriber in subscribers:
                if entry.cancel_event.is_set():
                    break
                await check_one(subscriber)

            # Flush остатка
            if batch:
                await self._flush_db_batch(batch, channel_id)
                batch.clear()

            if entry.cancel_event.is_set():
                entry.progress.status = 'cancelled'
                return

            # Генерируем CSV из non_members
            if entry.non_members:
                entry.csv_bytes = self._build_csv(entry.non_members)
                ts = datetime.now(UTC).strftime('%Y%m%d_%H%M%S')
                entry.filename = f'channel_report_{entry.progress.channel_db_id}_{ts}.csv'

            entry.progress.status = 'completed'

            # Отправляем CSV админу в Telegram DM (если есть CSV и admin_telegram_id)
            if admin_telegram_id and entry.csv_bytes and entry.filename:
                await self._send_csv_to_admin(bot, admin_telegram_id, entry)

        except asyncio.CancelledError:
            entry.progress.status = 'cancelled'
            raise
        except Exception as exc:
            logger.exception('Ошибка отчёта по каналу', report_id=report_id, channel_id=channel_id)
            entry.progress.status = 'failed'
            entry.progress.error_message = str(exc)
        finally:
            entry.progress.finished_at = datetime.now(UTC)
            if owned_bot and bot is not None:
                try:
                    await bot.session.close()
                except Exception:
                    pass

    async def _flush_db_batch(self, batch: list[tuple[int, bool]], channel_id: str) -> None:
        """Сохраняет батч {telegram_id: is_member} в UserChannelSubscription + Redis."""
        if not batch:
            return
        async with AsyncSessionLocal() as db:
            for telegram_id, is_member in batch:
                await upsert_user_channel_sub(db, telegram_id, channel_id, is_member)
            await db.commit()
        # Инвалидируем Redis после БД
        for telegram_id, is_member in batch:
            try:
                await ChannelSubCache.set_sub_status(telegram_id, channel_id, is_member)
            except Exception:
                pass

    async def _check_member(self, bot: Bot, telegram_id: int, channel_id: str) -> bool:
        """Живая проверка через Telegram API.

        Fail-closed: любая ошибка => False (не в канале).
        При FloodWait делает до 2-х попыток, уважая retry_after.
        """
        try:
            member = await bot.get_chat_member(chat_id=channel_id, user_id=telegram_id)
            return member.status in _GOOD_STATUSES
        except TelegramRetryAfter as exc:
            logger.warning('FloodWait в отчёте', retry_after=exc.retry_after, channel_id=channel_id)
            await asyncio.sleep(exc.retry_after + 0.5)
            try:
                member = await bot.get_chat_member(chat_id=channel_id, user_id=telegram_id)
                return member.status in _GOOD_STATUSES
            except Exception:
                return False
        except TelegramForbiddenError:
            logger.critical('Бот не имеет доступа к каналу', channel_id=channel_id)
            return False
        except TelegramBadRequest as exc:
            err = str(exc).lower()
            if (
                'user not found' in err
                or 'member not found' in err
                or 'participant_id_invalid' in err
                or 'chat not found' in err
            ):
                return False
            logger.error('BadRequest при проверке членства', channel_id=channel_id, error=str(exc))
            return False
        except TelegramNetworkError:
            return False
        except Exception as exc:
            logger.error('Неожиданная ошибка при проверке членства', error=str(exc))
            return False

    def _build_csv(self, rows: list[dict[str, Any]]) -> bytes:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                'user_id',
                'telegram_id',
                'username',
                'first_name',
                'last_name',
                'subscription_end_date',
            ]
        )
        for row in rows:
            end_date = row['subscription_end_date']
            if isinstance(end_date, datetime):
                end_date_str = end_date.isoformat()
            else:
                end_date_str = str(end_date) if end_date else ''
            writer.writerow(
                [
                    row['user_id'],
                    row['telegram_id'],
                    row['username'],
                    row['first_name'],
                    row['last_name'],
                    end_date_str,
                ]
            )
        return buf.getvalue().encode('utf-8-sig')

    async def _send_csv_to_admin(
        self,
        bot: Bot,
        admin_telegram_id: int,
        entry: _ReportEntry,
    ) -> None:
        from aiogram.types import BufferedInputFile

        try:
            caption = (
                f'📊 Отчёт по каналу {entry.progress.channel_title or entry.progress.channel_id}\n'
                f'Всего активных: {entry.progress.total}\n'
                f'✅ В канале: {entry.progress.in_channel}\n'
                f'❌ НЕ в канале: {entry.progress.not_in_channel}'
            )
            await bot.send_document(
                chat_id=admin_telegram_id,
                document=BufferedInputFile(entry.csv_bytes or b'', filename=entry.filename or 'report.csv'),
                caption=caption,
            )
        except Exception:
            logger.exception('Не удалось отправить CSV админу', admin_telegram_id=admin_telegram_id)


# Singleton
channel_membership_report_service = ChannelMembershipReportService()
