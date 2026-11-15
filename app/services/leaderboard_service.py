"""Leaderboard service and pure ranking computation.

Architecture:
- Pure ranking function `compute_rankings` handles valuation, sorting, and standard competition ranking.
- Redis caching with 30-second TTL under key `leaderboard:v1`.
- Stampede (thundering herd) protection via non-blocking mutex locks (`leaderboard:lock`).
- Graceful degradation: if Redis is unavailable, computation falls back directly to the database.
"""

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import logging
from typing import Any
import uuid
from uuid import UUID

import asyncio
from redis.asyncio import Redis

from app.core.cache import (
    LEADERBOARD_CACHE_KEY,
    LEADERBOARD_CACHE_TTL,
    LEADERBOARD_LOCK_KEY,
    LEADERBOARD_LOCK_TTL,
    LEADERBOARD_LOCK_WAIT_POLL_INTERVAL,
    LEADERBOARD_LOCK_WAIT_TIMEOUT,
    acquire_lock,
    get_cached_leaderboard,
    release_lock,
    set_cached_leaderboard,
)
from app.core.price_registry import get_price_safe
from app.repositories.leaderboard_repository import (
    HoldingLeaderboardRow,
    LeaderboardRepository,
    UserLeaderboardRow,
)
from app.schemas.leaderboard import (
    LeaderboardEntryOut,
    LeaderboardMeOut,
    LeaderboardResponse,
    RankedUser,
    mask_email_local_part,
)

logger = logging.getLogger(__name__)

QUANT_AMOUNT = Decimal("0.00000001")
QUANT_PERCENT = Decimal("0.01")


def quantize_amount(value: Decimal) -> Decimal:
    """Quantize balance or dollar PnL to 8 decimal places."""
    return value.quantize(QUANT_AMOUNT, rounding=ROUND_HALF_UP)


def quantize_percent(value: Decimal) -> Decimal:
    """Quantize percentage return to 2 decimal places."""
    return value.quantize(QUANT_PERCENT, rounding=ROUND_HALF_UP)


def compute_rankings(
    users: Sequence[Any],
    holdings: Sequence[Any],
    prices: dict[str, Decimal | None],
) -> list[RankedUser]:
    """Pure function computing total values, PnL, and competition ranks.

    Args:
        users: Iterable of user records (must have id, email, virtual_balance, initial_balance, created_at).
        holdings: Iterable of holding records (must have user_id, symbol, quantity, avg_buy_price).
        prices: Dict mapping uppercase symbol to latest live price (or None if unavailable/stale).

    Valuation Rationale for Missing / Stale Prices:
        Consistent with Day 9 portfolio valuation, if a symbol's live price is missing
        from the registry or stale (>10s), the holding is valued at its cost basis
        (avg_buy_price). This conservatively assumes 0% unrealised PnL on that holding,
        maintaining a stable, continuous valuation without falsely penalizing users.

    Sorting & Tiebreaking:
        1. total_value DESC (highest portfolio equity)
        2. created_at ASC (earlier registered user wins tie)
        3. id ASC (deterministic UUID tiebreak)

    Standard Competition Ranking ("1 2 2 4"):
        Users with exactly equal total_value share the same rank. The subsequent
        rank skips numbers equal to the size of the tied cohort.
    """
    # 1. Group holdings by user_id
    holdings_by_user: dict[UUID, list[Any]] = defaultdict(list)
    for h in holdings:
        uid = getattr(h, "user_id", None) or h.get("user_id")
        holdings_by_user[UUID(str(uid))].append(h)

    # 2. Value each user's portfolio
    user_computations: list[dict[str, Any]] = []

    for u in users:
        u_id = UUID(str(getattr(u, "id", None) or u.get("id")))
        email = str(getattr(u, "email", None) or u.get("email"))
        balance = Decimal(str(getattr(u, "virtual_balance", None) or u.get("virtual_balance")))
        initial = Decimal(str(getattr(u, "initial_balance", None) or u.get("initial_balance")))
        created_at = getattr(u, "created_at", None) or u.get("created_at")

        holdings_val = Decimal("0")
        for h in holdings_by_user.get(u_id, []):
            sym = str(getattr(h, "symbol", None) or h.get("symbol")).strip().upper()
            qty = Decimal(str(getattr(h, "quantity", None) or h.get("quantity")))
            cost_basis_price = Decimal(
                str(getattr(h, "avg_buy_price", None) or h.get("avg_buy_price"))
            )

            # Fallback to cost basis if price is missing or stale
            live_price = prices.get(sym)
            exec_price = live_price if live_price is not None else cost_basis_price
            holdings_val += qty * exec_price

        total_value = quantize_amount(balance + holdings_val)
        pnl = quantize_amount(total_value - initial)
        pnl_percent = (
            quantize_percent((pnl / initial) * Decimal("100"))
            if initial > Decimal("0")
            else Decimal("0.00")
        )

        user_computations.append(
            {
                "user_id": u_id,
                "email": email,
                "display_name": mask_email_local_part(email),
                "total_value": total_value,
                "pnl": pnl,
                "pnl_percent": pnl_percent,
                "created_at": created_at,
            }
        )

    # 3. Sort: total_value DESC, created_at ASC, id ASC
    sorted_users = sorted(
        user_computations,
        key=lambda item: (-item["total_value"], item["created_at"], item["user_id"]),
    )

    # 4. Standard competition ranking assignment (1, 2, 2, 4)
    ranked_users: list[RankedUser] = []
    for i, item in enumerate(sorted_users):
        if i > 0 and item["total_value"] == sorted_users[i - 1]["total_value"]:
            rank = ranked_users[-1].rank
        else:
            rank = i + 1

        ranked_users.append(
            RankedUser(
                user_id=item["user_id"],
                display_name=item["display_name"],
                total_value=item["total_value"],
                pnl=item["pnl"],
                pnl_percent=item["pnl_percent"],
                rank=rank,
            )
        )

    return ranked_users


