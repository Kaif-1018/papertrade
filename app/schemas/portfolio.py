"""Portfolio valuation schemas.

Serializes all Decimal amounts (quantized to 8 decimal places) and percentages
(quantized to 4 decimal places) as strings in API responses to prevent float rounding.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_serializer

QUANT_AMOUNT = Decimal("0.00000001")
QUANT_PERCENT = Decimal("0.0001")


class PortfolioHoldingOut(BaseModel):
    """Valuation details for a single asset holding."""

    symbol: str
    quantity: Decimal
    avg_buy_price: Decimal
    current_price: Decimal | None
    market_value: Decimal
    cost_basis: Decimal
    unrealised_pnl: Decimal
    pnl_percent: Decimal | None
    stale: bool

    model_config = ConfigDict(from_attributes=True)

    @field_serializer(
        "quantity",
        "avg_buy_price",
        "current_price",
        "market_value",
        "cost_basis",
        "unrealised_pnl",
        "pnl_percent",
        mode="plain",
    )
    def serialize_decimals(self, v: Decimal | None) -> str | None:
        return f"{v:f}" if v is not None else None


class PortfolioOut(BaseModel):
    """Aggregate portfolio valuation, cash balance, and performance metrics."""

    holdings_value: Decimal
    cash_balance: Decimal
    total_value: Decimal
    initial_balance: Decimal
    total_pnl: Decimal
    total_pnl_percent: Decimal | None
    has_stale_prices: bool
    as_of: datetime
    holdings: list[PortfolioHoldingOut]

    model_config = ConfigDict(from_attributes=True)

    @field_serializer(
        "holdings_value",
        "cash_balance",
        "total_value",
        "initial_balance",
        "total_pnl",
        "total_pnl_percent",
        mode="plain",
    )
    def serialize_decimals(self, v: Decimal | None) -> str | None:
        return f"{v:f}" if v is not None else None


# Alias for pure valuation function return type
PortfolioView = PortfolioOut
