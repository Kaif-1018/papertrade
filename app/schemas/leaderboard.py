"""Schemas and data models for leaderboard endpoints and pure ranking computation.

All monetary values are serialized as exact Decimal strings with 8 decimal places.
Percentage metrics are quantized to 2 decimal places.
User emails and IDs are strictly masked to ensure privacy.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


def mask_email_local_part(email: str) -> str:
    """Derive an anonymized display name from the local part of an email address.

    Rules:
    - Retains up to the first 2 characters of the local part followed by '***'.
    - Domain and '@' are strictly omitted to prevent user deanonymization.
    - Examples:
      - 'alice@example.com' -> 'al***'
      - 'kaif@gmail.com'    -> 'ka***'
      - 'b@test.com'       -> 'b***'
    """
    if not email:
        return "***"
    local = email.split("@")[0].strip()
    if not local:
        return "***"
    prefix = local[:2] if len(local) >= 2 else local[:1]
    return f"{prefix}***"


@dataclass(frozen=True)
class RankedUser:
    """Internal representation of a computed leaderboard ranking entry."""

    user_id: UUID
    display_name: str
    total_value: Decimal
    pnl: Decimal
    pnl_percent: Decimal
    rank: int


class LeaderboardEntryOut(BaseModel):
    """Public leaderboard entry for the top N ranking list."""

    model_config = ConfigDict(from_attributes=True)

    rank: int = Field(..., description="Competition rank (ties share same rank)")
    display_name: str = Field(..., description="Anonymized username derived from local email part")
    total_value: str = Field(..., description="Total portfolio value in USD (cash + holdings)")
    pnl: str = Field(..., description="Total dollar PnL against initial balance")
    pnl_percent: str = Field(..., description="Percentage return quantized to 2 decimals")
    is_me: bool = Field(..., description="Whether this entry corresponds to the authenticated caller")


class LeaderboardMeOut(BaseModel):
    """The calling user's personal rank standing (present even if outside top N)."""

    model_config = ConfigDict(from_attributes=True)

    rank: int = Field(..., description="User's current competition rank")
    total_value: str = Field(..., description="User's total portfolio value")
    pnl_percent: str = Field(..., description="User's percentage return quantized to 2 decimals")


class LeaderboardResponse(BaseModel):
    """Full payload for GET /api/leaderboard."""

    model_config = ConfigDict(from_attributes=True)

    as_of: datetime = Field(..., description="UTC server timestamp of computation")
    entries: list[LeaderboardEntryOut] = Field(..., description="Top N ranked participants")
    me: LeaderboardMeOut | None = Field(None, description="Calling user's rank card (if found)")
    total_users: int = Field(..., description="Total number of registered trading accounts")
