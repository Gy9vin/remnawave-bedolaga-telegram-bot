"""«there is no text in the message to edit» — гонка двойного клика.

При включённом ENABLE_LOGO_MODE патч превращает текстовое сообщение в фото
(edit_media с подписью). Telegram кладёт в callback_query снимок сообщения на
момент НАЖАТИЯ, поэтому при быстром втором клике по той же клавиатуре хендлер
получает объект, где `text` ещё заполнен, а на сервере сообщение уже медиа —
и `edit_text` падает с «there is no text in the message to edit».

Правильная реакция на эту ошибку — не падать, а поправить подпись: сообщение
существует, просто оно другого типа, чем думал вызывающий код.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.utils import message_patch


NO_TEXT_ERROR = 'Telegram server says - Bad Request: there is no text in the message to edit'


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=MagicMock(), message=message)


def _stale_message(monkeypatch, *, edit_caption_side_effect=None):
    """Сообщение, каким его видит хендлер: text есть, а на сервере — медиа."""
    message = MagicMock()
    message.text = 'старый текст'
    message.photo = None
    message.caption = None
    message.delete = AsyncMock()
    message.edit_caption = AsyncMock(side_effect=edit_caption_side_effect)

    async def raise_no_text(_self, _text, **_kwargs):
        raise _bad_request(NO_TEXT_ERROR)

    monkeypatch.setattr(message_patch, '_original_edit_text', raise_no_text)
    return message


@pytest.fixture(autouse=True)
def logo_mode_off(monkeypatch):
    # Ветка без логотипа — самый короткий путь до _original_edit_text.
    monkeypatch.setattr(message_patch.settings, 'ENABLE_LOGO_MODE', False)


@pytest.mark.asyncio
async def test_caption_is_edited_instead_of_text(monkeypatch):
    message = _stale_message(monkeypatch)

    await message_patch._edit_with_photo(message, 'новый текст', reply_markup='KB')

    message.edit_caption.assert_awaited_once()
    kwargs = message.edit_caption.await_args.kwargs
    assert kwargs['caption'] == 'новый текст'
    assert kwargs['reply_markup'] == 'KB'
    message.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_unsupported_kwargs_are_not_passed_to_edit_caption(monkeypatch):
    """У edit_caption нет disable_web_page_preview — его нельзя прокидывать."""
    message = _stale_message(monkeypatch)

    await message_patch._edit_with_photo(message, 'новый текст', disable_web_page_preview=True)

    assert 'disable_web_page_preview' not in message.edit_caption.await_args.kwargs


@pytest.mark.asyncio
async def test_unchanged_caption_is_swallowed(monkeypatch):
    """Повторный клик с тем же содержимым — не ошибка."""
    message = _stale_message(
        monkeypatch, edit_caption_side_effect=_bad_request('Bad Request: message is not modified')
    )

    assert await message_patch._edit_with_photo(message, 'тот же текст') is None
    message.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_falls_back_to_new_message_when_caption_edit_fails(monkeypatch):
    """Подпись не влезла или медиа не правится — заменяем сообщение целиком."""
    message = _stale_message(
        monkeypatch, edit_caption_side_effect=_bad_request('Bad Request: message caption is too long')
    )
    sent = AsyncMock(return_value='sent')
    monkeypatch.setattr(message_patch, '_text_answer', sent)

    result = await message_patch._edit_with_photo(message, 'новый текст')

    message.delete.assert_awaited_once()
    sent.assert_awaited_once()
    assert result == 'sent'


@pytest.mark.asyncio
async def test_long_text_skips_caption_and_replaces_message(monkeypatch):
    """Подпись длиннее лимита Telegram — в edit_caption даже не идём."""
    message = _stale_message(monkeypatch)
    sent = AsyncMock(return_value='sent')
    monkeypatch.setattr(message_patch, '_text_answer', sent)

    await message_patch._edit_with_photo(message, 'x' * 2000)

    message.edit_caption.assert_not_awaited()
    message.delete.assert_awaited_once()
    sent.assert_awaited_once()


@pytest.mark.asyncio
async def test_logo_mode_path_recovers_too(monkeypatch):
    """Прод-путь: логотип включён, у объекта есть text, на сервере — медиа."""
    monkeypatch.setattr(message_patch.settings, 'ENABLE_LOGO_MODE', True)
    message = _stale_message(monkeypatch)

    await message_patch._edit_with_photo(message, 'новый текст', reply_markup='KB')

    message.edit_caption.assert_awaited_once()
    assert message.edit_caption.await_args.kwargs['caption'] == 'новый текст'


@pytest.mark.asyncio
async def test_other_bad_requests_still_raise(monkeypatch):
    """Глушим только эту ошибку, остальные должны быть видны."""
    message = MagicMock()
    message.text = 'текст'
    message.photo = None

    async def raise_other(_self, _text, **_kwargs):
        raise _bad_request('Bad Request: CHAT_WRITE_FORBIDDEN')

    monkeypatch.setattr(message_patch, '_original_edit_text', raise_other)

    with pytest.raises(TelegramBadRequest):
        await message_patch._edit_with_photo(message, 'новый текст')
