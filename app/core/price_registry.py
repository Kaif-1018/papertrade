"""In-memory price registry for live market ticks.

Why a plain dict is safe here:
The application runs on a single Python asyncio event loop. In CPython,
dictionary reads and writes are atomic bytecode instructions protected by the GIL,
and neither set_price nor get_price contains any `await` expressions or I/O yield points.
Because no task can yield control mid-operation, dictionary mutations and lookups
are completely race-free and thread-safe within the event loop without locks.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app.core.config import get_settings
from app.core.exceptions import PriceUnavailable, StalePrice


@dataclass(frozen=True)
class PriceEntry:
    """Immutable snapshot of the most recent price tick for a symbol."""

    symbol: str
    price: Decimal
    event_time: datetime
    received_at: datetime


# Module-level in-memory store: O(1) reads and writes, no awaits
_registry: dict[str, PriceEntry] = {}


def set_price(
    symbol: str,
    price: Decimal,
    event_time: datetime,
    clock: Callable[[], datetime] | None = None,
) -> PriceEntry:
    """Record a newly arrived price tick in the registry.

    Normalizes symbol to uppercase.
    """
    now_fn = clock or (lambda: datetime.now(timezone.utc))
    received_at = now_fn()
    normalized_sym = symbol.strip().upper()

    entry = PriceEntry(
        symbol=normalized_sym,
        price=price,
        event_time=event_time,
        received_at=received_at,
    )
    _registry[normalized_sym] = entry
    return entry


def get_price(
    symbol: str,
    clock: Callable[[], datetime] | None = None,
    stale_seconds: int | float | None = None,
) -> PriceEntry:
    """Retrieve the latest price for a symbol.

    Raises:
        PriceUnavailable: If the symbol has never been recorded.
        StalePrice: If received_at is older than PRICE_STALE_SECONDS.
    """
    normalized_sym = symbol.strip().upper()

    if normalized_sym not in _registry:
        raise PriceUnavailable(f"Price is unavailable for symbol {normalized_sym}")

    entry = _registry[normalized_sym]
    now_fn = clock or (lambda: datetime.now(timezone.utc))
    now = now_fn()

    settings = get_settings()
    max_stale = (
        settings.PRICE_STALE_SECONDS if stale_seconds is None else stale_seconds
    )

    age_seconds = (now - entry.received_at).total_seconds()
    if age_seconds > max_stale:
        raise StalePrice(
            f"Price for {normalized_sym} is stale ({age_seconds:.2f}s > {max_stale}s)"
        )

    return entry


def get_price_safe(
    symbol: str,
    clock: Callable[[], datetime] | None = None,
    stale_seconds: int | float | None = None,
) -> Decimal | None:
    """Safely retrieve the latest price for a symbol.

    Returns the Decimal price if available and fresh, or None if the price
    has never been seen (PriceUnavailable) or is older than stale threshold (StalePrice).
    """
    try:
        entry = get_price(symbol, clock=clock, stale_seconds=stale_seconds)
        return entry.price
    except (PriceUnavailable, StalePrice):
        return None


def get_all() -> dict[str, PriceEntry]:
    """Return a shallow copy of all current registry entries."""
    return _registry.copy()


def is_supported(symbol: str) -> bool:
    """Return True if symbol exists in settings.TRADING_PAIRS."""
    normalized_sym = symbol.strip().upper()
    return normalized_sym in get_settings().trading_pairs_list


def remove_price(symbol: str) -> bool:
    """Remove a symbol from the registry. Returns True if removed."""
    normalized_sym = symbol.strip().upper()
    return _registry.pop(normalized_sym, None) is not None


def clear() -> None:
    """Wipe all registry entries (used for test isolation)."""
    _registry.clear()
