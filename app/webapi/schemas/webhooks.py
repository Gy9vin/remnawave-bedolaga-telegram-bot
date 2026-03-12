from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


def _validate_webhook_url(url: str) -> str:
    """Проверяет URL на SSRF: запрещает приватные адреса и localhost."""
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'}:
        raise ValueError('URL должен начинаться с http:// или https://')
    hostname = parsed.hostname
    if not hostname:
        raise ValueError('Некорректный URL: нет hostname')
    # Проверка по имени
    if hostname.lower() in {'localhost', '127.0.0.1', '::1', '0.0.0.0'}:
        raise ValueError('Webhook URL не может указывать на localhost')
    # Проверка по IP-адресу
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise ValueError('Webhook URL не может указывать на приватные/зарезервированные адреса')
    except ValueError as exc:
        # Это может быть либо наше исключение (приватный IP), либо просто не IP (hostname)
        if 'не может' in str(exc):
            raise
        # hostname — доменное имя, не IP — OK
    return url


class WebhookCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    url: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1, max_length=50)
    secret: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None)

    @field_validator('url')
    @classmethod
    def validate_url(cls, v: str) -> str:
        return _validate_webhook_url(v)


class WebhookUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    url: str | None = Field(default=None, min_length=1)
    secret: str | None = Field(default=None, max_length=128)
    description: str | None = None
    is_active: bool | None = None

    @field_validator('url')
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_webhook_url(v)
        return v


class WebhookResponse(BaseModel):
    id: int
    name: str
    url: str
    event_type: str
    is_active: bool
    description: str | None
    created_at: datetime
    updated_at: datetime
    last_triggered_at: datetime | None
    failure_count: int
    success_count: int

    class Config:
        from_attributes = True


class WebhookListResponse(BaseModel):
    items: list[WebhookResponse]
    total: int
    limit: int
    offset: int


class WebhookDeliveryResponse(BaseModel):
    id: int
    webhook_id: int
    event_type: str
    payload: dict[str, Any]
    response_status: int | None
    response_body: str | None
    status: str
    error_message: str | None
    attempt_number: int
    created_at: datetime
    delivered_at: datetime | None
    next_retry_at: datetime | None

    class Config:
        from_attributes = True


class WebhookDeliveryListResponse(BaseModel):
    items: list[WebhookDeliveryResponse]
    total: int
    limit: int
    offset: int


class WebhookStatsResponse(BaseModel):
    total_webhooks: int
    active_webhooks: int
    total_deliveries: int
    successful_deliveries: int
    failed_deliveries: int
    success_rate: float
