"""Trade and portfolio notification service.

Dispatches real-time WebSocket notifications to targeted user connections
after trades have committed to the database.
"""

from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import structlog

from app.schemas.trade import TradeOut
from app.services.portfolio_service import PortfolioService
from app.services.ws_manager import ConnectionManager

logger = structlog.stdlib.get_logger(__name__)


class TradeNotifier:
    """Dispatches post-commit trade confirmations and portfolio snapshots to users."""

    def __init__(
        self,
        manager: ConnectionManager,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.manager = manager
        self.session_factory = session_factory

    async def notify_trade_committed(
        self,
        user_id: UUID,
        trade: Any,
        balance: Decimal,
    ) -> None:
        """Send trade confirmation and fresh portfolio view to user WebSockets.

        This method MUST NEVER raise: all errors are trapped and logged so that
        the HTTP response and completed database transaction remain completely unaffected.
        """
        try:
            # (a) Trade confirmation message
            trade_payload: dict[str, Any]
            if isinstance(trade, TradeOut):
                trade_payload = trade.model_dump(mode="json")
            elif hasattr(trade, "model_dump"):
                trade_payload = trade.model_dump(mode="json")
            else:
                trade_payload = TradeOut.model_validate(trade).model_dump(mode="json")

            confirmation = {
                "type": "trade_confirmation",
                "trade": trade_payload,
                "balance": str(balance),
            }
            await self.manager.send_to_user(user_id, confirmation)

            # (b) Fresh portfolio view
            factory = self.session_factory
            if factory is None:
                from app.core.database import async_session_factory

                factory = async_session_factory

            async with factory() as session:
                service = PortfolioService(session)
                portfolio = await service.get_portfolio(user_id)

            update = {
                "type": "portfolio_update",
                "portfolio": portfolio.model_dump(mode="json"),
            }
            await self.manager.send_to_user(user_id, update)

            logger.debug(
                "trade_notification_dispatched",
                user_id=str(user_id),
                symbol=trade_payload.get("symbol"),
            )
        except Exception as exc:
            logger.warning(
                "trade_notification_failed",
                user_id=str(user_id),
                error=str(exc),
            )


def get_trade_notifier(request: Request) -> TradeNotifier:
    """FastAPI dependency to retrieve the singleton TradeNotifier from app.state."""
    if hasattr(request.app.state, "trade_notifier"):
        return request.app.state.trade_notifier

    # Fallback if accessed before lifespan initialization
    from app.core.database import async_session_factory

    ws_manager = getattr(request.app.state, "ws_manager", ConnectionManager())
    notifier = TradeNotifier(manager=ws_manager, session_factory=async_session_factory)
    request.app.state.trade_notifier = notifier
    return notifier

