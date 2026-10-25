"""Declarative base shared by all SQLAlchemy models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Single source-of-truth for all model metadata.

    Every model must inherit from this class so that Alembic autogenerate
    can discover the full schema in one place.
    """
