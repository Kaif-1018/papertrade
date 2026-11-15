"""Unit tests for pure leaderboard ranking algorithm (compute_rankings).

Verifies:
- Accurate ordering by total_value descending
- Standard competition ranking ('1 2 2 4'): tied total_values share rank, next rank skips
- Secondary tiebreak by earlier created_at and tertiary tiebreak by UUID id
- Missing / stale price fallback to cost basis (avg_buy_price)
- User with no holdings (cash-only account)
- Loss scenario: negative dollar PnL and negative percentage return
- Exact Decimal precision without floating-point drift
- Identity masking: derived from local email part, domain completely omitted
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid

import pytest

from app.schemas.leaderboard import RankedUser, mask_email_local_part
from app.services.leaderboard_service import compute_rankings


def make_user(
    email: str,
    virtual_balance: str,
    initial_balance: str = "10000.00000000",
    created_at: datetime | None = None,
    user_id: uuid.UUID | None = None,
) -> dict:
    return {
        "id": user_id or uuid.uuid4(),
        "email": email,
        "virtual_balance": Decimal(virtual_balance),
        "initial_balance": Decimal(initial_balance),
        "created_at": created_at or datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
    }


def make_holding(
    user_id: uuid.UUID,
    symbol: str,
    quantity: str,
    avg_buy_price: str,
) -> dict:
    return {
        "user_id": user_id,
        "symbol": symbol,
        "quantity": Decimal(quantity),
        "avg_buy_price": Decimal(avg_buy_price),
    }


def test_mask_email_local_part():
    """Verify display_name derivation hides domain and masks appropriately."""
    assert mask_email_local_part("alice@example.com") == "al***"
    assert mask_email_local_part("kaif@gmail.com") == "ka***"
    assert mask_email_local_part("bob@test.org") == "bo***"
    assert mask_email_local_part("x@domain.com") == "x***"
    assert mask_email_local_part("") == "***"
    # Ensure domain is NEVER present in the output
    assert "@" not in mask_email_local_part("trader123@crypto.com")
    assert "crypto.com" not in mask_email_local_part("trader123@crypto.com")


def test_compute_rankings_basic_ordering():
    """Rankings order users strictly by total_value descending."""
    u1 = make_user("alice@test.com", "5000.00000000")  # Cash 5k + BTC 1 @ 60k = 65k
    u2 = make_user("bob@test.com", "10000.00000000")   # Cash 10k + ETH 10 @ 3k = 40k
    u3 = make_user("charlie@test.com", "8000.00000000") # Cash 8k + no holdings = 8k

    holdings = [
        make_holding(u1["id"], "BTCUSDT", "1.00000000", "50000.00000000"),
        make_holding(u2["id"], "ETHUSDT", "10.00000000", "2500.00000000"),
    ]

    prices = {
        "BTCUSDT": Decimal("60000.00000000"),
        "ETHUSDT": Decimal("3000.00000000"),
    }

    rankings = compute_rankings([u1, u2, u3], holdings, prices)

    assert len(rankings) == 3
    # Rank 1: Alice (65,000)
    assert rankings[0].user_id == u1["id"]
    assert rankings[0].rank == 1
    assert rankings[0].total_value == Decimal("65000.00000000")
    assert rankings[0].pnl == Decimal("55000.00000000")
    assert rankings[0].pnl_percent == Decimal("550.00")
    assert rankings[0].display_name == "al***"

    # Rank 2: Bob (40,000)
    assert rankings[1].user_id == u2["id"]
    assert rankings[1].rank == 2
    assert rankings[1].total_value == Decimal("40000.00000000")
    assert rankings[1].pnl == Decimal("30000.00000000")
    assert rankings[1].pnl_percent == Decimal("300.00")

    # Rank 3: Charlie (8,000)
    assert rankings[2].user_id == u3["id"]
    assert rankings[2].rank == 3
    assert rankings[2].total_value == Decimal("8000.00000000")
    assert rankings[2].pnl == Decimal("-2000.00000000")
    assert rankings[2].pnl_percent == Decimal("-20.00")


def test_standard_competition_ranking_ties_and_skips():
    """Tied users receive identical ranks; the subsequent rank skips appropriately (1, 2, 2, 4)."""
    t0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    u1 = make_user("top@test.com", "20000.00000000", created_at=t0)
    u2 = make_user("tie_first@test.com", "15000.00000000", created_at=t0)
    u3 = make_user("tie_second@test.com", "15000.00000000", created_at=t0 + timedelta(hours=1))
    u4 = make_user("fourth@test.com", "12000.00000000", created_at=t0)
    u5 = make_user("fifth_a@test.com", "10000.00000000", created_at=t0)
    u6 = make_user("fifth_b@test.com", "10000.00000000", created_at=t0 + timedelta(minutes=5))
    u7 = make_user("seventh@test.com", "9000.00000000", created_at=t0)

    rankings = compute_rankings([u1, u2, u3, u4, u5, u6, u7], [], {})

    ranks = [r.rank for r in rankings]
    # Expected: 1, 2, 2, 4, 5, 5, 7
    assert ranks == [1, 2, 2, 4, 5, 5, 7]


def test_tiebreak_created_at_and_id():
    """When total_value is tied, earlier created_at appears before later created_at."""
    t_early = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    t_late = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    id_small = uuid.UUID("00000000-0000-0000-0000-000000000001")
    id_large = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

    u_early = make_user("early@test.com", "10000.00000000", created_at=t_early, user_id=id_large)
    u_late = make_user("late@test.com", "10000.00000000", created_at=t_late, user_id=id_small)

    rankings = compute_rankings([u_late, u_early], [], {})

    # Both share rank 1, but u_early is positioned first due to earlier created_at
    assert rankings[0].user_id == u_early["id"]
    assert rankings[1].user_id == u_late["id"]
    assert rankings[0].rank == 1
    assert rankings[1].rank == 1

    # Exact same timestamp tiebreaks by id
    u_id1 = make_user("id1@test.com", "10000.00000000", created_at=t_early, user_id=id_small)
    u_id2 = make_user("id2@test.com", "10000.00000000", created_at=t_early, user_id=id_large)

    rankings2 = compute_rankings([u_id2, u_id1], [], {})
    assert rankings2[0].user_id == id_small
    assert rankings2[1].user_id == id_large


def test_missing_price_fallback_to_cost_basis():
    """If a price is missing from prices dict (or is None), holding is valued at avg_buy_price."""
    u = make_user("trader@test.com", "5000.00000000")
    # 2 SOL bought at 150 each = cost basis 300
    h = make_holding(u["id"], "SOLUSDT", "2.00000000", "150.00000000")

    # SOLUSDT is absent or None in prices
    prices = {"BTCUSDT": Decimal("60000.00000000"), "SOLUSDT": None}

    rankings = compute_rankings([u], [h], prices)

    # 5,000 cash + (2 * 150) = 5,300.00000000
    assert rankings[0].total_value == Decimal("5300.00000000")
    # pnl against initial 10k: 5300 - 10000 = -4700.00000000
    assert rankings[0].pnl == Decimal("-4700.00000000")
    assert rankings[0].pnl_percent == Decimal("-47.00")


def test_user_with_no_holdings():
    """Users with zero holdings are accurately ranked solely on cash balance."""
    u = make_user("cash_only@test.com", "10000.00000000", initial_balance="10000.00000000")
    rankings = compute_rankings([u], [], {})

    assert rankings[0].total_value == Decimal("10000.00000000")
    assert rankings[0].pnl == Decimal("0.00000000")
    assert rankings[0].pnl_percent == Decimal("0.00")


def test_loss_scenario_and_decimal_exactness():
    """Verify loss calculations and exact 8-decimal and 2-decimal quantizations."""
    # Started with 10k, now has 1,234.56789012 cash + 0.1 BTC (bought at 50k, now at 40k = 4k)
    u = make_user("loss@test.com", "1234.56789012", initial_balance="10000.00000000")
    h = make_holding(u["id"], "BTCUSDT", "0.10000000", "50000.00000000")
    prices = {"BTCUSDT": Decimal("40000.00000000")}

    rankings = compute_rankings([u], [h], prices)

    # total_value = 1234.56789012 + (0.1 * 40000) = 1234.56789012 + 4000 = 5234.56789012
    assert rankings[0].total_value == Decimal("5234.56789012")
    # pnl = 5234.56789012 - 10000 = -4765.43210988
    assert rankings[0].pnl == Decimal("-4765.43210988")
    # pnl_percent = (-4765.43210988 / 10000) * 100 = -47.6543210988 -> quantized to -47.65
    assert rankings[0].pnl_percent == Decimal("-47.65")
