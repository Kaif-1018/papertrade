"""Repository for TradingPair models."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trading_pair import TradingPair


class TradingPairRepository:
    """Encapsulates database access for trading pairs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_symbol(self, symbol: str) -> TradingPair | None:
        """Fetch a trading pair by normalized uppercase symbol."""
        stmt = select(TradingPair).where(TradingPair.symbol == symbol.strip().upper())
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self) -> Sequence[TradingPair]:
        """Fetch all trading pairs ordered alphabetically."""
        stmt = select(TradingPair).order_by(TradingPair.symbol.asc())
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def list_active(self) -> Sequence[TradingPair]:
        """Fetch all currently active trading pairs."""
        stmt = (
            select(TradingPair)
            .where(TradingPair.is_active == True)  # noqa: E712
            .order_by(TradingPair.symbol.asc())
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def create(
        self,
        symbol: str,
        added_by: UUID | None = None,
        is_active: bool = True,
    ) -> TradingPair:
        """Create and persist a new trading pair row."""
        pair = TradingPair(
            symbol=symbol.strip().upper(),
            added_by=added_by,
            is_active=is_active,
        )
        self._session.add(pair)
        await self._session.flush()
        return pair

    async def set_active_status(
        self,
        symbol: str,
        is_active: bool,
        added_by: UUID | None = None,
    ) -> TradingPair | None:
        """Update is_active status of an existing trading pair."""
        pair = await self.get_by_symbol(symbol)
        if pair is None:
            return None
        pair.is_active = is_active
        if added_by is not None:
            pair.added_by = added_by
        await self._session.flush()
        return pair

    async def seed_missing_pairs(self, symbols: list[str]) -> list[TradingPair]:
        """Insert default pairs that do not already exist in the database."""
        existing = await self.list_all()
        existing_symbols = {p.symbol for p in existing}

        created: list[TradingPair] = []
        for s in symbols:
            norm = s.strip().upper()
            if norm not in existing_symbols:
                pair = TradingPair(symbol=norm, is_active=True, added_by=None)
                self._session.add(pair)
                created.append(pair)
                existing_symbols.add(norm)

        if created:
            await self._session.flush()
        return created
