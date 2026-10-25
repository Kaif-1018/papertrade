"""User model."""

import enum
import uuid
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import (
    CheckConstraint,
    Index,
    Numeric,
    String,
    TIMESTAMP,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class UserRole(str, enum.Enum):
    """Native Postgres enum for user roles."""

    USER = "USER"
    ADMIN = "ADMIN"


# SQLAlchemy type object shared by the model column and Alembic migrations.
# create_type=False: the migration owns CREATE/DROP TYPE — the ORM never fires it.
_userrole_sa_type = sa.Enum(
    UserRole, name="userrole", create_type=False
)
# Belt-and-suspenders: suppress before_create/after_create DDL event hooks.
# create_type=False alone does not set _create_events=False in SA 2.0.35.
_userrole_sa_type._create_events = False


class User(Base):
    """Platform user with a virtual USD balance."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("virtual_balance >= 0", name="ck_users_virtual_balance_gte_0"),
        Index("ix_users_email", "email", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        _userrole_sa_type,
        default=UserRole.USER,
        server_default=UserRole.USER.value,
        nullable=False,
    )
    # Never Float — exact decimal math throughout.
    virtual_balance: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False
    )
    initial_balance: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # Relationships — lazy="raise" prevents accidental async lazy-loads.
    holdings: Mapped[list["Holding"]] = relationship(  # noqa: F821
        "Holding",
        back_populates="user",
        lazy="raise",
        cascade="all, delete-orphan",
    )
    trades: Mapped[list["Trade"]] = relationship(  # noqa: F821
        "Trade",
        back_populates="user",
        lazy="raise",
        cascade="all, delete-orphan",
    )
    watchlist: Mapped[list["Watchlist"]] = relationship(  # noqa: F821
        "Watchlist",
        back_populates="user",
        lazy="raise",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r} role={self.role}>"
