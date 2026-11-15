"""TradingPair model — database source of truth for platform trading pairs."""

from datetime import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, ForeignKey, Index, String, TIMESTAMP, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class TradingPair(Base):
    """Supported trading pair on the platform.
    
    Pairs are never hard-deleted; deactivation is performed via is_active=False
    to preserve historical trade relationships and foreign keys.
    """

    __tablename__ = "trading_pairs"
    __table_args__ = (
        Index("ix_trading_pairs_symbol_active", "symbol", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    symbol: Mapped[str] = mapped_column(
        String(20),
        unique=True,
        index=True,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=sa.text("true"),
        nullable=False,
    )
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    added_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    added_by_user: Mapped["User | None"] = relationship(  # noqa: F821
        "User",
        foreign_keys=[added_by],
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<TradingPair symbol={self.symbol!r} active={self.is_active}>"
