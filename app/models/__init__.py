"""Models package — import all models here so Alembic autogenerate sees them."""

from app.models.base import Base
from app.models.holding import Holding
from app.models.price_snapshot import PriceSnapshot
from app.models.trade import Trade, TradeType
from app.models.user import User, UserRole
from app.models.watchlist import Watchlist

__all__ = [
    "Base",
    "Holding",
    "PriceSnapshot",
    "Trade",
    "TradeType",
    "User",
    "UserRole",
    "Watchlist",
]
