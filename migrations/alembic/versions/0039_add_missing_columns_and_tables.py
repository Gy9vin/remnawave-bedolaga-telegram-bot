"""Add missing columns and tables (auth_type, auto_renewed_before_expiry, payment_method_configs)

Revision ID: 0039
Revises: 0038
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect, text

revision: str = '0039'
down_revision: Union[str, None] = '0038'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    cols = [c['name'] for c in insp.get_columns(table_name)]
    return column_name in cols


def _table_exists(table_name: str) -> bool:
    conn = op.get_bind()
    insp = inspect(conn)
    return insp.has_table(table_name)


def upgrade() -> None:
    conn = op.get_bind()

    # 1. users.auth_type
    if not _column_exists('users', 'auth_type'):
        conn.execute(text("ALTER TABLE users ADD COLUMN auth_type VARCHAR(20) NOT NULL DEFAULT 'telegram'"))

    # 2. users.telegram_id — make nullable for email-only users
    # Check via information_schema (PostgreSQL only)
    try:
        result = conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = 'users' AND column_name = 'telegram_id'"
            )
        )
        row = result.fetchone()
        if row and row[0] == 'NO':
            conn.execute(text('ALTER TABLE users ALTER COLUMN telegram_id DROP NOT NULL'))
    except Exception:
        pass  # SQLite or other DB — skip

    # 3. subscriptions.auto_renewed_before_expiry
    if not _column_exists('subscriptions', 'auto_renewed_before_expiry'):
        conn.execute(
            text('ALTER TABLE subscriptions ADD COLUMN auto_renewed_before_expiry BOOLEAN NOT NULL DEFAULT FALSE')
        )

    # 4. payment_method_configs table (must be before payment_method_promo_groups due to FK)
    if not _table_exists('payment_method_configs'):
        op.create_table(
            'payment_method_configs',
            sa.Column('id', sa.Integer(), primary_key=True, index=True),
            sa.Column('method_id', sa.String(50), nullable=False, unique=True, index=True),
            sa.Column('sort_order', sa.Integer(), nullable=False, default=0, index=True),
            sa.Column('is_enabled', sa.Boolean(), nullable=False, default=True),
            sa.Column('display_name', sa.String(255), nullable=True),
            sa.Column('sub_options', sa.JSON(), nullable=True),
            sa.Column('min_amount_kopeks', sa.Integer(), nullable=True),
            sa.Column('max_amount_kopeks', sa.Integer(), nullable=True),
            sa.Column('user_type_filter', sa.String(20), nullable=False, server_default='all'),
            sa.Column('first_topup_filter', sa.String(10), nullable=False, server_default='any'),
            sa.Column('promo_group_filter_mode', sa.String(20), nullable=False, server_default='all'),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column(
                'updated_at',
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                onupdate=sa.func.now(),
            ),
        )

    # 5. payment_method_promo_groups association table
    if not _table_exists('payment_method_promo_groups'):
        op.create_table(
            'payment_method_promo_groups',
            sa.Column(
                'payment_method_config_id',
                sa.Integer(),
                sa.ForeignKey('payment_method_configs.id', ondelete='CASCADE'),
                primary_key=True,
            ),
            sa.Column(
                'promo_group_id',
                sa.Integer(),
                sa.ForeignKey('promo_groups.id', ondelete='CASCADE'),
                primary_key=True,
            ),
        )


def downgrade() -> None:
    pass
