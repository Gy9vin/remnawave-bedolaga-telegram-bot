"""GigaChat API клиент с OAuth2 авторефрешем."""

import ssl
import time
import uuid

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class GigaChatClient:
    """Клиент GigaChat с OAuth2 авторефрешем (токен живёт 30 мин)."""

    AUTH_URL = 'https://ngw.devices.sberbank.ru:9443/api/v2/oauth'
    API_URL = 'https://gigachat.devices.sberbank.ru/api/v1/chat/completions'

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        # SSL без верификации — требование GigaChat
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    @property
    def _auth_key(self) -> str | None:
        return getattr(settings, 'GIGACHAT_AUTH_KEY', None)

    @property
    def _client_id(self) -> str | None:
        return getattr(settings, 'GIGACHAT_CLIENT_ID', None)

    @property
    def _scope(self) -> str:
        return getattr(settings, 'GIGACHAT_SCOPE', 'GIGACHAT_API_PERS')

    @property
    def _model(self) -> str:
        return getattr(settings, 'GIGACHAT_MODEL', 'GigaChat-2-MAX')

    async def _refresh_token(self) -> bool:
        """Получить новый OAuth2 токен."""
        if not self._auth_key:
            logger.warning('GIGACHAT_AUTH_KEY не задан')
            return False
        try:
            headers = {
                'Authorization': f'Basic {self._auth_key}',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept': 'application/json',
                'RqUID': str(uuid.uuid4()),
            }
            if self._client_id:
                headers['X-Client-ID'] = self._client_id
            connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    self.AUTH_URL,
                    data={'scope': self._scope},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error('GigaChat auth failed', status=resp.status, body=text)
                        return False
                    data = await resp.json()
                    self._access_token = data.get('access_token')
                    # expires_at в миллисекундах
                    expires_ms = data.get('expires_at', 0)
                    self._token_expires_at = (expires_ms / 1000.0) - 60  # -60 сек буфер
                    logger.info('GigaChat token refreshed')
                    return True
        except Exception as e:
            logger.error('GigaChat auth exception', error=e)
            return False

    async def _ensure_token(self) -> bool:
        """Убедиться что токен актуален, обновить если нет."""
        if self._access_token and time.time() < self._token_expires_at:
            return True
        return await self._refresh_token()

    async def chat(
        self,
        messages: list[dict],
        system_prompt: str = '',
        temperature: float = 0.3,
        max_tokens: int = 1000,
    ) -> str | None:
        """Отправить запрос в GigaChat и вернуть ответ."""
        if not await self._ensure_token():
            return None
        try:
            full_messages = []
            if system_prompt:
                full_messages.append({'role': 'system', 'content': system_prompt})
            full_messages.extend(messages)

            payload = {
                'model': self._model,
                'messages': full_messages,
                'temperature': temperature,
                'max_tokens': max_tokens,
            }
            headers = {
                'Authorization': f'Bearer {self._access_token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            }
            connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    self.API_URL,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error('GigaChat API error', status=resp.status, body=text[:300])
                        return None
                    data = await resp.json()
                    choices = data.get('choices', [])
                    if choices:
                        return choices[0].get('message', {}).get('content', '').strip()
                    return None
        except Exception as e:
            logger.error('GigaChat chat exception', error=e)
            return None


# Глобальный синглтон
gigachat_client = GigaChatClient()
