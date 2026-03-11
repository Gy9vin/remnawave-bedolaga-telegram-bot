"""Add missing user columns (email_change, oauth providers, partner_status)

Revision ID: 0040
Revises: 0039
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision: str = '0040'
down_revision: Union[str, None] = '0039'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    cols = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in cols


def upgrade() -> None:
    conn = op.get_bind()

    # Email change columns
    if not _column_exists('users', 'email_change_new'):
        conn.execute(text('ALTER TABLE users ADD COLUMN email_change_new VARCHAR(255)'))

    if not _column_exists('users', 'email_change_code'):
        conn.execute(text('ALTER TABLE users ADD COLUMN email_change_code VARCHAR(6)'))

    if not _column_exists('users', 'email_change_expires'):
        conn.execute(text('ALTER TABLE users ADD COLUMN email_change_expires TIMESTAMP WITH TIME ZONE'))

    # OAuth provider IDs
    if not _column_exists('users', 'google_id'):
        conn.execute(text('ALTER TABLE users ADD COLUMN google_id VARCHAR(255)'))
        conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_id ON users (google_id)'))

    if not _column_exists('users', 'yandex_id'):
        conn.execute(text('ALTER TABLE users ADD COLUMN yandex_id VARCHAR(255)'))
        conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_users_yandex_id ON users (yandex_id)'))

    if not _column_exists('users', 'discord_id'):
        conn.execute(text('ALTER TABLE users ADD COLUMN discord_id VARCHAR(255)'))
        conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_users_discord_id ON users (discord_id)'))

    if not _column_exists('users', 'vk_id'):
        conn.execute(text('ALTER TABLE users ADD COLUMN vk_id BIGINT'))
        conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS ix_users_vk_id ON users (vk_id)'))

    # Partner status
    if not _column_exists('users', 'partner_status'):
        conn.execute(text("ALTER TABLE users ADD COLUMN partner_status VARCHAR(20) NOT NULL DEFAULT 'none'"))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_users_partner_status ON users (partner_status)'))


def downgrade() -> None:
    pass