class LeaderboardService:
    """Orchestrates leaderboard retrieval, Redis caching, and cache stampede protection."""

    def __init__(
        self,
        repository: LeaderboardRepository,
        redis: Redis | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repository
        self._redis = redis
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def get_leaderboard(
        self,
        current_user_id: UUID,
        limit: int = 20,
    ) -> LeaderboardResponse:
        """Retrieve the platform leaderboard with stampede-protected caching.

        Cache Strategy & Trade-off:
            - The entire ranked list is cached in Redis under `leaderboard:v1` for 30s.
            - A trade invalidates the cache immediately so a user's own trade reflects instantly.
            - Other users' portfolio valuations might be up to 30s stale as external prices fluctuate,
              which is an intentional and acceptable trade-off to protect DB performance.
        """
        # If Redis is unavailable, compute directly from DB without locking
        if self._redis is None:
            data = await self._compute_leaderboard_payload()
            return self._build_response(data, current_user_id, limit)

        # 1. Check Redis cache
        cached_json = await get_cached_leaderboard(self._redis)
        if cached_json:
            try:
                data = json.loads(cached_json)
                return self._build_response(data, current_user_id, limit)
            except Exception as exc:
                logger.warning("invalid_leaderboard_cache_payload", extra={"error": str(exc)})

        # 2. Cache miss -> Stampede protection via distributed mutex lock
        token = uuid.uuid4().hex
        got_lock = await acquire_lock(
            self._redis,
            LEADERBOARD_LOCK_KEY,
            token,
            expire_seconds=LEADERBOARD_LOCK_TTL,
        )

        if got_lock:
            try:
                data = await self._compute_leaderboard_payload()
                payload = json.dumps(data)
                await set_cached_leaderboard(
                    self._redis,
                    payload,
                    ttl=LEADERBOARD_CACHE_TTL,
                )
                return self._build_response(data, current_user_id, limit)
            finally:
                await release_lock(self._redis, LEADERBOARD_LOCK_KEY, token)

        # 3. Lock not acquired -> Loser polls briefly for the winner's cached result
        loop = asyncio.get_event_loop()
        deadline = loop.time() + LEADERBOARD_LOCK_WAIT_TIMEOUT

        while loop.time() < deadline:
            await asyncio.sleep(LEADERBOARD_LOCK_WAIT_POLL_INTERVAL)
            cached_json = await get_cached_leaderboard(self._redis)
            if cached_json:
                try:
                    data = json.loads(cached_json)
                    return self._build_response(data, current_user_id, limit)
                except Exception:
                    pass

        # 4. Fallback: Lock wait expired, compute directly to guarantee service availability
        logger.warning("leaderboard_lock_wait_timeout_fallback_to_direct_compute")
        data = await self._compute_leaderboard_payload()
        return self._build_response(data, current_user_id, limit)

    async def _compute_leaderboard_payload(self) -> dict[str, Any]:
        """Execute the two DB queries, gather live prices, and compute rankings."""
        users, holdings = await self._repo.get_all_users_and_holdings()

        # Gather unique holding symbols to resolve live prices
        unique_symbols = {h.symbol.upper() for h in holdings}
        prices = {sym: get_price_safe(sym) for sym in unique_symbols}

        ranked_users = compute_rankings(users, holdings, prices)

        as_of = self._clock().isoformat()
        serialized_users = [
            {
                "user_id": str(u.user_id),
                "rank": u.rank,
                "display_name": u.display_name,
                "total_value": f"{u.total_value:.8f}",
                "pnl": f"{u.pnl:.8f}",
                "pnl_percent": f"{u.pnl_percent:.2f}",
            }
            for u in ranked_users
        ]

        return {
            "as_of": as_of,
            "total_users": len(users),
            "users": serialized_users,
        }

    def _build_response(
        self,
        data: dict[str, Any],
        current_user_id: UUID,
        limit: int,
    ) -> LeaderboardResponse:
        """Slice the cached full ranking into the top N entries and extract the 'me' block."""
        all_users = data.get("users", [])
        as_of_str = data.get("as_of")
        as_of = (
            datetime.fromisoformat(as_of_str)
            if as_of_str
            else self._clock()
        )
        total_users = data.get("total_users", len(all_users))

        str_user_id = str(current_user_id)
        me_entry: LeaderboardMeOut | None = None

        for u in all_users:
            if u["user_id"] == str_user_id:
                me_entry = LeaderboardMeOut(
                    rank=u["rank"],
                    total_value=u["total_value"],
                    pnl_percent=u["pnl_percent"],
                )
                break

        top_slice = all_users[:limit]
        entries = [
            LeaderboardEntryOut(
                rank=u["rank"],
                display_name=u["display_name"],
                total_value=u["total_value"],
                pnl=u["pnl"],
                pnl_percent=u["pnl_percent"],
                is_me=(u["user_id"] == str_user_id),
            )
            for u in top_slice
        ]

        return LeaderboardResponse(
            as_of=as_of,
            entries=entries,
            me=me_entry,
            total_users=total_users,
        )
