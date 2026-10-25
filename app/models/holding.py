"""Holding model — tracks how much of each symbol a user owns."""

import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, Numeric, String, UniqueConstraint, func
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Holding(Base):
    """One row per (user, symbol) pair representing the current position."""

    __tablename__ = "holdings"
    __table_args__ = (
        UniqueConstraint("user_id", "symbol", name="uq_holdings_user_symbol"),
        CheckConstraint("quantity >= 0", name="ck_holdings_quantity_gte_0"),
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
    # Symbol is always stored upper-cased (enforced at service level).
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    avg_buy_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)

    user: Mapped["User"] = relationship(  # noqa: F821
        "User",
        back_populates="holdings",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return (
            f"<Holding user={self.user_id} symbol={self.symbol!r} qty={self.quantity}>"
        )
