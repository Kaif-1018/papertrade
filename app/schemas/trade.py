"""Trade request/response schemas.

Decimal handling:
- Quantities and prices are serialized as JSON strings to preserve precision.
- TradeRequest accepts quantity as either a JSON string ("0.005") or number (0.005).
  Both paths route through Decimal(str(v)) to avoid float rounding. Pydantic's native
  Decimal coercion goes through float internally, so a before-mode validator intercepts
  first and converts via string.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from app.core.config import get_settings

# 8 decimal places — matches Numeric(18, 8) columns in the DB.
QUANT = Decimal("0.00000001")

# Maximum decimal places allowed in a quantity input.
MAX_QUANTITY_DP = 8

# Reject absurdly large orders.
MAX_QUANTITY = Decimal("1000000")


def quantize(value: Decimal, rounding: str = ROUND_HALF_UP) -> Decimal:
    """Quantize *value* to 8 decimal places.

    Rounding choice: we use ROUND_HALF_UP for total_value (price × quantity)
    because this is the cost charged/credited — not a user credit. The CLAUDE.md
    rule "ROUND_DOWN on credits to the user" applies to credits like sell proceeds;
    here we compute the exact trade total first, then the service decides rounding
    direction for each side (debit vs. credit) at the balance level.
    """
    return value.quantize(QUANT, rounding=rounding)


class TradeRequest(BaseModel):
    """Incoming buy/sell request.

    symbol: uppercased, must be in TRADING_PAIRS.
    quantity: >0, ≤1_000_000, at most 8 decimal places.
    """

    symbol: str
    quantity: Decimal

    @field_validator("symbol", mode="before")
    @classmethod
    def uppercase_symbol(cls, v: object) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("symbol must be a non-empty string")
        return v.strip().upper()

    @field_validator("symbol", mode="after")
    @classmethod
    def validate_supported(cls, v: str) -> str:
        settings = get_settings()
        if v not in settings.trading_pairs_list:
            raise ValueError(
                f"Symbol {v} is not supported. "
                f"Supported: {', '.join(settings.trading_pairs_list)}"
            )
        return v

    @field_validator("quantity", mode="before")
    @classmethod
    def parse_quantity_no_float(cls, v: object) -> Decimal:
        """Convert to Decimal via string to avoid float rounding.

        JSON numbers arrive as float in Python. We convert float -> str -> Decimal
        to capture the user's intended value. For example, JSON 0.1 becomes
        Decimal('0.1') not Decimal('0.1000000000000000055511151231257827021181583404541015625').
        """
        if isinstance(v, Decimal):
            return v
        if isinstance(v, (int, float, str)):
            try:
                return Decimal(str(v))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError(f"Invalid quantity: {exc}") from exc
        raise ValueError(f"quantity must be a number or numeric string, got {type(v).__name__}")

    @field_validator("quantity", mode="after")
    @classmethod
    def validate_quantity(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("quantity must be greater than 0")
        if v > MAX_QUANTITY:
            raise ValueError(f"quantity must not exceed {MAX_QUANTITY}")
        # Check decimal places: quantize and compare
        sign, digits, exponent = v.as_tuple()
        if exponent != "n" and isinstance(exponent, int) and abs(exponent) > MAX_QUANTITY_DP:
            raise ValueError(f"quantity must have at most {MAX_QUANTITY_DP} decimal places")
        return v


class TradeOut(BaseModel):
    """Single trade record in API responses."""

    id: UUID
    symbol: str
    trade_type: str
    quantity: Decimal
    price: Decimal
    total_value: Decimal
    executed_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("quantity", "price", "total_value", mode="plain")
    def serialize_decimal(self, v: Decimal | str) -> str:
        return str(v)

    @field_validator("trade_type", mode="before")
    @classmethod
    def coerce_trade_type(cls, v: object) -> str:
        """Handle TradeType enum or raw string."""
        return str(v.value) if hasattr(v, "value") else str(v)


class TradeResult(BaseModel):
    """Response payload after a successful trade execution."""

    trade: TradeOut
    balance: Decimal

    @field_serializer("balance", mode="plain")
    def serialize_balance(self, v: Decimal | str) -> str:
        return str(v)

