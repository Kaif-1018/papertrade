"""FIFO Realised PnL Engine.

Pure mathematical FIFO (First-In, First-Out) matching algorithm with zero database
dependencies and zero I/O. Computes exact realised profit-and-loss and open inventory
lots for a single trading symbol.

Rounding Choice & Precision:
----------------------------
All calculations are performed using Python's `decimal.Decimal` with full precision.
PnL is accumulated without intermediate rounding per step and quantized to 8 decimal
places strictly at the end. This prevents fractional rounding errors from compounding
across high-frequency trade slices.

Worked Example:
---------------
1. BUY  1.00000000 @ $100.00 -> Lot 1: (qty=1.0, price=100.0)
2. BUY  1.00000000 @ $110.00 -> Lot 2: (qty=1.0, price=110.0)
3. SELL 1.50000000 @ $120.00:
   - Match 1: Consumes 1.0 from Lot 1 @ $100.
     PnL = (120.00 - 100.00) * 1.0 = +$20.00
   - Match 2: Consumes 0.5 from Lot 2 @ $110.
     PnL = (120.00 - 110.00) * 0.5 = +$5.00
   - Remaining in Lot 2: 0.50000000 @ $110.00.
   - Total Realised PnL = $20.00 + $5.00 = +$25.00.
   - Remaining Open Quantity = 0.50000000.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

# 8 decimal places for exact cryptocurrency asset accounting
QUANT_8 = Decimal("0.00000001")


def quantize_8(value: Decimal) -> Decimal:
    """Quantize *value* to 8 decimal places using ROUND_HALF_UP."""
    return value.quantize(QUANT_8, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class TradeRecord:
    """Generic trade representation decoupled from ORM models."""

    id: UUID
    symbol: str
    trade_type: str  # "BUY" or "SELL"
    quantity: Decimal
    price: Decimal
    executed_at: datetime


@dataclass
class Lot:
    """An open buy lot available to match against future sells."""

    trade_id: UUID
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class Match:
    """An executed sell consumption of a specific buy lot."""

    sell_trade_id: UUID
    buy_trade_id: UUID
    quantity: Decimal
    buy_price: Decimal
    sell_price: Decimal
    pnl: Decimal


@dataclass
class SymbolPnl:
    """Realised PnL summary and open lots for a single symbol."""

    symbol: str
    realised_pnl: Decimal
    total_bought_qty: Decimal
    total_sold_qty: Decimal
    total_buy_value: Decimal
    total_sell_value: Decimal
    open_lots: list[Lot]
    open_quantity: Decimal
    closed_matches: list[Match]


def calculate_fifo_pnl(trades: list[TradeRecord], symbol: str) -> SymbolPnl:
    """Compute FIFO realised PnL for *symbol* over chronological trade records.

    Args:
        trades: List of TradeRecord instances for *symbol*.
        symbol: Target trading pair (e.g. "BTCUSDT").

    Returns:
        SymbolPnl dataclass containing aggregate totals, open lots, and matched pairs.

    Raises:
        ValueError: If a SELL trade exceeds available open BUY lots (data corruption guard).
    """
    norm_symbol = symbol.strip().upper()

    # Filter to target symbol and sort chronologically, breaking ties by UUID id
    filtered_trades = [
        t for t in trades if getattr(t, "symbol", "").strip().upper() == norm_symbol
    ]
    sorted_trades = sorted(
        filtered_trades,
        key=lambda t: (t.executed_at, t.id),
    )

    open_lots: deque[Lot] = deque()
    closed_matches: list[Match] = []

    total_bought_qty = Decimal("0")
    total_sold_qty = Decimal("0")
    total_buy_value = Decimal("0")
    total_sell_value = Decimal("0")
    unquantized_realised_pnl = Decimal("0")

    for trade in sorted_trades:
        t_type = (
            trade.trade_type.value
            if hasattr(trade.trade_type, "value")
            else str(trade.trade_type)
        ).upper()

        qty = Decimal(str(trade.quantity))
        price = Decimal(str(trade.price))

        if t_type == "BUY":
            open_lots.append(
                Lot(
                    trade_id=trade.id,
                    quantity=qty,
                    price=price,
                )
            )
            total_bought_qty += qty
            total_buy_value += qty * price

        elif t_type == "SELL":
            qty_remaining_to_sell = qty
            total_sold_qty += qty
            total_sell_value += qty * price

            while qty_remaining_to_sell > Decimal("0"):
                if not open_lots:
                    raise ValueError(
                        f"Data corruption guard: SELL trade {trade.id} requires "
                        f"{qty_remaining_to_sell} {norm_symbol}, but no open BUY lots remain."
                    )

                lot = open_lots[0]
                match_qty = min(lot.quantity, qty_remaining_to_sell)
                match_pnl = (price - lot.price) * match_qty

                unquantized_realised_pnl += match_pnl
                closed_matches.append(
                    Match(
                        sell_trade_id=trade.id,
                        buy_trade_id=lot.trade_id,
                        quantity=match_qty,
                        buy_price=lot.price,
                        sell_price=price,
                        pnl=quantize_8(match_pnl),
                    )
                )

                lot.quantity -= match_qty
                qty_remaining_to_sell -= match_qty

                if lot.quantity == Decimal("0"):
                    open_lots.popleft()

        else:
            raise ValueError(f"Unknown trade type: {trade.trade_type}")

    open_lots_list = list(open_lots)
    open_quantity = sum((lot.quantity for lot in open_lots_list), Decimal("0"))

    return SymbolPnl(
        symbol=norm_symbol,
        realised_pnl=quantize_8(unquantized_realised_pnl),
        total_bought_qty=quantize_8(total_bought_qty),
        total_sold_qty=quantize_8(total_sold_qty),
        total_buy_value=quantize_8(total_buy_value),
        total_sell_value=quantize_8(total_sell_value),
        open_lots=open_lots_list,
        open_quantity=quantize_8(open_quantity),
        closed_matches=closed_matches,
    )

