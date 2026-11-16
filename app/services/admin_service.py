"""AdminService orchestrating trading pair management, user portfolio resets, and platform statistics."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
import structlog

from app.core import price_registry
from app.core.cache import invalidate_leaderboard
from app.core.exceptions import (
    BinanceUnreachableError,
    InvalidBinanceSymbolError,
    NotFoundError,
    PairExistsError,
    PairHasHoldingsError,
    PairNotFoundError,
)
from app.core.logging import audit_logger
from app.models.trading_pair import TradingPair
from app.repositories.admin_repository import AdminRepository
from app.repositories.holding_repository import HoldingRepository
from app.repositories.trading_pair_repository import TradingPairRepository
from app.repositories.user import UserRepository
from app.schemas.admin import (
    AdminPairOut,
    AdminResetOut,
    AdminStatsOut,
    TopSymbolVolume,
)
from app.services.notifier import TradeNotifier
from app.services.portfolio_service import quantize_amount

logger = structlog.stdlib.get_logger(__name__)


async def verify_binance_symbol(symbol: str) -> None:
    """Verify that symbol exists on Binance public REST API within 5s timeout.

    Raises:
        InvalidBinanceSymbolError: If Binance returns an error or status indicating symbol doesn't exist.
        BinanceUnreachableError: If Binance cannot be reached, times out, or returns a 5xx error.
    """
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and "symbol" in data:
                    return
            if resp.status_code in (400, 404):
                raise InvalidBinanceSymbolError(f"Symbol {symbol} does not exist on Binance")
            raise BinanceUnreachableError(f"Binance returned status {resp.status_code}")
    except (InvalidBinanceSymbolError, BinanceUnreachableError):
        raise
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
        raise BinanceUnreachableError(f"Cannot reach Binance: {exc}")
    except Exception as exc:
        raise BinanceUnreachableError(f"Error checking Binance: {exc}")


class AdminService:
    """Provides high-level administrative functions and coordinates feed updates."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._pair_repo = TradingPairRepository(session)
        self._admin_repo = AdminRepository(session)
        self._user_repo = UserRepository(session)
        self._holding_repo = HoldingRepository(session)

    async def add_pair(
        self,
        admin_id: UUID,
        symbol: str,
        price_feed: Any = None,
    ) -> tuple[TradingPair, str]:
        """Validate, verify with Binance, and add or reactivate a trading pair.

        Consistency guarantee:
        The database commit executes FIRST. If the subsequent WebSocket feed subscription
        call fails, the error is logged and 'feed_sync': 'pending' is returned.
        """
        norm_symbol = symbol.strip().upper()
        await verify_binance_symbol(norm_symbol)

        pair = await self._pair_repo.get_by_symbol(norm_symbol)
        if pair is not None and pair.is_active:
            raise PairExistsError(f"Trading pair {norm_symbol} already exists and is active")

        if pair is not None and not pair.is_active:
            # Re-activating a previously deactivated pair is allowed
            pair.is_active = True
            pair.added_by = admin_id
            await self._session.flush()
        else:
            pair = await self._pair_repo.create(
                symbol=norm_symbol,
                added_by=admin_id,
                is_active=True,
            )

        await self._session.commit()

        # Update in-memory active set
        price_registry.add_active_symbol(norm_symbol)

        logger.info(
            "admin_action",
            extra={
                "admin_id": str(admin_id),
                "action": "add_pair",
                "target": norm_symbol,
            },
        )
        audit_logger.info(
            "audit_admin_action",
            admin_id=str(admin_id),
            action="add_pair",
            target=norm_symbol,
        )

        feed_sync = "synced"
        if price_feed is not None:
            try:
                await price_feed.add_symbol(norm_symbol)
            except Exception as exc:
                logger.warning(
                    "feed_add_symbol_failed",
                    symbol=norm_symbol,
                    error=str(exc),
                )
                feed_sync = "pending"

        return pair, feed_sync

    async def deactivate_pair(
        self,
        admin_id: UUID,
        symbol: str,
        force: bool = False,
        price_feed: Any = None,
    ) -> tuple[str, bool, str]:
        """Soft-deactivate a trading pair (never hard delete).

        If users hold the symbol, requires force=True, otherwise raises PairHasHoldingsError (409).
        When deactivated, remove_symbol() is called on the feed.
        """
        norm_symbol = symbol.strip().upper()
        pair = await self._pair_repo.get_by_symbol(norm_symbol)
        if pair is None:
            raise PairNotFoundError(f"Trading pair {norm_symbol} not found")

        if not pair.is_active:
            return norm_symbol, False, "synced"

        holders_count = await self._admin_repo.get_holders_count_for_symbol(norm_symbol)
        if holders_count > 0 and not force:
            raise PairHasHoldingsError(holders_count=holders_count)

        pair.is_active = False
        await self._session.commit()

        price_registry.deactivate_symbol(norm_symbol)

        logger.info(
            "admin_action",
            extra={
                "admin_id": str(admin_id),
                "action": "deactivate_pair",
                "target": norm_symbol,
                "force": force,
                "holders_count": holders_count,
            },
        )
        audit_logger.info(
            "audit_admin_action",
            admin_id=str(admin_id),
            action="deactivate_pair",
            target=norm_symbol,
            force=force,
            holders_count=holders_count,
        )

        feed_sync = "synced"
        if price_feed is not None:
            try:
                await price_feed.remove_symbol(norm_symbol)
            except Exception as exc:
                logger.warning(
                    "feed_remove_symbol_failed",
                    symbol=norm_symbol,
                    error=str(exc),
                )
                feed_sync = "pending"

        return norm_symbol, False, feed_sync

    async def list_pairs(self, price_feed: Any = None) -> list[AdminPairOut]:
        """Fetch all trading pairs with active status, holder counts, and feed subscriptions."""
        pairs = await self._pair_repo.list_all()
        holder_counts = await self._admin_repo.get_all_symbols_holder_counts()

        subscribed_symbols: set[str] = set()
        if price_feed is not None:
            if hasattr(price_feed, "symbols"):
                subscribed_symbols = set(price_feed.symbols)
            elif hasattr(price_feed, "_symbols"):
                subscribed_symbols = set(price_feed._symbols)

        results: list[AdminPairOut] = []
        for p in pairs:
            price, age_ms, is_stale = price_registry.get_price_status_safe(p.symbol)
            results.append(
                AdminPairOut(
                    symbol=p.symbol,
                    is_active=p.is_active,
                    holder_count=holder_counts.get(p.symbol, 0),
                    last_price=str(price) if price is not None else None,
                    price_age_ms=age_ms,
                    feed_subscribed=p.symbol in subscribed_symbols,
                    added_at=p.added_at,
                )
            )
        return results

    async def reset_portfolio(
        self,
        admin_id: UUID,
        user_id: UUID,
        reason: str,
        notifier: TradeNotifier | None = None,
    ) -> AdminResetOut:
        """Reset a user's portfolio to initial balance within one atomic database transaction.

        Trades are KEPT as immutable historical audit records.
        """
        user = await self._user_repo.get_for_update(user_id)
        if user is None:
            raise NotFoundError(f"User {user_id} not found")

        balance_before = user.virtual_balance

        holdings = await self._holding_repo.list_by_user(user_id)
        snapshot = [
            {
                "symbol": h.symbol,
                "quantity": str(h.quantity),
                "avg_buy_price": str(h.avg_buy_price),
            }
            for h in holdings
            if h.quantity > Decimal("0")
        ]

        # Wipe holdings and restore balance
        await self._holding_repo.delete_all_by_user(user_id)
        user.virtual_balance = user.initial_balance

        reset_record = await self._admin_repo.create_portfolio_reset(
            user_id=user_id,
            admin_id=admin_id,
            reason=reason,
            balance_before=balance_before,
            holdings_snapshot=snapshot,
        )

        await self._session.commit()

        logger.info(
            "admin_action",
            extra={
                "admin_id": str(admin_id),
                "action": "reset_portfolio",
                "target": str(user_id),
                "reason": reason,
            },
        )
        audit_logger.info(
            "audit_admin_action",
            admin_id=str(admin_id),
            action="reset_portfolio",
            target=str(user_id),
            reason=reason,
        )

        # Post-commit failure-isolated hooks
        try:
            await invalidate_leaderboard()
        except Exception as exc:
            logger.warning("leaderboard_invalidation_after_reset_failed", error=str(exc))

        if notifier is not None:
            try:
                await notifier.notify_portfolio_reset(user_id)
            except Exception as exc:
                logger.warning("ws_notification_after_reset_failed", error=str(exc))

        return AdminResetOut(
            id=reset_record.id,
            user_id=reset_record.user_id,
            admin_id=reset_record.admin_id,
            reason=reset_record.reason,
            balance_before=str(reset_record.balance_before),
            balance_after=str(user.virtual_balance),
            holdings_snapshot=reset_record.holdings_snapshot,
            reset_at=reset_record.reset_at,
        )

    @staticmethod
    async def get_platform_stats(
        session_factory: async_sessionmaker[AsyncSession],
        ws_manager: Any = None,
        price_feed: Any = None,
    ) -> AdminStatsOut:
        """Fetch platform-wide stats running independent aggregate SQL queries concurrently."""
        since_24h = datetime.now(timezone.utc) - timedelta(hours=24)

        async def _fetch_users():
            async with session_factory() as session:
                return await AdminRepository.get_total_users(session)

        async def _fetch_trades():
            async with session_factory() as session:
                return await AdminRepository.get_trade_volume_stats(session, since_24h)

        async def _fetch_top_symbols():
            async with session_factory() as session:
                return await AdminRepository.get_top_symbols_by_volume(session, limit=5)

        total_users, trade_stats, top_symbols = await asyncio.gather(
            _fetch_users(),
            _fetch_trades(),
            _fetch_top_symbols(),
        )

        active_ws = ws_manager.active_count() if ws_manager is not None else 0
        ws_breakdown = ws_manager.topic_counts() if ws_manager is not None else {}

        feed_health: dict[str, Any] = {}
        if price_feed is not None:
            import dataclasses

            if hasattr(price_feed, "get_status"):
                status_obj = price_feed.get_status()
                if dataclasses.is_dataclass(status_obj):
                    feed_health = dataclasses.asdict(status_obj)
                elif isinstance(status_obj, dict):
                    feed_health = status_obj
                elif hasattr(status_obj, "__dict__") and status_obj.__dict__:
                    feed_health = dict(status_obj.__dict__)
                else:
                    feed_health = {
                        "connected": getattr(status_obj, "connected", True),
                        "reconnect_count": getattr(status_obj, "reconnect_count", 0),
                    }
            elif hasattr(price_feed, "stats"):
                if dataclasses.is_dataclass(price_feed.stats):
                    feed_health = dataclasses.asdict(price_feed.stats)
                else:
                    feed_health = dict(price_feed.stats.__dict__)


        return AdminStatsOut(
            total_users=total_users,
            total_trades=trade_stats["total_trades"],
            volume_24h=str(quantize_amount(trade_stats["volume_24h"])),
            volume_all_time=str(quantize_amount(trade_stats["volume_all_time"])),
            trades_24h=trade_stats["trades_24h"],
            active_traders_24h=trade_stats["active_traders_24h"],
            top_symbols_by_volume=[
                TopSymbolVolume(
                    symbol=item["symbol"],
                    volume=str(quantize_amount(item["volume"])),
                    trade_count=item["trade_count"],
                )
                for item in top_symbols
            ],
            active_ws_connections=active_ws,
            ws_topics_breakdown=ws_breakdown,
            feed_health=feed_health,
        )
