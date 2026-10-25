"""PriceSnapshot model — periodic price captures for analytics / charts."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Index, Numeric, String, TIMESTAMP, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PriceSnapshot(Base):
    """A point-in-time price record written by the feed ingestion task.

    Used for sparklines, analytics, and audit purposes.  Live prices
    come from the in-memory price registry, not this table.
    """

    __tablename__ = "price_snapshots"
    __table_args__ = (
        # Most queries filter by symbol then order by time descending.
        Index("ix_price_snapshots_symbol_recorded_at", "symbol", text("recorded_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<PriceSnapshot symbol={self.symbol!r} price={self.price} at={self.recorded_at}>"
