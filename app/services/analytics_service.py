"""AnalyticsService orchestrating trade history pagination and portfolio PnL analysis."""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.core.exceptions import InvalidDateRangeError
from app.core.price_registry import get_price_safe
from app.repositories.holding_repository import HoldingRepository
from app.repositories.trade_repository import TradeRepository
from app.schemas.history import (
    MatchOut,
    PnlAnalyticsOut,
    SymbolPnlOut,
    TradeHistoryOut,
    decode_cursor,
    encode_cursor,
)
from app.schemas.trade import TradeOut
from app.services.pnl import Match, calculate_fifo_pnl, quantize_8

logger = structlog.stdlib.get_logger(__name__)

QUANT_4 = Decimal("0.0001")


def quantize_4(value: Decimal) -> Decimal:
    """Quantize percentage or ratio to 4 decimal places."""
    return value.quantize(QUANT_4, rounding=ROUND_HALF_UP)


class AnalyticsService:
    """Service providing keyset-paginated trade history and FIFO realised PnL analytics."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._trades = TradeRepository(session)
        self._holdings = HoldingRepository(session)

    async def get_trade_history(
        self,
        user_id: UUID,
        symbol: str | None = None,
        trade_type: str | None = None,
        from_: datetime | None = None,
        to_: datetime | None = None,
        limit: int = 20,
        cursor: str | None = None,
        include_total: bool = False,
    ) -> TradeHistoryOut:
        """Retrieve paginated trade history using stable keyset pagination.

        Raises:
            InvalidDateRangeError (422): If from_ > to_.
            InvalidCursorError (422): If cursor cannot be decoded.
        """
        if from_ is not None and to_ is not None and from_ > to_:
            raise InvalidDateRangeError(
                "Date range is invalid: 'from' must be earlier than or equal to 'to'"
            )

        cursor_dt, cursor_id = decode_cursor(cursor) if cursor else (None, None)

        rows = await self._trades.list_trades_keyset(
            user_id=user_id,
            symbol=symbol,
            trade_type=trade_type,
            from_=from_,
            to_=to_,
            cursor_dt=cursor_dt,
            cursor_id=cursor_id,
            limit=limit,
        )

        has_more = len(rows) > limit
        items = rows[:limit]

        next_cursor = (
            encode_cursor(items[-1].executed_at, items[-1].id)
            if has_more and items
            else None
        )

        total_count = None
        if include_total:
            total_count = await self._trades.count_trades(
                user_id=user_id,
                symbol=symbol,
                trade_type=trade_type,
                from_=from_,
                to_=to_,
            )

        trade_outs = [TradeOut.model_validate(trade) for trade in items]

        return TradeHistoryOut(
            items=trade_outs,
            next_cursor=next_cursor,
            has_more=has_more,
            total_count=total_count,
        )

    async def get_pnl_analytics(
        self,
        user_id: UUID,
        symbol: str | None = None,
        include_matches: bool = False,
    ) -> PnlAnalyticsOut:
        """Compute FIFO realised and unrealised PnL across user trades."""
        norm_symbol = symbol.strip().upper() if symbol else None

        # 1. Fetch all user trades chronologically
        trades = await self._trades.list_chronological_trades(
            user_id=user_id,
            symbol=norm_symbol,
        )

        # 2. Fetch database holdings for sanity check
        db_holdings = await self._holdings.list_by_user(user_id)
        db_holdings_map = {h.symbol: h.quantity for h in db_holdings}

        # 3. Group trades by symbol
        trades_by_symbol: dict[str, list] = defaultdict(list)
        for trade in trades:
            trades_by_symbol[trade.symbol].append(trade)

        if norm_symbol and norm_symbol not in trades_by_symbol:
            trades_by_symbol[norm_symbol] = []

        # 4. Compute FIFO PnL per symbol
        total_realised_pnl = Decimal("0")
        total_unrealised_pnl = Decimal("0")
        all_closed_matches: list[Match] = []
        symbol_results: list[SymbolPnlOut] = []
        consistency_warnings: list[str] = []

        for sym, sym_trades in sorted(trades_by_symbol.items()):
            pnl_res = calculate_fifo_pnl(sym_trades, sym)

            # Sanity check: FIFO open quantity must equal holding row in DB
            db_qty = db_holdings_map.get(sym, Decimal("0"))
            if pnl_res.open_quantity != db_qty:
                msg = (
                    f"Holding quantity mismatch for {sym}: "
                    f"FIFO open quantity is {pnl_res.open_quantity}, "
                    f"database holding is {db_qty}"
                )
                logger.error(
                    "pnl_holding_quantity_mismatch",
                    extra={"user_id": str(user_id), "symbol": sym, "detail": msg},
                )
                consistency_warnings.append(msg)

            # Unrealised PnL calculation using live registry
            current_price = None
            stale_price = False
            unrealised_pnl = Decimal("0.00000000")

            if pnl_res.open_quantity > Decimal("0"):
                price = get_price_safe(sym)
                if price is not None:
                    current_price = price
                    stale_price = False
                    market_val = pnl_res.open_quantity * current_price
                    cost_basis = sum(lot.quantity * lot.price for lot in pnl_res.open_lots)
                    unrealised_pnl = quantize_8(market_val - cost_basis)
                else:
                    # Stale or unavailable price fallback: value at cost basis
                    current_price = None
                    stale_price = True
                    unrealised_pnl = Decimal("0.00000000")

            total_realised_pnl += pnl_res.realised_pnl
            total_unrealised_pnl += unrealised_pnl
            all_closed_matches.extend(pnl_res.closed_matches)

            matches_out = None
            if include_matches:
                matches_out = [
                    MatchOut(
                        sell_trade_id=m.sell_trade_id,
                        buy_trade_id=m.buy_trade_id,
                        quantity=m.quantity,
                        buy_price=m.buy_price,
                        sell_price=m.sell_price,
                        pnl=m.pnl,
                    )
                    for m in pnl_res.closed_matches
                ]

            symbol_results.append(
                SymbolPnlOut(
                    symbol=sym,
                    realised_pnl=pnl_res.realised_pnl,
                    unrealised_pnl=unrealised_pnl,
                    total_bought_qty=pnl_res.total_bought_qty,
                    total_sold_qty=pnl_res.total_sold_qty,
                    total_buy_value=pnl_res.total_buy_value,
                    total_sell_value=pnl_res.total_sell_value,
                    open_quantity=pnl_res.open_quantity,
                    current_price=current_price,
                    stale_price=stale_price,
                    matches=matches_out,
                )
            )

        # 5. Overall performance metrics
        winning_matches = sum(1 for m in all_closed_matches if m.pnl > Decimal("0"))
        total_matches = len(all_closed_matches)
        win_rate = (
            quantize_4(Decimal(winning_matches) / Decimal(total_matches) * Decimal("100"))
            if total_matches > 0
            else None
        )

        best_symbol = None
        worst_symbol = None
        if symbol_results:
            best_symbol = max(symbol_results, key=lambda s: s.realised_pnl).symbol
            worst_symbol = min(symbol_results, key=lambda s: s.realised_pnl).symbol

        consistency_warning = "; ".join(consistency_warnings) if consistency_warnings else None

        return PnlAnalyticsOut(
            total_realised_pnl=quantize_8(total_realised_pnl),
            total_unrealised_pnl=quantize_8(total_unrealised_pnl),
            total_trades=len(trades),
            win_rate=win_rate,
            best_symbol=best_symbol,
            worst_symbol=worst_symbol,
            symbols=symbol_results,
            consistency_warning=consistency_warning,
        )

