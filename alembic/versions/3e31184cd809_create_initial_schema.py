"""create_initial_schema

Revision ID: 3e31184cd809
Revises: 
Create Date: 2026-10-25 17:15:51.147972

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '3e31184cd809'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------ enums
    # Raw DDL explicitly creates the native PostgreSQL enum types.
    op.execute("CREATE TYPE userrole AS ENUM ('USER', 'ADMIN')")
    op.execute("CREATE TYPE tradetype AS ENUM ('BUY', 'SELL')")

    # Column references specify create_type=False and _create_events=False
    # so SQLAlchemy does not auto-emit CREATE TYPE or attach DDL event hooks.
    user_role_enum = postgresql.ENUM(
        'USER', 'ADMIN', name='userrole', create_type=False, _create_events=False
    )
    trade_type_enum = postgresql.ENUM(
        'BUY', 'SELL', name='tradetype', create_type=False, _create_events=False
    )

    # --------------------------------------------------------- price_snapshots
    op.create_table(
        'price_snapshots',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('symbol', sa.String(length=20), nullable=False),
        sa.Column('price', sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column(
            'recorded_at', sa.TIMESTAMP(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_price_snapshots_symbol_recorded_at',
        'price_snapshots', ['symbol', sa.text('recorded_at DESC')], unique=False,
    )

    # --------------------------------------------------------------- users
    op.create_table(
        'users',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('role', user_role_enum, server_default='USER', nullable=False),
        sa.Column('virtual_balance', sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column('initial_balance', sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column(
            'created_at', sa.TIMESTAMP(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.CheckConstraint('virtual_balance >= 0', name='ck_users_virtual_balance_gte_0'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
    )
    op.create_index('ix_users_email', 'users', ['email'], unique=True)

    # ------------------------------------------------------------- holdings
    op.create_table(
        'holdings',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('symbol', sa.String(length=20), nullable=False),
        sa.Column('quantity', sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column('avg_buy_price', sa.Numeric(precision=18, scale=8), nullable=False),
        sa.CheckConstraint('quantity >= 0', name='ck_holdings_quantity_gte_0'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'symbol', name='uq_holdings_user_symbol'),
    )

    # -------------------------------------------------------------- trades
    op.create_table(
        'trades',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('symbol', sa.String(length=20), nullable=False),
        sa.Column('trade_type', trade_type_enum, nullable=False),
        sa.Column('quantity', sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column('price', sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column('total_value', sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column(
            'executed_at', sa.TIMESTAMP(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.CheckConstraint('price > 0', name='ck_trades_price_gt_0'),
        sa.CheckConstraint('quantity > 0', name='ck_trades_quantity_gt_0'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_trades_user_executed_at',
        'trades', ['user_id', sa.text('executed_at DESC')], unique=False,
    )
    op.create_index('ix_trades_user_symbol', 'trades', ['user_id', 'symbol'], unique=False)

    # ----------------------------------------------------------- watchlist
    op.create_table(
        'watchlist',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('symbol', sa.String(length=20), nullable=False),
        sa.Column(
            'added_at', sa.TIMESTAMP(timezone=True),
            server_default=sa.text('now()'), nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'symbol', name='uq_watchlist_user_symbol'),
    )


def downgrade() -> None:
    # Drop tables in reverse FK dependency order.
    op.drop_table('watchlist')
    op.drop_index('ix_trades_user_symbol', table_name='trades')
    op.drop_index('ix_trades_user_executed_at', table_name='trades')
    op.drop_table('trades')
    op.drop_table('holdings')
    op.drop_index('ix_users_email', table_name='users')
    op.drop_table('users')
    op.drop_index('ix_price_snapshots_symbol_recorded_at', table_name='price_snapshots')
    op.drop_table('price_snapshots')

    # Drop native PG enums — must be explicit, autogenerate omits this.
    op.execute('DROP TYPE IF EXISTS tradetype')
    op.execute('DROP TYPE IF EXISTS userrole')
