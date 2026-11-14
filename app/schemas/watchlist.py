"""Watchlist response schemas."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_serializer


class WatchlistItemOut(BaseModel):
    """A single symbol entry on a user's watchlist with live price metadata."""

    symbol: str
    added_at: datetime
    price: Decimal | None = None
    price_age_ms: int | None = None
    stale: bool = True

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("price", mode="plain")
    def serialize_price(self, v: Decimal | str | None) -> str | None:
        """Serialize Decimal as string in JSON responses."""
        if v is None:
            return None
        return f"{v:f}" if isinstance(v, Decimal) else str(v)

