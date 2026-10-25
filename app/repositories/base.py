"""Generic async repository providing common DB operations.

Repositories are the *only* layer that touches the session directly.
They never commit — the calling service owns the transaction.
"""

from typing import Generic, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """CRUD helpers that every concrete repository inherits."""

    def __init__(self, model: type[ModelT], session: AsyncSession) -> None:
        self._model = model
        self._session = session

    async def get_by_id(self, id: UUID) -> ModelT | None:
        """Return the row with *id*, or None if not found."""
        result = await self._session.execute(
            select(self._model).where(self._model.id == id)  # type: ignore[attr-defined]
        )
        return result.scalar_one_or_none()

    def add(self, instance: ModelT) -> None:
        """Stage *instance* for INSERT — does not flush or commit."""
        self._session.add(instance)

    async def delete(self, instance: ModelT) -> None:
        """Stage *instance* for DELETE — does not flush or commit."""
        await self._session.delete(instance)
