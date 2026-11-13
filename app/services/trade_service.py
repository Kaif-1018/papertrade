"""Trade execution service — BUY and SELL with atomic transactions.

Design decisions
----------------
* **Price read before locks**: We read the execution price from the in-memory
  registry *before* acquiring any database locks. This minimises the time
  rows are locked and lets PriceUnavailable / StalePrice rejections happen
  without touching the DB at all.

* **Lock order**: User row is always locked first, then the holding row.
  Consistent ordering prevents deadlocks when two concurrent trades operate
  on the same user or (user, symbol) pair.

* **Rounding**: total_value = quantize(price × quantity, ROUND_HALF_UP) at
  8 decimal places. This total is stored once on the trade record and is the
  exact amount debited/credited — no second rounding step at the balance level.
  For sells the CLAUDE.md rule is "ROUND_DOWN on credits to the user"; we apply
  ROUND_DOWN when computing the credit amount added to the balance.

* **Immutable trades**: Once inserted, a trade row is never updated or deleted.
"""

import asyncio
import logging
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.services.notifier import TradeNotifier

from app.core.exceptions import (
    InsufficientFunds,
    InsufficientHoldings,
    InvalidQuantity,
    NotFoundError,
    SymbolNotSupported,
)
from app.core.price_registry import get_price, is_supported
from app.models.trade import Trade, TradeType
from app.repositories.holding_repository import HoldingRepository
from app.repositories.trade_repository import TradeRepository
from app.repositories.user import UserRepository
from app.schemas.trade import (
    MAX_QUANTITY,
    MAX_QUANTITY_DP,
    QUANT,
    TradeOut,
    TradeResult,
    quantize,
)

logger = logging.getLogger(__name__)

# Strong references to running background notification tasks to prevent premature garbage collection
_active_notification_tasks: set[asyncio.Task[Any]] = set()


def _log_notification_task_result(task: asyncio.Task[Any]) -> None:
    """Done callback logging any unhandled exception in fire-and-forget notification tasks."""
    try:
        exc = task.exception()
        if exc is not None:
            logger.warning("trade_notification_task_exception", extra={"error": str(exc)})
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("trade_notification_done_callback_error", extra={"error": str(exc)})


