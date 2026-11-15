"""PortfolioReset model — audit record for administrative user portfolio resets."""

from datetime import datetime
from decimal import Decimal
from typing import Any
import uuid

from sqlalchemy import ForeignKey, Numeric, String, TIMESTAMP, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class PortfolioReset(Base):
    """Audit log of an administrative portfolio reset.
    
    When an admin resets a user:
    - User cash balance is restored to initial_balance.
    - All existing user holdings are deleted.
    - An immutable snapshot of previous balance and holdings is saved here.
    - Past trades remain in the trades table for compliance and audit history.
    """

    __tablename__ = "portfolio_resets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    balance_before: Mapped[Decimal] = mapped_column(
        Numeric(18, 8),
        nullable=False,
    )
    holdings_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
    )
    reset_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    user: Mapped["User"] = relationship(  # noqa: F821
        "User",
        foreign_keys=[user_id],
        lazy="raise",
    )
    admin: Mapped["User | None"] = relationship(  # noqa: F821
        "User",
        foreign_keys=[admin_id],
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<PortfolioReset user_id={self.user_id} reset_at={self.reset_at}>"
