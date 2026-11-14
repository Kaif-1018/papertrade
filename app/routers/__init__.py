"""Routers module."""

from app.routers import (
    admin,
    auth,
    health,
    portfolio,
    prices,
    trade,
    watchlist,
    ws_portfolio,
    ws_prices,
)

__all__ = [
    "admin",
    "auth",
    "health",
    "portfolio",
    "prices",
    "trade",
    "watchlist",
    "ws_portfolio",
    "ws_prices",
]
