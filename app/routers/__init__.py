"""Routers module."""

from app.routers import admin, auth, health, portfolio, prices, trade, ws_portfolio, ws_prices

__all__ = [
    "admin",
    "auth",
    "health",
    "portfolio",
    "prices",
    "trade",
    "ws_portfolio",
    "ws_prices",
]
