"""create_admin_tables

Revision ID: 7a8f9c1d2e3b
Revises: 3e31184cd809
Create Date: 2026-11-15 15:30:00.000000

"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '7a8f9c1d2e3b'
down_revision: Union[str, None] = '3e31184cd809'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Default trading pairs to seed
DEFAULT_TRADING_PAIRS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "ADAUSDT",
    "DOGEUSDT",
]


def upgrade() -> None:
    # --------------------------------------------------------- trading_pairs
    trading_pairs_table = op.create_table(
        'trading_pairs',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('symbol', sa.String(length=20), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('added_by', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            'added_at',
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['added_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('symbol'),
    )
    op.create_index(
        'ix_trading_pairs_symbol',
        'trading_pairs',
        ['symbol'],
        unique=True,
    )
    op.create_index(
        'ix_trading_pairs_symbol_active',
        'trading_pairs',
        ['symbol', 'is_active'],
        unique=False,
    )

    # Seed initial trading pairs
    seed_rows = [
        {
            'id': uuid.uuid4(),
            'symbol': symbol,
            'is_active': True,
            'added_by': None,
        }
        for symbol in DEFAULT_TRADING_PAIRS
    ]
    op.bulk_insert(trading_pairs_table, seed_rows)

    # --------------------------------------------------------- portfolio_resets
    op.create_table(
        'portfolio_resets',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('admin_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('reason', sa.String(length=255), nullable=False),
        sa.Column('balance_before', sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column('holdings_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            'reset_at',
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['admin_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_portfolio_resets_user_id',
        'portfolio_resets',
        ['user_id'],
        unique=False,
    )
    op.create_index(
        'ix_portfolio_resets_reset_at',
        'portfolio_resets',
        ['reset_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_portfolio_resets_reset_at', table_name='portfolio_resets')
    op.drop_index('ix_portfolio_resets_user_id', table_name='portfolio_resets')
    op.drop_table('portfolio_resets')

    op.drop_index('ix_trading_pairs_symbol_active', table_name='trading_pairs')
    op.drop_index('ix_trading_pairs_symbol', table_name='trading_pairs')
    op.drop_table('trading_pairs')
