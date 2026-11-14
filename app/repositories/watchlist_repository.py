"""WatchlistRepository — DB access for the watchlist table.

Repositories never commit — the calling service owns the transaction.
"""

from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.watchlist import Watchlist
from app.repositories.base import BaseRepository


class WatchlistRepository(BaseRepository[Watchlist]):
    """Queries scoped to the watchlist table."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Watchlist, session)

    async def get_by_user_and_symbol(
        self, user_id: UUID, symbol: str
    ) -> Watchlist | None:
        """Return the watchlist entry for (user_id, symbol), or None."""
        result = await self._session.execute(
            select(Watchlist).where(
                Watchlist.user_id == user_id,
                Watchlist.symbol == symbol,
            )
        )
        return result.scalar_one_or_none()

    async def count_by_user(self, user_id: UUID) -> int:
        """Return the total number of watchlist items for *user_id*."""
        result = await self._session.execute(
            select(func.count(Watchlist.id)).where(Watchlist.user_id == user_id)
        )
        return result.scalar() or 0

    async def list_by_user(self, user_id: UUID) -> list[Watchlist]:
        """Return all watchlist entries for *user_id* ordered by added_at ascending."""
        result = await self._session.execute(
            select(Watchlist)
            .where(Watchlist.user_id == user_id)
            .order_by(Watchlist.added_at.asc())
        )
        return list(result.scalars().all())

    async def insert_on_conflict_do_nothing(
        self, user_id: UUID, symbol: str
    ) -> tuple[Watchlist, bool]:
        """Insert a (user_id, symbol) entry using ON CONFLICT DO NOTHING.

        Returns:
            (Watchlist, created: bool)
            - If inserted: (created_item, True)
            - If conflict: (existing_item, False)
        """
        stmt = (
            pg_insert(Watchlist)
            .values(
                id=uuid4(),
                user_id=user_id,
                symbol=symbol,
            )
            .on_conflict_do_nothing(
                constraint="uq_watchlist_user_symbol"
            )
            .returning(Watchlist)
        )
        result = await self._session.execute(stmt)
        inserted = result.scalar_one_or_none()
        if inserted is not None:
            return inserted, True

        # Existing row conflict — fetch the existing record
        existing = await self.get_by_user_and_symbol(user_id, symbol)
        if existing is None:
            raise RuntimeError(
                f"Failed to find existing watchlist entry for {user_id}:{symbol} after conflict"
            )
        return existing, False

    async def delete_by_user_and_symbol(self, user_id: UUID, symbol: str) -> bool:
        """Delete the entry for (user_id, symbol).

        Returns True if a row was deleted, False if not found.
        """
        result = await self._session.execute(
            delete(Watchlist).where(
                Watchlist.user_id == user_id,
                Watchlist.symbol == symbol,
            )
        )
        return (result.rowcount or 0) > 0

