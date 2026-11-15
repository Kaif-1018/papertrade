"""Pydantic schemas for administrative endpoints."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AdminPairCreate(BaseModel):
    """Payload to add or reactivate a trading pair."""

    symbol: str = Field(..., description="Trading pair symbol (e.g. BTCUSDT)")

    @field_validator("symbol")
    @classmethod
    def validate_and_normalize_symbol(cls, v: str) -> str:
        if not v or not isinstance(v, str):
            raise ValueError("Symbol must be a non-empty string")
        norm = v.strip().upper()
        import re

        if not re.match(r"^[A-Z0-9]{5,20}$", norm):
            raise ValueError("Symbol format must match ^[A-Z0-9]{5,20}$")
        return norm


class AdminPairOut(BaseModel):
    """Representation of a platform trading pair."""

    model_config = ConfigDict(from_attributes=True)

    symbol: str
    is_active: bool
    holder_count: int
    last_price: str | None = None
    price_age_ms: int | None = None
    feed_subscribed: bool = False
    added_at: datetime
    feed_sync: str | None = None


class AdminPairDeleteOut(BaseModel):
    """Response returned after deactivating a trading pair."""

    symbol: str
    is_active: bool
    feed_sync: str


class AdminResetRequest(BaseModel):
    """Request body for resetting a user's portfolio."""

    reason: str = Field(
        ...,
        min_length=5,
        max_length=200,
        description="Administrative justification for the portfolio reset",
    )


class AdminResetOut(BaseModel):
    """Audit response describing the executed portfolio reset."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    admin_id: UUID | None
    reason: str
    balance_before: str
    balance_after: str
    holdings_snapshot: list[dict[str, Any]]
    reset_at: datetime


class TopSymbolVolume(BaseModel):
    """Volume summary for a trading symbol."""

    symbol: str
    volume: str
    trade_count: int


class AdminStatsOut(BaseModel):
    """Platform-wide operational and financial statistics."""

    total_users: int
    total_trades: int
    volume_24h: str
    volume_all_time: str
    trades_24h: int
    active_traders_24h: int
    top_symbols_by_volume: list[TopSymbolVolume]
    active_ws_connections: int
    ws_topics_breakdown: dict[str, int]
    feed_health: dict[str, Any]
