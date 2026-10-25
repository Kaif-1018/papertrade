"""Shared FastAPI dependencies.

Re-exports here so routers import from a single place.
"""

from app.core.database import get_db

__all__ = ["get_db"]
