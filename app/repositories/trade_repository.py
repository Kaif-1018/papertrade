"""TradeRepository — DB access for the trades table.

Trade records are **immutable by design**: once a trade is executed and
recorded, it is never updated or deleted.  FIFO PnL, analytics and audit
trails all depend on this immutability.  Accordingly, this repository
exposes only an `add` method — no update, no delete.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trade import Trade
from app.repositories.base import BaseRepository


class TradeRepository(BaseRepository[Trade]):
    """Insert-only repository for executed trades.

    Repositories never commit — the calling service owns the transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Trade, session)