class TradeService:
    """Orchestrates buy/sell trades within a single database transaction."""

    def __init__(
        self,
        session: AsyncSession,
        notifier: "TradeNotifier | None" = None,
    ) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._holdings = HoldingRepository(session)
        self._trades = TradeRepository(session)
        self._notifier = notifier

    def _fire_notification(
        self,
        user_id: UUID,
        trade: TradeOut,
        balance: Decimal,
    ) -> None:
        """Dispatch post-commit trade notification asynchronously without awaiting."""
        if self._notifier is None:
            return

        try:
            task = asyncio.create_task(
                self._notifier.notify_trade_committed(
                    user_id=user_id,
                    trade=trade,
                    balance=balance,
                )
            )
            _active_notification_tasks.add(task)
            task.add_done_callback(_active_notification_tasks.discard)
            task.add_done_callback(_log_notification_task_result)
        except Exception as exc:
            logger.warning("failed_to_spawn_trade_notification_task", extra={"error": str(exc)})

    @staticmethod
    def _validate_symbol(symbol: str) -> str:
        """Validate and normalize symbol."""
        if not symbol or not isinstance(symbol, str) or not symbol.strip():
            raise SymbolNotSupported("Symbol must be a non-empty string")
        sym = symbol.strip().upper()
        if not is_supported(sym):
            raise SymbolNotSupported(f"Symbol {sym} is not supported")
        return sym

    @staticmethod
    def _validate_quantity(quantity: Decimal) -> None:
        """Service-level defense: enforce positive, bounded, 8-dp decimal quantity."""
        if not isinstance(quantity, Decimal):
            raise InvalidQuantity(f"Quantity must be a Decimal, got {type(quantity).__name__}")
        if quantity <= Decimal("0"):
            raise InvalidQuantity("Quantity must be greater than 0")
        if quantity > MAX_QUANTITY:
            raise InvalidQuantity(f"Quantity exceeds maximum allowed ({MAX_QUANTITY})")
        sign, digits, exponent = quantity.as_tuple()
        if exponent != "n" and isinstance(exponent, int) and abs(exponent) > MAX_QUANTITY_DP:
            raise InvalidQuantity(f"Quantity must have at most {MAX_QUANTITY_DP} decimal places")

    # ------------------------------------------------------------------
    # BUY
    # ------------------------------------------------------------------

    async def buy(self, user_id: UUID, symbol: str, quantity: Decimal) -> TradeResult:
        """Execute a BUY trade.

        Steps:
        1. Validate inputs and read execution price from registry.
        2. Lock the user row.
        3. Compute total and verify balance.
        4. Deduct balance, upsert holding, insert trade record.
        5. Commit and return result.
        """
        symbol = self._validate_symbol(symbol)
        self._validate_quantity(quantity)

        # 1. Read price BEFORE opening locks — minimise lock hold time.
        #    get_price raises PriceUnavailable or StalePrice if the feed
        #    has not sent a tick for this symbol recently.
        price_entry = get_price(symbol)
        exec_price = price_entry.price

        # Compute total once and store it — this is the canonical cost.
        # ROUND_HALF_UP for trade total: this is the debit from the buyer's balance.
        total = quantize(exec_price * quantity, ROUND_HALF_UP)

        try:
            # 2. Lock user row first (consistent lock order: user -> holding).
            user = await self._users.get_for_update(user_id)
            if user is None:
                raise NotFoundError("User not found")

            # 3. Balance check.
            if user.virtual_balance < total:
                raise InsufficientFunds(
                    f"Insufficient funds: need {total}, have {user.virtual_balance}",
                    details={
                        "required": str(total),
                        "available": str(user.virtual_balance),
                    },
                )

            # 4a. Deduct balance.
            user.virtual_balance -= total

            # 4b. Upsert holding.
            holding = await self._holdings.get_for_update(user_id, symbol)
            if holding is not None:
                # Weighted average: (old_qty * old_avg + new_qty * new_price) / total_qty
                new_qty = holding.quantity + quantity
                new_avg = quantize(
                    (holding.quantity * holding.avg_buy_price + quantity * exec_price)
                    / new_qty,
                    ROUND_HALF_UP,
                )
                holding.quantity = new_qty
                holding.avg_buy_price = new_avg
            else:
                self._holdings.create(
                    user_id=user_id,
                    symbol=symbol,
                    quantity=quantity,
                    avg_buy_price=exec_price,
                )

            # 4c. Insert immutable trade record.
            trade = Trade(
                user_id=user_id,
                symbol=symbol,
                trade_type=TradeType.BUY,
                quantity=quantity,
                price=exec_price,
                total_value=total,
            )
            self._trades.add(trade)

            # Flush to populate defaults (id, executed_at) before commit.
            await self._session.flush()

            result = TradeResult(
                trade=TradeOut.model_validate(trade),
                balance=user.virtual_balance,
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

        # Post-commit notification: strictly fire-and-forget, never blocks response
        self._fire_notification(user_id=user_id, trade=result.trade, balance=result.balance)

        logger.info(
            "trade_executed",
            extra={
                "trade_type": "BUY",
                "symbol": symbol,
                "quantity": str(quantity),
                "price": str(exec_price),
                "total": str(total),
                "user_id": str(user_id),
            },
        )
        return result

    # ------------------------------------------------------------------
    # SELL
    # ------------------------------------------------------------------

    async def sell(self, user_id: UUID, symbol: str, quantity: Decimal) -> TradeResult:
        """Execute a SELL trade.

        Steps:
        1. Validate inputs and read execution price from registry.
        2. Lock user row FIRST, then holding row (deadlock prevention).
        3. Verify sufficient holdings.
        4. Deduct holdings, credit balance, insert trade record.
        5. Commit and return result.
        """
        symbol = self._validate_symbol(symbol)
        self._validate_quantity(quantity)

        # 1. Read price BEFORE opening locks.
        price_entry = get_price(symbol)
        exec_price = price_entry.price

        # Credit amount: ROUND_DOWN on credits to the user per CLAUDE.md.
        total = quantize(exec_price * quantity, ROUND_DOWN)

        try:
            # 2. Lock user row FIRST — consistent lock order prevents deadlocks.
            #    If two transactions lock (user, holding) in different orders, they
            #    can deadlock. By always locking user first, all transactions wait
            #    in the same queue.
            user = await self._users.get_for_update(user_id)
            if user is None:
                raise NotFoundError("User not found")

            # Then lock the holding row.
            holding = await self._holdings.get_for_update(user_id, symbol)
            if holding is None:
                raise InsufficientHoldings(
                    f"No holdings found for {symbol}",
                    details={
                        "requested": str(quantity),
                        "available": "0",
                    },
                )

            # 3. Verify quantity.
            if holding.quantity < quantity:
                raise InsufficientHoldings(
                    f"Insufficient holdings: want to sell {quantity}, have {holding.quantity}",
                    details={
                        "requested": str(quantity),
                        "available": str(holding.quantity),
                    },
                )

            # 4a. Deduct holdings. avg_buy_price is unchanged on sells.
            holding.quantity -= quantity
            if holding.quantity == Decimal("0"):
                await self._holdings.delete(holding)

            # 4b. Credit balance.
            user.virtual_balance += total

            # 4c. Insert immutable trade record.
            trade = Trade(
                user_id=user_id,
                symbol=symbol,
                trade_type=TradeType.SELL,
                quantity=quantity,
                price=exec_price,
                total_value=total,
            )
            self._trades.add(trade)

            await self._session.flush()

            result = TradeResult(
                trade=TradeOut.model_validate(trade),
                balance=user.virtual_balance,
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise

        # Post-commit notification: strictly fire-and-forget, never blocks response
        self._fire_notification(user_id=user_id, trade=result.trade, balance=result.balance)

        logger.info(
            "trade_executed",
            extra={
                "trade_type": "SELL",
                "symbol": symbol,
                "quantity": str(quantity),
                "price": str(exec_price),
                "total": str(total),
                "user_id": str(user_id),
            },
        )
        return result

