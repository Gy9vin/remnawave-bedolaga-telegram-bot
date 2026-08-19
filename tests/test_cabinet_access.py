"""Тесты доступа к веб-кабинету: хелпер has_cabinet_access + гейт зависимости.

Покрывает:
  1. has_cabinet_access — поведение при разных комбинациях флагов.
  2. get_current_cabinet_user — 403 cabinet_access_denied при отсутствии доступа.
  3. get_current_cabinet_user — пропускает пользователя при cabinet_access=True.
  4. get_current_cabinet_user — пропускает пользователя при CABINET_OPEN_TO_ALL=True.
  5. get_current_cabinet_user — пропускает админа без cabinet_access.
  8. _build_cabinet_main_menu_keyboard — скрывает кнопки ЛК при has_cabinet_access=False.
  9. _build_cabinet_main_menu_keyboard — показывает кнопки ЛК при has_cabinet_access=True.
  10. get_user_management_keyboard — показывает «Включить» при cabinet_access=False.
  11. get_user_management_keyboard — показывает «Выключить» при cabinet_access=True.
  12. toggle_cabinet_access — устанавливает cabinet_access в True/False и коммитит.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status as http_status

from app.config import Settings
from app.database.models import UserStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    *,
    user_id: int = 1,
    telegram_id: int = 100,
    cabinet_access: bool = False,
    email: str | None = None,
    email_verified: bool = False,
    status_value: str = UserStatus.ACTIVE.value,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        telegram_id=telegram_id,
        username='testuser',
        email=email,
        email_verified=email_verified,
        status=status_value,
        cabinet_access=cabinet_access,
        balance_kopeks=0,
        last_activity=datetime(2025, 1, 1, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
        cabinet_last_login=None,
        referral_code='ref',
        referred_by_id=None,
        remnawave_uuid=None,
    )


def _make_request() -> MagicMock:
    req = MagicMock()
    req.headers = MagicMock()
    req.headers.get = MagicMock(return_value=None)
    req.method = 'GET'
    req.url = MagicMock()
    req.url.path = '/cabinet/test'
    return req


def _credentials(token: str = 'fake.jwt.token') -> MagicMock:  # noqa: S107 — test sentinel
    return MagicMock(credentials=token)


@pytest.fixture
def db() -> AsyncMock:
    session = AsyncMock()
    session.commit = AsyncMock(return_value=None)
    session.refresh = AsyncMock(return_value=None)
    return session


# ---------------------------------------------------------------------------
# 1-3. Юнит-тесты хелпера has_cabinet_access
# ---------------------------------------------------------------------------


def test_has_cabinet_access_true_when_user_flag_set() -> None:
    """cabinet_access=True → доступ независимо от глобального флага."""
    from app.utils.cabinet_access import has_cabinet_access

    user = _make_user(cabinet_access=True)
    with patch('app.utils.cabinet_access.settings') as mock_settings:
        mock_settings.CABINET_OPEN_TO_ALL = False
        assert has_cabinet_access(user) is True


def test_has_cabinet_access_true_when_global_flag_set() -> None:
    """CABINET_OPEN_TO_ALL=True → доступ независимо от user.cabinet_access."""
    from app.utils.cabinet_access import has_cabinet_access

    user = _make_user(cabinet_access=False)
    with patch('app.utils.cabinet_access.settings') as mock_settings:
        mock_settings.CABINET_OPEN_TO_ALL = True
        assert has_cabinet_access(user) is True


def test_has_cabinet_access_false_when_both_false() -> None:
    """Оба флага False → доступа нет."""
    from app.utils.cabinet_access import has_cabinet_access

    user = _make_user(cabinet_access=False)
    with patch('app.utils.cabinet_access.settings') as mock_settings:
        mock_settings.CABINET_OPEN_TO_ALL = False
        assert has_cabinet_access(user) is False


# ---------------------------------------------------------------------------
# 4. Гейт: 403 при отсутствии доступа
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dependency_raises_403_when_no_cabinet_access(db: AsyncMock) -> None:
    """Активный пользователь без cabinet_access и без глобального флага → 403."""
    from app.cabinet.dependencies import get_current_cabinet_user

    user = _make_user(cabinet_access=False)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '1', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch(
            'app.cabinet.dependencies.blacklist_service.is_user_blacklisted',
            AsyncMock(return_value=(False, None)),
        ),
        patch('app.cabinet.dependencies.maintenance_service.is_maintenance_active', return_value=False),
        patch('app.cabinet.dependencies.settings.CHANNEL_IS_REQUIRED_SUB', False, create=True),
        patch('app.cabinet.dependencies.has_cabinet_access', return_value=False),
        # is_admin — метод класса Settings; патчим через patch.object на типе
        patch.object(Settings, 'is_admin', return_value=False),
    ):
        with pytest.raises(HTTPException) as exc:
            await get_current_cabinet_user(
                request=_make_request(),
                credentials=_credentials(),
                db=db,
            )

    assert exc.value.status_code == http_status.HTTP_403_FORBIDDEN
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail['code'] == 'cabinet_access_denied'


# ---------------------------------------------------------------------------
# 5. Гейт: пропуск при cabinet_access=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dependency_passes_when_user_cabinet_access_true(db: AsyncMock) -> None:
    """cabinet_access=True → пользователь проходит гейт."""
    from app.cabinet.dependencies import get_current_cabinet_user

    user = _make_user(cabinet_access=True)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '1', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch(
            'app.cabinet.dependencies.blacklist_service.is_user_blacklisted',
            AsyncMock(return_value=(False, None)),
        ),
        patch('app.cabinet.dependencies.maintenance_service.is_maintenance_active', return_value=False),
        patch('app.cabinet.dependencies.settings.CHANNEL_IS_REQUIRED_SUB', False, create=True),
        patch('app.cabinet.dependencies.has_cabinet_access', return_value=True),
        patch('app.cabinet.dependencies.schedule_cabinet_action_log', MagicMock()),
    ):
        result = await get_current_cabinet_user(
            request=_make_request(),
            credentials=_credentials(),
            db=db,
        )

    assert result is user


# ---------------------------------------------------------------------------
# 6. Гейт: пропуск при CABINET_OPEN_TO_ALL=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dependency_passes_when_global_flag_set(db: AsyncMock) -> None:
    """CABINET_OPEN_TO_ALL=True → пользователь без cabinet_access всё равно проходит."""
    from app.cabinet.dependencies import get_current_cabinet_user

    user = _make_user(cabinet_access=False)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '1', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch(
            'app.cabinet.dependencies.blacklist_service.is_user_blacklisted',
            AsyncMock(return_value=(False, None)),
        ),
        patch('app.cabinet.dependencies.maintenance_service.is_maintenance_active', return_value=False),
        patch('app.cabinet.dependencies.settings.CHANNEL_IS_REQUIRED_SUB', False, create=True),
        patch('app.cabinet.dependencies.has_cabinet_access', return_value=True),
        patch('app.cabinet.dependencies.schedule_cabinet_action_log', MagicMock()),
    ):
        result = await get_current_cabinet_user(
            request=_make_request(),
            credentials=_credentials(),
            db=db,
        )

    assert result is user


# ---------------------------------------------------------------------------
# 7. Гейт: администратор проходит без cabinet_access
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dependency_passes_admin_without_cabinet_access(db: AsyncMock) -> None:
    """Администратор без cabinet_access не получает 403 (admin bypass)."""
    from app.cabinet.dependencies import get_current_cabinet_user

    user = _make_user(cabinet_access=False, telegram_id=777)

    with (
        patch('app.cabinet.dependencies.get_token_payload', return_value={'sub': '1', 'type': 'access'}),
        patch('app.cabinet.dependencies.get_user_by_id', AsyncMock(return_value=user)),
        patch(
            'app.cabinet.dependencies.blacklist_service.is_user_blacklisted',
            AsyncMock(return_value=(False, None)),
        ),
        patch('app.cabinet.dependencies.maintenance_service.is_maintenance_active', return_value=False),
        patch('app.cabinet.dependencies.settings.CHANNEL_IS_REQUIRED_SUB', False, create=True),
        patch('app.cabinet.dependencies.has_cabinet_access', return_value=False),
        # is_admin — метод класса Settings; патчим через patch.object на типе
        patch.object(Settings, 'is_admin', return_value=True),
        patch('app.cabinet.dependencies.schedule_cabinet_action_log', MagicMock()),
    ):
        result = await get_current_cabinet_user(
            request=_make_request(),
            credentials=_credentials(),
            db=db,
        )

    assert result is user


# ---------------------------------------------------------------------------
# 8-9. Клавиатура: скрытие/показ кнопок ЛК
# ---------------------------------------------------------------------------


class _StubTexts:
    MENU_SUBSCRIPTION = '📱 Подписка'
    MENU_REFERRALS = '🤝 Рефералы'
    MENU_SUPPORT = '💬 Поддержка'
    MENU_ADMIN = '🛠 Админ'
    BACK = '⬅️ Назад'
    BALANCE_BUTTON = '💰 Баланс: {balance}'

    def t(self, key, default=''):
        return default

    def format_price(self, kopeks: int) -> str:
        return f'{kopeks / 100:.2f} ₽'


def _has_webapp_buttons(markup) -> bool:
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.web_app:
                return True
    return False


def test_cabinet_keyboard_hides_buttons_when_no_access() -> None:
    """has_cabinet_access=False — кнопки с WebApp-ссылками не добавляются."""
    from unittest.mock import MagicMock, patch

    from app.keyboards.inline import _build_cabinet_main_menu_keyboard

    stub_texts = _StubTexts()
    fake_layout = {
        'row_1': {'buttons': ['home', 'subscription', 'balance'], 'max_per_row': 3},
    }
    fake_styles: dict = {}

    # Импорты происходят внутри функции, патчим по исходному пути модуля
    with (
        patch('app.utils.menu_layout_cache.get_cached_menu_layout', return_value=fake_layout),
        patch('app.utils.button_styles_cache.get_cached_button_styles', return_value=fake_styles),
        patch('app.utils.miniapp_buttons.build_cabinet_url', return_value='https://example.com'),
        patch('app.utils.miniapp_buttons._resolve_style', return_value=None),
        patch('app.utils.miniapp_buttons.CALLBACK_TO_CABINET_STYLE', {}),
        patch('app.utils.button_styles_cache.CALLBACK_TO_SECTION', {}),
        patch('app.keyboards.inline.settings') as mock_settings,
    ):
        mock_settings.CABINET_BUTTON_STYLE = ''
        mock_settings.is_referral_program_enabled.return_value = True
        mock_settings.is_language_selection_enabled.return_value = True
        mock_settings.is_multi_tariff_enabled.return_value = False

        markup = _build_cabinet_main_menu_keyboard(
            'ru',
            stub_texts,
            is_admin=False,
            is_moderator=False,
            has_cabinet_access=False,
        )

    assert not _has_webapp_buttons(markup), 'WebApp-кнопок быть не должно при has_cabinet_access=False'


def test_cabinet_keyboard_shows_buttons_when_access_granted() -> None:
    """has_cabinet_access=True — кнопки с WebApp-ссылками присутствуют."""
    from unittest.mock import patch

    from app.keyboards.inline import _build_cabinet_main_menu_keyboard

    stub_texts = _StubTexts()
    fake_layout = {
        'row_1': {'buttons': ['home', 'subscription'], 'max_per_row': 2},
    }
    fake_styles: dict = {}

    with (
        patch('app.utils.menu_layout_cache.get_cached_menu_layout', return_value=fake_layout),
        patch('app.utils.button_styles_cache.get_cached_button_styles', return_value=fake_styles),
        patch('app.utils.miniapp_buttons.build_cabinet_url', return_value='https://example.com'),
        patch('app.utils.miniapp_buttons._resolve_style', return_value=None),
        patch('app.utils.miniapp_buttons.CALLBACK_TO_CABINET_STYLE', {}),
        patch('app.utils.button_styles_cache.CALLBACK_TO_SECTION', {}),
        patch('app.keyboards.inline.settings') as mock_settings,
    ):
        mock_settings.CABINET_BUTTON_STYLE = ''
        mock_settings.is_referral_program_enabled.return_value = True
        mock_settings.is_language_selection_enabled.return_value = True
        mock_settings.is_multi_tariff_enabled.return_value = False

        markup = _build_cabinet_main_menu_keyboard(
            'ru',
            stub_texts,
            is_admin=False,
            is_moderator=False,
            has_cabinet_access=True,
        )

    assert _has_webapp_buttons(markup), 'WebApp-кнопки должны быть при has_cabinet_access=True'


# ---------------------------------------------------------------------------
# 10-11. get_user_management_keyboard — cabinet_access toggle button text
# ---------------------------------------------------------------------------


def test_admin_keyboard_shows_enable_when_no_cabinet_access() -> None:
    """cabinet_access=False — кнопка содержит слово 'Включить'."""
    from app.keyboards.admin import get_user_management_keyboard

    kb = get_user_management_keyboard(42, 'active', cabinet_access=False)
    btns = [
        btn.text
        for row in kb.inline_keyboard
        for btn in row
        if 'cabinet_toggle' in (btn.callback_data or '')
    ]
    assert btns, 'Кнопка cabinet_toggle не найдена'
    assert 'Включить' in btns[0]


def test_admin_keyboard_shows_disable_when_cabinet_access_true() -> None:
    """cabinet_access=True — кнопка содержит слово 'Выключить'."""
    from app.keyboards.admin import get_user_management_keyboard

    kb = get_user_management_keyboard(42, 'active', cabinet_access=True)
    btns = [
        btn.text
        for row in kb.inline_keyboard
        for btn in row
        if 'cabinet_toggle' in (btn.callback_data or '')
    ]
    assert btns, 'Кнопка cabinet_toggle не найдена'
    assert 'Выключить' in btns[0]


# ---------------------------------------------------------------------------
# 12. toggle_cabinet_access — устанавливает поле и коммитит
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_cabinet_access_sets_flag_and_commits() -> None:
    """toggle_cabinet_access переключает cabinet_access=False→True и вызывает db.commit()."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.handlers.admin.users import toggle_cabinet_access

    target_user = _make_user(user_id=99, cabinet_access=False)
    target_user.status = 'active'

    db = AsyncMock()
    db.commit = AsyncMock(return_value=None)

    admin_user = _make_user(user_id=1, telegram_id=111)
    admin_user.language = 'ru'  # требуется для get_user_management_keyboard

    callback = MagicMock()
    callback.data = 'admin_user_cabinet_toggle_99'
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.edit_reply_markup = AsyncMock()

    with (
        patch('app.handlers.admin.users.get_user_by_id', AsyncMock(return_value=target_user)),
        patch('app.handlers.admin.users.get_user_management_keyboard', return_value=MagicMock()),
    ):
        # Два уровня __wrapped__: admin_required → error_handler → actual func
        actual_func = toggle_cabinet_access.__wrapped__.__wrapped__
        await actual_func(callback, admin_user, db)

    assert target_user.cabinet_access is True
    db.commit.assert_awaited_once()
