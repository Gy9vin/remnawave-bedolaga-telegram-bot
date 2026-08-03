#!/usr/bin/env python3
"""
Массовый бэкфилл User.remnawave_id для RemnaWave v3.

Контекст: панель теперь на v3, где пользователь адресуется числовым id
(колонка ``users.remnawave_id``, migrations/alembic/versions/9026_user_remnawave_id.py).
Колонка добавлена БЕЗ данных и заполняется лениво — только когда
``get_panel_user_ref()`` (app/services/remnawave_service.py) резолвит
конкретного пользователя в рамках уже идущего запроса. Из-за этого у
большинства существующих пользователей поле пустует, и любой путь, где
ленивый резолв ещё не отработал (или отработать не может — например,
read-only контексты), либо падает, либо тихо не отрабатывает.

Этот скрипт закрывает разрыв одним проходом:

  1. Строит ОДИН индекс всех пользователей панели через постраничный
     курсорный стрим ``GET /api/users/stream`` (shortUuid → id, telegramId →
     id, email → id). Это на порядок дешевле, чем резолвить каждого
     локального пользователя отдельным HTTP-запросом
     (resolve_user_id/get_user_by_telegram_id/get_user_by_email) — при
     тысячах пользователей с NULL один проход по стриму заменяет тысячи
     round-trip'ов.
  2. Идёт по локальным пользователям с ``remnawave_id IS NULL`` батчами и
     сопоставляет их с индексом панели по ТОЙ ЖЕ логике приоритетов, что и
     ``get_panel_user_ref`` + email-фоллбек, применяемый в остальном коде
     (app/services/subscription_service.py, app/cabinet/routes/admin_users.py
     и т.д.): short_uuid (по самой свежей подписке) → telegram_id → email.
  3. Пишет найденный remnawave_id, коммитит по батчам (не одной гигантской
     транзакцией) и печатает итоговую статистику.

Идемпотентность и устойчивость к сбоям: скрипт всегда выбирает только
строки с ``remnawave_id IS NULL``, поэтому повторный запуск (в т.ч. после
сбоя на середине) просто продолжает с того места, где предыдущий прогон
остановился — уже проставленные строки не трогаются и повторно не
обрабатываются.

Использование:
    ./.venv/bin/python backfill_remnawave_ids.py --dry-run
    ./.venv/bin/python backfill_remnawave_ids.py
    ./.venv/bin/python backfill_remnawave_ids.py --batch-size 300 --page-size 500
    ./.venv/bin/python backfill_remnawave_ids.py --dry-run --limit 50   # прогон на пробной выборке
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import User
from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s %(message)s',
)
logger = logging.getLogger('backfill_remnawave_ids')


# ============================================================
# Чистая логика сопоставления (без сети/БД) — покрыта тестами в
# tests/services/test_backfill_remnawave_ids.py
# ============================================================


@dataclass
class PanelIndex:
    """Индекс пользователей панели RemnaWave v3, построенный одним проходом
    по /api/users/stream, для офлайн-сопоставления с локальной БД."""

    by_short_uuid: dict[str, int] = field(default_factory=dict)
    by_telegram_id: dict[int, list[int]] = field(default_factory=dict)
    by_email: dict[str, list[int]] = field(default_factory=dict)
    total_panel_users: int = 0

    def add(
        self,
        *,
        short_uuid: str | None,
        telegram_id: int | None,
        email: str | None,
        panel_id: int | None,
    ) -> None:
        self.total_panel_users += 1
        if panel_id is None:
            return

        if short_uuid:
            existing = self.by_short_uuid.get(short_uuid)
            if existing is not None and existing != panel_id:
                logger.warning(
                    'Дублирующийся shortUuid в панели: %s уже указывает на id=%s, '
                    'повторно встречен с id=%s — оставляем первый',
                    short_uuid,
                    existing,
                    panel_id,
                )
            else:
                self.by_short_uuid[short_uuid] = panel_id

        if telegram_id:
            self.by_telegram_id.setdefault(telegram_id, []).append(panel_id)

        if email:
            self.by_email.setdefault(email.strip().lower(), []).append(panel_id)


@dataclass
class MatchResult:
    panel_id: int | None
    source: str | None = None  # 'short_uuid' | 'telegram_id' | 'email'
    reason: str | None = None  # заполняется, когда panel_id is None


def select_short_uuid(subscriptions: list) -> str | None:
    """Первый непустой remnawave_short_uuid среди подписок пользователя.

    ``user.subscriptions`` уже упорядочены как ``created_at DESC``
    (см. relationship на User в app/database/models.py), поэтому «первый»
    здесь означает «у самой свежей подписки». Зеркалирует цикл в
    get_panel_user_ref: берётся ровно ОДИН кандидат, без перебора остальных
    подписок при промахе резолва — так же ведёт себя и живой путь.
    """
    for sub in subscriptions:
        short_uuid = getattr(sub, 'remnawave_short_uuid', None)
        if short_uuid:
            return short_uuid
    return None


def match_user_to_panel_id(
    *,
    short_uuid: str | None,
    telegram_id: int | None,
    email: str | None,
    index: PanelIndex,
) -> MatchResult:
    """Сопоставляет локального пользователя с id панели.

    Приоритет источников — тот же, что используется по всему коду для
    резолва панельного пользователя: short_uuid > telegram_id > email.

    Если по источнику найдено НЕСКОЛЬКО РАЗНЫХ id панели (в панели, например,
    задублировался telegramId/email на разные аккаунты) — сопоставление
    сознательно не выполняется (reason='ambiguous_*'), вместо угадывания
    первого попавшегося: это разовый массовый бэкфилл, ошибочная запись
    remnawave_id не самовосстановится сама.
    """
    if short_uuid:
        panel_id = index.by_short_uuid.get(short_uuid)
        if panel_id is not None:
            return MatchResult(panel_id, source='short_uuid')

    if telegram_id:
        candidates = index.by_telegram_id.get(telegram_id)
        if candidates:
            distinct = set(candidates)
            if len(distinct) > 1:
                return MatchResult(None, reason='ambiguous_telegram_id')
            return MatchResult(next(iter(distinct)), source='telegram_id')

    if email:
        candidates = index.by_email.get(email.strip().lower())
        if candidates:
            distinct = set(candidates)
            if len(distinct) > 1:
                return MatchResult(None, reason='ambiguous_email')
            return MatchResult(next(iter(distinct)), source='email')

    if not short_uuid and not telegram_id and not email:
        return MatchResult(None, reason='no_signal')

    return MatchResult(None, reason='not_found')


@dataclass
class Stats:
    matched: int = 0
    matched_by: dict[str, int] = field(
        default_factory=lambda: {'short_uuid': 0, 'telegram_id': 0, 'email': 0}
    )
    not_found: int = 0
    not_found_by_reason: dict[str, int] = field(
        default_factory=lambda: {
            'no_signal': 0,
            'not_found': 0,
            'ambiguous_telegram_id': 0,
            'ambiguous_email': 0,
        }
    )

    @property
    def processed(self) -> int:
        return self.matched + self.not_found


def process_batch(
    users: list[User],
    index: PanelIndex,
    *,
    dry_run: bool,
    stats: Stats,
) -> None:
    """Сопоставляет батч ORM-пользователей с индексом панели.

    В dry-run режиме ничего не пишет в атрибуты ORM-объектов (кроме
    подсчёта статистики) — вызывающий код просто не должен коммитить сессию.
    """
    for user in users:
        short_uuid = select_short_uuid(list(user.subscriptions or []))
        match = match_user_to_panel_id(
            short_uuid=short_uuid,
            telegram_id=user.telegram_id,
            email=user.email,
            index=index,
        )

        if match.panel_id is not None:
            stats.matched += 1
            stats.matched_by[match.source] += 1
            logger.info(
                'MATCH user_id=%s telegram_id=%s email=%s -> remnawave_id=%s (source=%s)%s',
                user.id,
                user.telegram_id,
                user.email,
                match.panel_id,
                match.source,
                ' [dry-run]' if dry_run else '',
            )
            if not dry_run:
                user.remnawave_id = match.panel_id
        else:
            stats.not_found += 1
            stats.not_found_by_reason[match.reason] += 1
            logger.debug(
                'NO MATCH user_id=%s telegram_id=%s email=%s reason=%s',
                user.id,
                user.telegram_id,
                user.email,
                match.reason,
            )


# ============================================================
# I/O: панель (стрим) + БД (батчи)
# ============================================================


async def build_panel_index(api: RemnaWaveAPI, page_size: int) -> PanelIndex:
    """Один полный проход по /api/users/stream, без обогащения happ-ссылок
    (enrich_happ_links=False) — они здесь не нужны и стоят лишнего запроса
    на каждого пользователя."""
    index = PanelIndex()
    cursor: str | None = None
    page_no = 0

    while True:
        page_no += 1
        page = await api.get_all_users_page_stream(
            cursor=cursor, size=page_size, enrich_happ_links=False
        )
        for panel_user in page['users']:
            index.add(
                short_uuid=panel_user.short_uuid,
                telegram_id=panel_user.telegram_id,
                email=panel_user.email,
                panel_id=panel_user.id,
            )
        logger.info(
            'Панель: страница %d, +%d пользователей (всего %d)',
            page_no,
            len(page['users']),
            index.total_panel_users,
        )
        if not page['hasMore'] or not page['nextCursor']:
            break
        cursor = page['nextCursor']

    return index


async def fetch_null_batch(db, last_id: int, batch_size: int) -> list[User]:
    """Следующий батч пользователей с remnawave_id IS NULL, id > last_id.

    Курсор по id (а не offset) — так, после того как в батче что-то
    проставили (и строка перестала попадать под remnawave_id IS NULL),
    следующая выборка всё равно не возвращает уже пройденные id повторно.
    """
    result = await db.execute(
        select(User)
        .where(User.remnawave_id.is_(None), User.id > last_id)
        .options(selectinload(User.subscriptions))
        .order_by(User.id)
        .limit(batch_size)
    )
    return list(result.scalars().all())


def print_summary(stats: Stats, total_null_at_start: int, *, dry_run: bool) -> None:
    sep = '=' * 60
    print(f'\n{sep}')
    print('Бэкфилл User.remnawave_id (RemnaWave v3) — итоги')
    print(sep)
    print(f'Пользователей с remnawave_id IS NULL (на старте): {total_null_at_start}')
    print(f'Обработано:                                       {stats.processed}')
    print(f'  Сопоставлено:                                   {stats.matched}')
    print(f'    ├─ по short_uuid:                             {stats.matched_by["short_uuid"]}')
    print(f'    ├─ по telegram_id:                            {stats.matched_by["telegram_id"]}')
    print(f'    └─ по email:                                  {stats.matched_by["email"]}')
    print(f'  Не найдено:                                     {stats.not_found}')
    print(f'    ├─ нет признаков для резолва (no_signal):     {stats.not_found_by_reason["no_signal"]}')
    print(f'    ├─ не найден в панели (not_found):            {stats.not_found_by_reason["not_found"]}')
    print(f'    ├─ неоднозначно по telegram_id (ambiguous):   {stats.not_found_by_reason["ambiguous_telegram_id"]}')
    print(f'    └─ неоднозначно по email (ambiguous):         {stats.not_found_by_reason["ambiguous_email"]}')
    print(sep)
    if dry_run:
        print('Режим: DRY RUN — в БД ничего не записано')
    else:
        print('Режим: PRODUCTION — remnawave_id записан и закоммичен')
    print(f'{sep}\n')


async def run(args: argparse.Namespace) -> int:
    auth_params = settings.get_remnawave_auth_params()
    if not auth_params.get('base_url') or not auth_params.get('api_key'):
        logger.error('REMNAWAVE_API_URL / REMNAWAVE_API_KEY не настроены — бэкфилл невозможен')
        return 1

    api = RemnaWaveAPI(**auth_params)
    stats = Stats()

    async with api:
        try:
            api_version = await api.get_api_version()
        except RemnaWaveAPIError as e:
            logger.error('Не удалось определить версию API панели: %s', e)
            return 1

        if api_version != 3:
            logger.info(
                'Панель работает на API v%s — remnawave_id используется только на v3, '
                'бэкфилл не требуется',
                api_version,
            )
            return 0

        logger.info(
            'Строим индекс пользователей панели через /api/users/stream (page_size=%d)…',
            args.page_size,
        )
        try:
            index = await build_panel_index(api, args.page_size)
        except RemnaWaveAPIError as e:
            logger.error('Не удалось получить список пользователей панели: %s', e)
            return 1

        logger.info(
            'Индекс построен: %d пользователей панели, %d уникальных short_uuid, '
            '%d уникальных telegram_id, %d уникальных email',
            index.total_panel_users,
            len(index.by_short_uuid),
            len(index.by_telegram_id),
            len(index.by_email),
        )

        async with AsyncSessionLocal() as db:
            total_null_at_start = await db.scalar(
                select(func.count()).select_from(User).where(User.remnawave_id.is_(None))
            )
            logger.info('Пользователей с remnawave_id IS NULL: %d', total_null_at_start)

            last_id = 0
            processed = 0

            while True:
                if args.limit is not None and processed >= args.limit:
                    logger.info('Достигнут --limit=%d, останавливаемся', args.limit)
                    break

                batch_size = args.batch_size
                if args.limit is not None:
                    batch_size = min(batch_size, args.limit - processed)

                users = await fetch_null_batch(db, last_id, batch_size)
                if not users:
                    break

                last_id = users[-1].id
                process_batch(users, index, dry_run=args.dry_run, stats=stats)

                if not args.dry_run:
                    await db.commit()

                processed += len(users)
                logger.info(
                    'Батч обработан: %d пользователей (курсор id>%d, всего обработано %d)',
                    len(users),
                    last_id,
                    processed,
                )

    print_summary(stats, total_null_at_start, dry_run=args.dry_run)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Массовый бэкфилл User.remnawave_id для RemnaWave v3',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Ничего не писать в БД — только построить статистику сопоставления',
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=500,
        help='Сколько пользователей БД обрабатывать и коммитить за один батч (default: 500)',
    )
    parser.add_argument(
        '--page-size',
        type=int,
        default=500,
        help='Размер страницы /api/users/stream панели, 1..1000 (default: 500)',
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=None,
        help='Максимум пользователей БД для обработки за запуск (для пробного прогона)',
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    exit_code = asyncio.run(run(args))
    sys.exit(exit_code)


if __name__ == '__main__':
    main()
