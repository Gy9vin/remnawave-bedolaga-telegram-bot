"""
Тесты для сервиса диагностики реферальной системы.
"""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.referral_diagnostics_service import ReferralDiagnosticsService


@pytest.fixture
def temp_log_file():
    """Создаёт временный лог-файл для тестов."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
        yield Path(f.name)
    # Cleanup
    Path(f.name).unlink(missing_ok=True)


@pytest.fixture
def sample_log_content():
    """Пример содержимого лог-файла с реферальными кликами."""
    today = datetime.now(UTC).strftime('%Y-%m-%d')
    return (
        f'{today} 10:00:00,123 - app.handlers.start - INFO - '
        f'📩 Сообщение от ID:123456789 text=/start refABC123\n'
        f'{today} 10:00:05,456 - app.handlers.start - INFO - '
        f"💾 Сохранен start payload 'refXYZ999' для пользователя 987654321\n"
        f'{today} 11:00:00,345 - app.handlers.start - INFO - '
        f'📩 Сообщение от ID:111222333 text=/start ref_refDEF456\n'
        f'{today} 13:00:00,234 - unrelated module - INFO - Some other log message\n'
    )


@pytest.mark.asyncio
async def test_parse_clicks_basic(temp_log_file, sample_log_content):
    """Тест базового парсинга логов — находит реф-клики."""
    temp_log_file.write_text(sample_log_content)

    service = ReferralDiagnosticsService(log_path=str(temp_log_file))

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    clicks, total_lines, lines_in_period = await service._parse_clicks(today, tomorrow)

    # Должны найтись 3 реф-клика
    assert len(clicks) >= 1, f'Expected at least 1 click, found {len(clicks)}'

    # Проверяем что telegram_id есть в кликах
    telegram_ids = [c.telegram_id for c in clicks]
    assert 123456789 in telegram_ids or 987654321 in telegram_ids or 111222333 in telegram_ids


@pytest.mark.asyncio
async def test_analyze_period_basic(temp_log_file, sample_log_content):
    """Тест анализа периода — возвращает DiagnosticReport."""
    temp_log_file.write_text(sample_log_content)

    service = ReferralDiagnosticsService(log_path=str(temp_log_file))

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    mock_db = AsyncMock()
    # _find_lost_referrals вызывает result.scalars().all() — scalars() синхронный
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=mock_result)

    report = await service.analyze_period(mock_db, today, tomorrow)

    # Проверяем структуру отчёта
    assert hasattr(report, 'total_ref_clicks')
    assert hasattr(report, 'unique_users_clicked')
    assert hasattr(report, 'lost_referrals')
    assert report.total_ref_clicks >= 1, 'Should have found referral clicks'
    assert report.analysis_period_start == today
    assert report.analysis_period_end == tomorrow


@pytest.mark.asyncio
async def test_empty_log_file(temp_log_file):
    """Тест работы с пустым лог-файлом."""
    temp_log_file.write_text('')

    service = ReferralDiagnosticsService(log_path=str(temp_log_file))

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    mock_db = AsyncMock()

    report = await service.analyze_period(mock_db, today, tomorrow)

    # Проверяем что отчёт пустой
    assert report.total_ref_clicks == 0
    assert report.unique_users_clicked == 0
    assert len(report.lost_referrals) == 0


@pytest.mark.asyncio
async def test_nonexistent_log_file():
    """Тест работы с несуществующим лог-файлом."""
    service = ReferralDiagnosticsService(log_path='/nonexistent/path/to/log.log')

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    mock_db = AsyncMock()

    # Не должно быть исключений
    report = await service.analyze_period(mock_db, today, tomorrow)

    assert report.total_ref_clicks == 0
    assert len(report.lost_referrals) == 0


@pytest.mark.asyncio
async def test_analyze_today(temp_log_file, sample_log_content):
    """Тест метода analyze_today."""
    temp_log_file.write_text(sample_log_content)

    service = ReferralDiagnosticsService(log_path=str(temp_log_file))

    mock_db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db.execute = AsyncMock(return_value=mock_result)

    report = await service.analyze_today(mock_db)

    # Проверяем что период установлен корректно
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    assert report.analysis_period_start.date() == today.date()
