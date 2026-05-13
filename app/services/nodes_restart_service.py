"""Автоматическая принудительная перезагрузка всех нод Remnawave.

Дёргает POST /api/nodes/actions/restart-all с forceRestart=true по двум сценариям:
- interval: каждые N часов от последнего рестарта
- daily: раз в сутки в указанный час UTC

Запускается из monitoring loop (раз в минуту). State (last_run_at) держим
в памяти — при рестарте бота он сбрасывается, тогда следующий авто-рестарт
случится через интервал. Это приемлемо.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class _State:
    last_run_at: datetime | None = None
    last_daily_fired_date: str | None = None  # YYYY-MM-DD UTC, чтобы не палить дважды в день
    last_result_ok: bool | None = None
    last_error: str | None = None


_state = _State()


def get_state() -> dict:
    return {
        'last_run_at': _state.last_run_at,
        'last_result_ok': _state.last_result_ok,
        'last_error': _state.last_error,
        'last_daily_fired_date': _state.last_daily_fired_date,
    }


def _should_fire(now: datetime) -> bool:
    if not getattr(settings, 'NODES_AUTO_RESTART_ENABLED', False):
        return False

    mode = (getattr(settings, 'NODES_AUTO_RESTART_MODE', 'interval') or 'interval').lower()

    if mode == 'daily':
        target_hour = int(getattr(settings, 'NODES_AUTO_RESTART_AT_HOUR', 4) or 0) % 24
        today_str = now.strftime('%Y-%m-%d')
        if _state.last_daily_fired_date == today_str:
            return False
        return now.hour == target_hour

    # interval
    interval_hours = max(1, int(getattr(settings, 'NODES_AUTO_RESTART_INTERVAL_HOURS', 24) or 24))
    if _state.last_run_at is None:
        # При первом запуске бота не палим сразу — ждём один интервал.
        # Зафиксируем «как будто запустили только что» чтобы интервал начался от старта.
        _state.last_run_at = now
        return False
    return (now - _state.last_run_at) >= timedelta(hours=interval_hours)


async def run_restart_all(force: bool | None = None, reason: str = 'auto') -> bool:
    """Сделать POST /api/nodes/actions/restart-all. Возвращает True/False."""
    from app.services.remnawave_service import remnawave_service

    force_value = bool(force) if force is not None else bool(getattr(settings, 'NODES_AUTO_RESTART_FORCE', True))
    try:
        async with remnawave_service.get_api_client() as api:
            ok = await api.restart_all_nodes(force_restart=force_value)
        _state.last_run_at = datetime.now(UTC)
        _state.last_result_ok = bool(ok)
        _state.last_error = None
        logger.info(
            '🔁 Remnawave: запрошен перезапуск всех нод',
            force=force_value,
            reason=reason,
            event_sent=bool(ok),
        )
        return bool(ok)
    except Exception as exc:
        _state.last_run_at = datetime.now(UTC)
        _state.last_result_ok = False
        _state.last_error = str(exc)
        logger.error(
            'Ошибка авто-рестарта нод Remnawave',
            force=force_value,
            reason=reason,
            error=str(exc),
        )
        return False


async def maybe_run_periodic() -> None:
    """Точка входа из monitoring loop. Ничего не делает если ещё не пора / выключено."""
    now = datetime.now(UTC)
    if not _should_fire(now):
        return

    ok = await run_restart_all(reason='scheduler')
    if (getattr(settings, 'NODES_AUTO_RESTART_MODE', 'interval') or 'interval').lower() == 'daily':
        _state.last_daily_fired_date = now.strftime('%Y-%m-%d')
    logger.info('🔁 Авто-рестарт нод по таймеру', ok=ok, mode=getattr(settings, 'NODES_AUTO_RESTART_MODE', 'interval'))
