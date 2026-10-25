"""Trade model — immutable record of every executed buy or sell."""

import enum
import uuid
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, TIMESTAMP, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class TradeType(str, enum.Enum):
    """Native Postgres enum for trade direction."""

    BUY = "BUY"
    SELL = "SELL"


# create_type=False: migration owns CREATE/DROP TYPE, ORM never fires it.
_tradetype_sa_type = sa.Enum(TradeType, name="tradetype", create_type=False)
# Belt-and-suspenders: suppress before_create/after_create DDL event hooks.
# create_type=False alone does not set _create_events=False in SA 2.0.35.
_tradetype_sa_type._create_events = False


class Trade(Base):
    """An immutable record of a single executed trade.

    Records are never updated or deleted — FIFO PnL is computed from history.
    """

    __tablename__ = "trades"
    __table_args__ = (
        # Fast lookup of a user's full history sorted by time descending.
        Index("ix_trades_user_executed_at", "user_id", sa.text("executed_at DESC")),
        # Fast lookup of a user's trades for a specific symbol (PnL, analytics).
        Index("ix_trades_user_symbol", "user_id", "symbol"),
        CheckConstraint("quantity > 0", name="ck_trades_quantity_gt_0"),
        CheckConstraint("price > 0", name="ck_trades_price_gt_0"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    trade_type: Mapped[TradeType] = mapped_column(_tradetype_sa_type, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    total_value: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    user: Mapped["User"] = relationship(  # noqa: F821
        "User",
        back_populates="trades",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return (
            f"<Trade id={self.id} {self.trade_type} "
            f"{self.quantity} {self.symbol!r} @ {self.price}>"
        )
