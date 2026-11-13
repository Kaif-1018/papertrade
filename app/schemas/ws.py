"""WebSocket message schemas for price broadcasting."""

from typing import Literal
from pydantic import BaseModel, ConfigDict


class WSClientAction(BaseModel):
    """Client request message received over WebSocket."""

    action: Literal["subscribe", "unsubscribe", "ping"]
    symbol: str | None = None

    model_config = ConfigDict(extra="ignore")


class WSPongResponse(BaseModel):
    """Response to client ping action."""

    type: Literal["pong"] = "pong"


class WSSubscribedResponse(BaseModel):
    """Confirmation of topic subscription."""

    type: Literal["subscribed"] = "subscribed"
    symbol: str


class WSUnsubscribedResponse(BaseModel):
    """Confirmation of topic unsubscription."""

    type: Literal["unsubscribed"] = "unsubscribed"
    symbol: str


class WSPriceSnapshot(BaseModel):
    """Initial price snapshot sent immediately upon subscription."""

    type: Literal["snapshot"] = "snapshot"
    symbol: str
    price: str
    event_time: str
    age_ms: int


class WSPriceTick(BaseModel):
    """Real-time price tick broadcast from Redis."""

    type: Literal["price"] = "price"
    symbol: str
    price: str
    ts: str


class WSErrorResponse(BaseModel):
    """Structured error payload for invalid actions or limit breaches."""

    type: Literal["error"] = "error"
    code: str
    message: str

