"""Pydantic v2 schemas для отчётов по обязательным каналам."""

from datetime import datetime

from pydantic import BaseModel


class ChannelReportStartResponse(BaseModel):
    report_id: str


class ChannelReportStatusResponse(BaseModel):
    report_id: str
    status: str  # pending | running | completed | failed | cancelled
    channel_db_id: int
    channel_id: str
    channel_title: str | None = None
    total: int = 0
    processed: int = 0
    in_channel: int = 0
    not_in_channel: int = 0
    errors: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    has_csv: bool = False
