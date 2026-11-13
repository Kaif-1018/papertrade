"""Portfolio valuation service and pure valuation algorithm.

Pure function `value_portfolio` handles all valuation math with exact Decimal
arithmetic, no floating-point conversions, and no database or network I/O.
"""

from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.core.exceptions import NotFoundError
from app.core.price_registry import get_price_safe
from app.repositories.holding_repository import HoldingRepository
from app.repositories.user import UserRepository
from app.schemas.portfolio import (
    PortfolioHoldingOut,
    PortfolioOut,
    QUANT_AMOUNT,
    QUANT_PERCENT,
)

logger = structlog.stdlib.get_logger(__name__)


def quantize_amount(value: Decimal, rounding: str = ROUND_HALF_UP) -> Decimal:
    """Quantize an asset/cash amount to 8 decimal places."""
    return value.quantize(QUANT_AMOUNT, rounding=rounding)


def quantize_percent(value: Decimal, rounding: str = ROUND_HALF_UP) -> Decimal:
    """Quantize a percentage metric to 4 decimal places."""
    return value.quantize(QUANT_PERCENT, rounding=rounding)


def value_portfolio(
    balance: Decimal,
    initial_balance: Decimal,
    holdings: list[Any],
    prices: dict[str, Decimal | None],
    clock: Callable[[], datetime] | None = None,
) -> PortfolioOut:
    """Pure valuation function computing holding market values, PnL, and portfolio totals.

    Args:
        balance: Current liquid cash balance (virtual USD).
        initial_balance: User's baseline deposit balance (e.g. 10,000 USD).
        holdings: Collection of Holding entities, dataclasses, or dicts with
                  `symbol`, `quantity`, and `avg_buy_price`.
        prices: Mapping of uppercase symbol -> Decimal price (or None if unavailable/stale).
        clock: Optional zero-argument function returning the current UTC datetime.

    Valuation Rationale for Missing / Stale Prices:
        If a market price is missing or stale (feed disconnect or tick > stale threshold),
        valuing the position at $0 would falsely wipe out the user's equity and trigger
        catastrophic margin errors. Valuing the holding at its original cost basis
        conservatively assumes 0% unrealised PnL for that specific asset while keeping
        the overall portfolio valuation stable, continuous, and meaningful.
    """
    holding_views: list[PortfolioHoldingOut] = []

    for h in holdings:
        # Support both SQLAlchemy model instances and dicts
        symbol = str(getattr(h, "symbol", None) or h.get("symbol")).strip().upper()
        quantity = Decimal(str(getattr(h, "quantity", None) or h.get("quantity")))
        avg_buy_price = Decimal(
            str(getattr(h, "avg_buy_price", None) or h.get("avg_buy_price"))
        )

        cost_basis = quantize_amount(quantity * avg_buy_price)
        current_price_raw = prices.get(symbol)

        if current_price_raw is None:
            # Stale / Unavailable price fallback: value at cost basis
            stale = True
            current_price = None
            market_value = cost_basis
            unrealised_pnl = Decimal("0.00000000")
            pnl_percent = (
                Decimal("0.0000") if cost_basis != Decimal("0") else None
            )
        else:
            stale = False
            current_price = quantize_amount(Decimal(str(current_price_raw)))
            market_value = quantize_amount(quantity * current_price)
            unrealised_pnl = quantize_amount(market_value - cost_basis)

            if cost_basis == Decimal("0"):
                pnl_percent = None
            else:
                raw_pct = (unrealised_pnl / cost_basis) * Decimal("100")
                pnl_percent = quantize_percent(raw_pct)

        holding_views.append(
            PortfolioHoldingOut(
                symbol=symbol,
                quantity=quantize_amount(quantity),
                avg_buy_price=quantize_amount(avg_buy_price),
                current_price=current_price,
                market_value=market_value,
                cost_basis=cost_basis,
                unrealised_pnl=unrealised_pnl,
                pnl_percent=pnl_percent,
                stale=stale,
            )
        )

    # Sort holdings alphabetically by symbol for deterministic order
    holding_views.sort(key=lambda x: x.symbol)

    holdings_value = quantize_amount(
        sum((h.market_value for h in holding_views), Decimal("0"))
    )
    cash_balance = quantize_amount(Decimal(str(balance)))
    initial_bal = quantize_amount(Decimal(str(initial_balance)))
    total_value = quantize_amount(cash_balance + holdings_value)
    total_pnl = quantize_amount(total_value - initial_bal)

    if initial_bal == Decimal("0"):
        total_pnl_percent = None
    else:
        raw_total_pct = (total_pnl / initial_bal) * Decimal("100")
        total_pnl_percent = quantize_percent(raw_total_pct)

    has_stale_prices = any(h.stale for h in holding_views)
    as_of = (clock or (lambda: datetime.now(timezone.utc)))()

    return PortfolioOut(
        holdings_value=holdings_value,
        cash_balance=cash_balance,
        total_value=total_value,
        initial_balance=initial_bal,
        total_pnl=total_pnl,
        total_pnl_percent=total_pnl_percent,
        has_stale_prices=has_stale_prices,
        as_of=as_of,
        holdings=holding_views,
    )


class PortfolioService:
    """Service to load user holdings, fetch market prices, and value portfolios."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._holdings = HoldingRepository(session)

    async def get_portfolio(self, user_id: UUID) -> PortfolioOut:
        """Fetch user portfolio without row locks and calculate real-time valuation."""
        user = await self._users.get_by_id(user_id)
        if user is None:
            raise NotFoundError(f"User {user_id} not found")

        holdings = await self._holdings.list_by_user(user_id)

        prices: dict[str, Decimal | None] = {}
        for h in holdings:
            sym = h.symbol.upper()
            prices[sym] = get_price_safe(sym)

        return value_portfolio(
            balance=user.virtual_balance,
            initial_balance=user.initial_balance,
            holdings=holdings,
            prices=prices,
        )

