"""add is_main + last_post fields to required_channels; add last_seen_channel_post_id to users

Adds soft-mode nudge columns:
- required_channels.is_main (bool, default false) — marks the single main channel
- required_channels.last_post_message_id, last_post_link, last_post_title, last_post_at
  — cached latest channel post for the nudge card
- users.last_seen_channel_post_id — per-user de-dup: last post id seen in nudge

Revision ID: 9025
Revises: 9024
Create Date: 2026-07-31

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '9025'
down_revision: Union[str, Sequence[str], None] = '9024'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'required_channels',
        sa.Column('is_main', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.add_column(
        'required_channels',
        sa.Column('last_post_message_id', sa.Integer(), nullable=True),
    )
    op.add_column(
        'required_channels',
        sa.Column('last_post_link', sa.String(length=500), nullable=True),
    )
    op.add_column(
        'required_channels',
        sa.Column('last_post_title', sa.String(length=200), nullable=True),
    )
    op.add_column(
        'required_channels',
        sa.Column('last_post_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('last_seen_channel_post_id', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('users', 'last_seen_channel_post_id')
    op.drop_column('required_channels', 'last_post_at')
    op.drop_column('required_channels', 'last_post_title')
    op.drop_column('required_channels', 'last_post_link')
    op.drop_column('required_channels', 'last_post_message_id')
    op.drop_column('required_channels', 'is_main')
