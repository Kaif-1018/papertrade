"""HoldingRepository — DB access for the holdings table.

Repositories never commit — the calling service owns the transaction.
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.holding import Holding
from app.repositories.base import BaseRepository


class HoldingRepository(BaseRepository[Holding]):
    """Queries scoped to the holdings table.

    Provides locking reads (FOR UPDATE) for safe concurrent trade execution.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Holding, session)

    async def get_for_update(self, user_id: UUID, symbol: str) -> Holding | None:
        """SELECT ... FOR UPDATE on the (user_id, symbol) holding.

        Acquires a row-level lock so no other transaction can modify or read
        this holding with FOR UPDATE until we commit or rollback.
        """
        result = await self._session.execute(
            select(Holding)
            .where(Holding.user_id == user_id, Holding.symbol == symbol)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def list_by_user(self, user_id: UUID) -> list[Holding]:
        """Fetch all holdings for a user without row locks.

        Read-only query: used by portfolio valuation and queries that do
        not mutate balance or holdings.
        """
        result = await self._session.execute(
            select(Holding)
            .where(Holding.user_id == user_id)
            .order_by(Holding.symbol)
        )
        return list(result.scalars().all())

    def create(
        self,
        user_id: UUID,
        symbol: str,
        quantity: Decimal,
        avg_buy_price: Decimal,
    ) -> Holding:
        """Stage a new Holding for INSERT (does not commit)."""
        holding = Holding(
            user_id=user_id,
            symbol=symbol,
            quantity=quantity,
            avg_buy_price=avg_buy_price,
        )
        self.add(holding)
        return holding

    async def delete(self, holding: Holding) -> None:
        """Stage *holding* for DELETE (does not commit)."""
        await self._session.delete(holding)


