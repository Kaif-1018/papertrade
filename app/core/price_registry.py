"""In-memory price registry for live market ticks.

Why a plain dict is safe here:
The application runs on a single Python asyncio event loop. In CPython,
dictionary reads and writes are atomic bytecode instructions protected by the GIL,
and neither set_price nor get_price contains any `await` expressions or I/O yield points.
Because no task can yield control mid-operation, dictionary mutations and lookups
are completely race-free and thread-safe within the event loop without locks.
"""

from collections.abc import Callable, Iterable
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

# Caches the last known price for any symbol that ever received a price tick.
# Even when a pair is deactivated and remove_price() drops it from _registry,
# _last_known_prices preserves its terminal price so users can still execute SELLs.
_last_known_prices: dict[str, PriceEntry] = {}

# DB-driven active and known trading pairs set (cached in memory, refreshed on admin operations)
_active_symbols: set[str] = set()
_known_symbols: set[str] = set()


def set_active_symbols(symbols: Iterable[str]) -> None:
    """Synchronize the in-memory active trading pairs cache from the database."""
    global _active_symbols, _known_symbols
    norm = {s.strip().upper() for s in symbols}
    _active_symbols = norm
    _known_symbols.update(norm)


def add_active_symbol(symbol: str) -> None:
    """Mark a symbol as actively supported in the in-memory cache."""
    norm = symbol.strip().upper()
    _active_symbols.add(norm)
    _known_symbols.add(norm)


def deactivate_symbol(symbol: str) -> None:
    """Mark a symbol as deactivated in the in-memory cache."""
    norm = symbol.strip().upper()
    _active_symbols.discard(norm)
    _known_symbols.add(norm)


def get_active_symbols() -> set[str]:
    """Return all currently active trading pairs."""
    if _active_symbols:
        return set(_active_symbols)
    return set(get_settings().trading_pairs_list)


def get_known_symbols() -> set[str]:
    """Return all known trading pairs (both active and deactivated)."""
    if _known_symbols:
        return set(_known_symbols)
    return get_active_symbols()


def set_price(
    symbol: str,
    price: Decimal,
    event_time: datetime,
    clock: Callable[[], datetime] | None = None,
) -> PriceEntry:
    """Record a newly arrived price tick in the registry.

    Normalizes symbol to uppercase and updates both live and last-known price maps.
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
    _last_known_prices[normalized_sym] = entry
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


def get_sell_price(
    symbol: str,
    clock: Callable[[], datetime] | None = None,
    stale_seconds: int | float | None = None,
) -> PriceEntry:
    """Retrieve the price for liquidating (selling) a holding.

    For active symbols, delegates to get_price() enforcing freshness.
    For deactivated symbols where live WebSocket feeds have stopped, allows selling
    at the terminal last known price stored in _last_known_prices without raising StalePrice.
    If no price was ever seen, raises PriceUnavailable.
    """
    normalized_sym = symbol.strip().upper()

    if is_supported(normalized_sym):
        return get_price(normalized_sym, clock=clock, stale_seconds=stale_seconds)

    # Inactive symbol liquidation fallback
    entry = _registry.get(normalized_sym) or _last_known_prices.get(normalized_sym)
    if entry is None:
        raise PriceUnavailable(f"Price is unavailable for symbol {normalized_sym}")

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


def get_price_status_safe(
    symbol: str,
    clock: Callable[[], datetime] | None = None,
    stale_seconds: int | float | None = None,
) -> tuple[Decimal | None, int | None, bool]:
    """Safely retrieve price, age_ms, and stale status for a symbol.

    Returns:
        (price, age_ms, stale)
        - If symbol is missing from registry: (None, None, True)
        - If symbol is present but older than stale_seconds: (entry.price, age_ms, True)
        - If symbol is present and fresh: (entry.price, age_ms, False)
    Never raises.
    """
    normalized_sym = symbol.strip().upper()
    entry = _registry.get(normalized_sym) or _last_known_prices.get(normalized_sym)
    if entry is None:
        return None, None, True

    now_fn = clock or (lambda: datetime.now(timezone.utc))
    now = now_fn()

    settings = get_settings()
    max_stale = (
        settings.PRICE_STALE_SECONDS if stale_seconds is None else stale_seconds
    )
    age_seconds = (now - entry.received_at).total_seconds()
    age_ms = max(0, int(age_seconds * 1000))
    is_stale = age_seconds > max_stale

    return entry.price, age_ms, is_stale


def get_all() -> dict[str, PriceEntry]:
    """Return a shallow copy of all current registry entries."""
    return _registry.copy()


def is_supported(symbol: str) -> bool:
    """Return True if symbol is active in DB-driven trading pairs (or default settings list)."""
    normalized_sym = symbol.strip().upper()
    if _active_symbols:
        return normalized_sym in _active_symbols
    return normalized_sym in get_settings().trading_pairs_list


def is_known(symbol: str) -> bool:
    """Return True if symbol is known in the system (active or deactivated)."""
    normalized_sym = symbol.strip().upper()
    if _known_symbols:
        return normalized_sym in _known_symbols
    return is_supported(normalized_sym)


def remove_price(symbol: str) -> bool:
    """Remove a symbol from live registry while retaining terminal price in _last_known_prices."""
    normalized_sym = symbol.strip().upper()
    return _registry.pop(normalized_sym, None) is not None


def clear() -> None:
    """Wipe all registry entries and last known prices (used for test isolation)."""
    _registry.clear()
    _last_known_prices.clear()
    _active_symbols.clear()
    _known_symbols.clear()
