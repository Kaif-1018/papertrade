"""Watchlist service orchestrating personal watchlist operations and live price updates."""

import asyncio
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.core.config import get_settings
from app.core.exceptions import (
    NotFoundError,
    NotInWatchlistError,
    SymbolNotSupported,
    WatchlistFullError,
)
from app.core.price_registry import get_price_status_safe, is_supported
from app.models.watchlist import Watchlist
from app.repositories.user import UserRepository
from app.repositories.watchlist_repository import WatchlistRepository
from app.schemas.watchlist import WatchlistItemOut
from app.services.ws_manager import ConnectionManager

logger = structlog.stdlib.get_logger(__name__)

# Strong references to running background tasks to prevent premature garbage collection
_active_watchlist_tasks: set[asyncio.Task[Any]] = set()


def _log_task_result(task: asyncio.Task[Any]) -> None:
    """Done callback logging any unhandled exception in fire-and-forget notification tasks."""
    try:
        exc = task.exception()
        if exc is not None:
            logger.warning("watchlist_notification_task_error", extra={"error": str(exc)})
    except (asyncio.CancelledError, Exception) as exc:
        logger.warning("watchlist_task_done_callback_error", extra={"error": str(exc)})


class WatchlistService:
    """Service handling watchlist CRUD, concurrency safety, cap enforcement, and live sync."""

    def __init__(
        self,
        session: AsyncSession,
        manager: ConnectionManager | None = None,
    ) -> None:
        self._session = session
        self._repo = WatchlistRepository(session)
        self._users = UserRepository(session)
        self._manager = manager

    def _fire_notification(self, action: str, user_id: UUID, symbol: str) -> None:
        """Dispatch post-commit live sync notification asynchronously without awaiting."""
        if self._manager is None:
            return

        try:
            task = asyncio.create_task(self._notify_manager(action, user_id, symbol))
            _active_watchlist_tasks.add(task)
            task.add_done_callback(_active_watchlist_tasks.discard)
            task.add_done_callback(_log_task_result)
        except Exception as exc:
            logger.warning(
                "failed_to_spawn_watchlist_notification",
                extra={"error": str(exc), "action": action, "symbol": symbol},
            )

    async def _notify_manager(self, action: str, user_id: UUID, symbol: str) -> None:
        """Call manager live sync method with complete exception suppression."""
        if self._manager is None:
            return
        try:
            if action == "added":
                await self._manager.on_watchlist_item_added(user_id, symbol)
            elif action == "removed":
                await self._manager.on_watchlist_item_removed(user_id, symbol)
        except Exception as exc:
            logger.warning(
                "watchlist_manager_notification_failed",
                extra={"error": str(exc), "action": action, "symbol": symbol},
            )

    async def add(self, user_id: UUID, symbol: str) -> tuple[Watchlist, bool]:
        """Add *symbol* to *user_id*'s watchlist.

        Idempotent:
        - If already exists: returns (existing_item, False)
        - If created: returns (created_item, True)

        Concurrency safety:
        - Validates symbol against active pairs.
        - Locks the user row (FOR UPDATE) to serialize concurrent cap checks.
        - Uses INSERT ... ON CONFLICT DO NOTHING to eliminate duplicate row races.
        - Fires post-commit notification to open price WebSockets with watchlist mode enabled.
        """
        norm_symbol = symbol.strip().upper()
        if not is_supported(norm_symbol):
            raise SymbolNotSupported(f"Trading pair '{norm_symbol}' is not supported")

        # 1. Lock user row to serialize concurrent cap evaluations
        user = await self._users.get_for_update(user_id)
        if user is None:
            raise NotFoundError("User not found")

        # 2. Check if already present (idempotent, does not consume cap)
        existing = await self._repo.get_by_user_and_symbol(user_id, norm_symbol)
        if existing is not None:
            return existing, False

        # 3. Check cap against settings.WATCHLIST_MAX_ITEMS
        settings = get_settings()
        count = await self._repo.count_by_user(user_id)
        if count >= settings.WATCHLIST_MAX_ITEMS:
            raise WatchlistFullError(settings.WATCHLIST_MAX_ITEMS)

        # 4. Insert with ON CONFLICT DO NOTHING
        item, created = await self._repo.insert_on_conflict_do_nothing(user_id, norm_symbol)
        await self._session.commit()

        # 5. Fire post-commit live sync notification
        if created:
            self._fire_notification("added", user_id, norm_symbol)

        return item, created

    async def remove(self, user_id: UUID, symbol: str) -> None:
        """Remove *symbol* from *user_id*'s watchlist.

        Raises NotInWatchlistError (404) if absent.
        Fires post-commit notification to manager on success.
        """
        norm_symbol = symbol.strip().upper()
        deleted = await self._repo.delete_by_user_and_symbol(user_id, norm_symbol)
        if not deleted:
            raise NotInWatchlistError("Symbol is not in your watchlist")

        await self._session.commit()
        self._fire_notification("removed", user_id, norm_symbol)

    async def list_with_prices(self, user_id: UUID) -> list[WatchlistItemOut]:
        """Fetch all watchlist items for *user_id* enriched with live price registry status."""
        items = await self._repo.list_by_user(user_id)
        result: list[WatchlistItemOut] = []

        for item in items:
            price, age_ms, stale = get_price_status_safe(item.symbol)
            result.append(
                WatchlistItemOut(
                    symbol=item.symbol,
                    added_at=item.added_at,
                    price=price,
                    price_age_ms=age_ms,
                    stale=stale,
                )
            )

        return result

    async def get_watchlist_symbols(self, user_id: UUID) -> list[str]:
        """Return list of uppercase symbol strings currently on *user_id*'s watchlist."""
        items = await self._repo.list_by_user(user_id)
        return [item.symbol for item in items]

