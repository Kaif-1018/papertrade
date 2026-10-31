"""Price response schemas."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_serializer


class PriceOut(BaseModel):
    """Current price payload for a trading symbol."""

    symbol: str
    price: Decimal
    event_time: datetime
    age_ms: int

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("price", mode="plain")
    def serialize_price(self, v: Decimal | str) -> str:
        """Serialize Decimal as string in JSON responses."""
        return str(v)

