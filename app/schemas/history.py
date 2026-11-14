"""Trade history pagination and PnL analytics response schemas."""

import base64
from datetime import datetime
from decimal import Decimal
import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_serializer

from app.core.exceptions import InvalidCursorError
from app.schemas.trade import TradeOut


def encode_cursor(executed_at: datetime, trade_id: UUID) -> str:
    """Encode (executed_at, trade_id) into an opaque URL-safe base64 string."""
    payload = {
        "ts": executed_at.isoformat(),
        "id": str(trade_id),
    }
    raw = json.dumps(payload, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("utf-8")


def decode_cursor(cursor_str: str) -> tuple[datetime, UUID]:
    """Decode an opaque URL-safe base64 string into (executed_at, trade_id).

    Raises:
        InvalidCursorError: If the string is malformed or missing expected keys.
    """
    try:
        raw_bytes = base64.urlsafe_b64decode(cursor_str.encode("utf-8"))
        payload = json.loads(raw_bytes.decode("utf-8"))
        ts = datetime.fromisoformat(payload["ts"])
        trade_id = UUID(payload["id"])
        return ts, trade_id
    except Exception as exc:
        raise InvalidCursorError(f"Invalid cursor format: {exc}") from exc


class TradeHistoryOut(BaseModel):
    """Paginated trade history response with keyset cursor."""

    items: list[TradeOut]
    next_cursor: str | None = None
    has_more: bool
    total_count: int | None = None

    model_config = ConfigDict(from_attributes=True)


class MatchOut(BaseModel):
    """A matched closed buy lot against a sell order."""

    sell_trade_id: UUID
    buy_trade_id: UUID
    quantity: Decimal
    buy_price: Decimal
    sell_price: Decimal
    pnl: Decimal

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("quantity", "buy_price", "sell_price", "pnl", mode="plain")
    def serialize_decimals(self, v: Decimal | str) -> str:
        return f"{v:f}" if isinstance(v, Decimal) else str(v)


class SymbolPnlOut(BaseModel):
    """PnL and position analytics for a single symbol."""

    symbol: str
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    total_bought_qty: Decimal
    total_sold_qty: Decimal
    total_buy_value: Decimal
    total_sell_value: Decimal
    open_quantity: Decimal
    current_price: Decimal | None = None
    stale_price: bool = False
    matches: list[MatchOut] | None = None

    model_config = ConfigDict(from_attributes=True)

    @field_serializer(
        "realised_pnl",
        "unrealised_pnl",
        "total_bought_qty",
        "total_sold_qty",
        "total_buy_value",
        "total_sell_value",
        "open_quantity",
        mode="plain",
    )
    def serialize_amounts(self, v: Decimal | str) -> str:
        return f"{v:f}" if isinstance(v, Decimal) else str(v)

    @field_serializer("current_price", mode="plain")
    def serialize_price(self, v: Decimal | str | None) -> str | None:
        if v is None:
            return None
        return f"{v:f}" if isinstance(v, Decimal) else str(v)


class PnlAnalyticsOut(BaseModel):
    """Aggregate portfolio PnL and trading performance analytics."""

    total_realised_pnl: Decimal
    total_unrealised_pnl: Decimal
    total_trades: int
    win_rate: Decimal | None = None
    best_symbol: str | None = None
    worst_symbol: str | None = None
    symbols: list[SymbolPnlOut]
    consistency_warning: str | None = None

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("total_realised_pnl", "total_unrealised_pnl", mode="plain")
    def serialize_totals(self, v: Decimal | str) -> str:
        return f"{v:f}" if isinstance(v, Decimal) else str(v)

    @field_serializer("win_rate", mode="plain")
    def serialize_win_rate(self, v: Decimal | str | None) -> str | None:
        if v is None:
            return None
        return f"{v:f}" if isinstance(v, Decimal) else str(v)

