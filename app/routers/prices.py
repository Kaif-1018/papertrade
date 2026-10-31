"""Prices router for real-time and fallback price queries."""

from datetime import datetime, timezone

from fastapi import APIRouter, status

from app.core.exceptions import SymbolNotSupported
from app.core.price_registry import get_all, get_price, is_supported
from app.schemas.price import PriceOut

router = APIRouter(prefix="/api/prices", tags=["prices"])


@router.get(
    "",
    response_model=list[PriceOut],
    status_code=status.HTTP_200_OK,
    summary="Get all current prices",
)
async def get_all_prices() -> list[PriceOut]:
    """Return the latest price snapshots for all active pairs in the registry."""
    entries = get_all()
    now = datetime.now(timezone.utc)

    return [
        PriceOut(
            symbol=entry.symbol,
            price=entry.price,
            event_time=entry.event_time,
            age_ms=max(0, int((now - entry.received_at).total_seconds() * 1000)),
        )
        for entry in entries.values()
    ]


@router.get(
    "/{symbol}",
    response_model=PriceOut,
    status_code=status.HTTP_200_OK,
    summary="Get price for a specific symbol",
)
async def get_price_by_symbol(symbol: str) -> PriceOut:
    """Return the latest price for a supported symbol.

    Raises:
        SymbolNotSupported (404): If symbol is not configured in TRADING_PAIRS.
        PriceUnavailable (503): If the symbol has not received any ticks yet.
        StalePrice (503): If the tick age exceeds PRICE_STALE_SECONDS.
    """
    normalized_sym = symbol.strip().upper()

    if not is_supported(normalized_sym):
        raise SymbolNotSupported(f"Trading pair '{normalized_sym}' is not supported")

    entry = get_price(normalized_sym)
    now = datetime.now(timezone.utc)
    age_ms = max(0, int((now - entry.received_at).total_seconds() * 1000))

    return PriceOut(
        symbol=entry.symbol,
        price=entry.price,
        event_time=entry.event_time,
        age_ms=age_ms,
    )

