"""Unit and property-based test suite for pure FIFO realised PnL engine (app/services/pnl.py).

Verifies:
- Case 1: Single buy and sell with exact hand calculation
- Case 2: Sell spanning multiple lots with exact hand calculation
- Case 3: Partial lot consumed across multiple sequential sells
- Case 4: Sell exactly matching lot boundary
- Case 5: Loss scenario (sell price < buy price)
- Case 6: Sell with no prior buy or exceeding quantity raises ValueError
- Case 7: Identical timestamps resolved deterministically via UUID tiebreak
- Case 8: Multi-symbol trades processed strictly independently
- Case 9: Decimal exactness on awkward values (0.1, 0.2, 0.3) with zero float drift
- Case 10: Property invariant tests over randomized valid trade sequences
"""

from datetime import datetime, timezone
from decimal import Decimal
import random
import uuid

import pytest

from app.services.pnl import Lot, Match, SymbolPnl, TradeRecord, calculate_fifo_pnl


def make_trade(
    symbol: str,
    trade_type: str,
    quantity: str,
    price: str,
    ts: datetime,
    trade_id: uuid.UUID | None = None,
) -> TradeRecord:
    return TradeRecord(
        id=trade_id or uuid.uuid4(),
        symbol=symbol,
        trade_type=trade_type,
        quantity=Decimal(quantity),
        price=Decimal(price),
        executed_at=ts,
    )


# ---------------------------------------------------------------------------
# Hand-Computed Scenarios (Documented in Comments)
# ---------------------------------------------------------------------------

def test_case_1_single_buy_and_sell():
    """Hand calculation:
    BUY  1.00000000 @ $50,000.00
    SELL 1.00000000 @ $55,000.00
    PnL = (55000.00 - 50000.00) * 1.0 = +$5,000.00000000
    Open Quantity = 0
    """
    t0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
    trades = [
        make_trade("BTCUSDT", "BUY", "1.00000000", "50000.00000000", t0),
        make_trade("BTCUSDT", "SELL", "1.00000000", "55000.00000000", t1),
    ]

    pnl = calculate_fifo_pnl(trades, "BTCUSDT")

    assert pnl.realised_pnl == Decimal("5000.00000000")
    assert pnl.open_quantity == Decimal("0.00000000")
    assert pnl.total_bought_qty == Decimal("1.00000000")
    assert pnl.total_sold_qty == Decimal("1.00000000")
    assert len(pnl.closed_matches) == 1
    assert pnl.closed_matches[0].pnl == Decimal("5000.00000000")
    assert pnl.closed_matches[0].quantity == Decimal("1.00000000")


def test_case_2_sell_spanning_multiple_lots():
    """Hand calculation (Worked example from specification):
    1. BUY  1.00000000 @ $100.00 -> Lot 1
    2. BUY  1.00000000 @ $110.00 -> Lot 2
    3. SELL 1.50000000 @ $120.00:
       - Match 1: 1.0 from Lot 1 @ 100 -> PnL = (120 - 100) * 1.0 = 20.00
       - Match 2: 0.5 from Lot 2 @ 110 -> PnL = (120 - 110) * 0.5 = 5.00
       Total Realised PnL = 20.00 + 5.00 = +$25.00000000
       Remaining in Lot 2 = 0.50000000 @ $110.00
       Remaining Open Qty = 0.50000000
    """
    t0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    b1_id, b2_id, s_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    trades = [
        make_trade("ETHUSDT", "BUY", "1.00000000", "100.00000000", t0, b1_id),
        make_trade("ETHUSDT", "BUY", "1.00000000", "110.00000000", t1, b2_id),
        make_trade("ETHUSDT", "SELL", "1.50000000", "120.00000000", t2, s_id),
    ]

    res = calculate_fifo_pnl(trades, "ETHUSDT")

    assert res.realised_pnl == Decimal("25.00000000")
    assert res.open_quantity == Decimal("0.50000000")
    assert len(res.open_lots) == 1
    assert res.open_lots[0].trade_id == b2_id
    assert res.open_lots[0].quantity == Decimal("0.50000000")
    assert res.open_lots[0].price == Decimal("110.00000000")

    assert len(res.closed_matches) == 2
    assert res.closed_matches[0].buy_trade_id == b1_id
    assert res.closed_matches[0].quantity == Decimal("1.00000000")
    assert res.closed_matches[0].pnl == Decimal("20.00000000")

    assert res.closed_matches[1].buy_trade_id == b2_id
    assert res.closed_matches[1].quantity == Decimal("0.50000000")
    assert res.closed_matches[1].pnl == Decimal("5.00000000")


def test_case_3_partial_lot_consumed_across_two_sells():
    """Hand calculation:
    1. BUY  2.00000000 @ $100.00 -> Lot 1 (qty 2.0)
    2. SELL 0.50000000 @ $120.00:
       - Match 1: 0.5 from Lot 1 -> PnL = (120 - 100) * 0.5 = 10.00
       - Lot 1 remaining = 1.50000000
    3. SELL 1.00000000 @ $130.00:
       - Match 2: 1.0 from Lot 1 -> PnL = (130 - 100) * 1.0 = 30.00
       - Lot 1 remaining = 0.50000000
    Total Realised PnL = 10.00 + 30.00 = +$40.00000000
    Remaining Open Qty = 0.50000000
    """
    t0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    b_id = uuid.uuid4()

    trades = [
        make_trade("SOLUSDT", "BUY", "2.00000000", "100.00000000", t0, b_id),
        make_trade("SOLUSDT", "SELL", "0.50000000", "120.00000000", t1),
        make_trade("SOLUSDT", "SELL", "1.00000000", "130.00000000", t2),
    ]

    res = calculate_fifo_pnl(trades, "SOLUSDT")

    assert res.realised_pnl == Decimal("40.00000000")
    assert res.open_quantity == Decimal("0.50000000")
    assert len(res.open_lots) == 1
    assert res.open_lots[0].trade_id == b_id
    assert res.open_lots[0].quantity == Decimal("0.50000000")


def test_case_4_sell_exactly_at_lot_boundary():
    """A sell that exactly consumes an entire lot leaves next lot untouched."""
    t0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    b1_id, b2_id = uuid.uuid4(), uuid.uuid4()

    trades = [
        make_trade("BTCUSDT", "BUY", "1.00000000", "50000.00", t0, b1_id),
        make_trade("BTCUSDT", "BUY", "2.00000000", "60000.00", t1, b2_id),
        make_trade("BTCUSDT", "SELL", "1.00000000", "70000.00", t2),
    ]

    res = calculate_fifo_pnl(trades, "BTCUSDT")

    assert res.realised_pnl == Decimal("20000.00000000")
    assert res.open_quantity == Decimal("2.00000000")
    assert len(res.open_lots) == 1
    assert res.open_lots[0].trade_id == b2_id
    assert res.open_lots[0].quantity == Decimal("2.00000000")


def test_case_5_loss_scenario():
    """Selling at a lower price accurately produces a negative realised PnL."""
    t0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)

    trades = [
        make_trade("BTCUSDT", "BUY", "0.50000000", "60000.00000000", t0),
        make_trade("BTCUSDT", "SELL", "0.50000000", "52000.00000000", t1),
    ]

    res = calculate_fifo_pnl(trades, "BTCUSDT")

    # (52000 - 60000) * 0.5 = -4000
    assert res.realised_pnl == Decimal("-4000.00000000")
    assert len(res.closed_matches) == 1
    assert res.closed_matches[0].pnl == Decimal("-4000.00000000")


def test_case_6_sell_with_insufficient_open_lots_raises():
    """Data corruption guard: SELL exceeding open lots must raise ValueError."""
    t0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)

    # 1. Sell with zero prior buys
    with pytest.raises(ValueError) as exc1:
        calculate_fifo_pnl([make_trade("BTCUSDT", "SELL", "1.0", "50000", t0)], "BTCUSDT")
    assert "Data corruption guard" in str(exc1.value)

    # 2. Sell quantity > buy quantity
    trades = [
        make_trade("BTCUSDT", "BUY", "1.00000000", "50000.00", t0),
        make_trade("BTCUSDT", "SELL", "1.00000001", "55000.00", t1),
    ]
    with pytest.raises(ValueError) as exc2:
        calculate_fifo_pnl(trades, "BTCUSDT")
    assert "Data corruption guard" in str(exc2.value)


def test_case_7_identical_timestamps_id_tiebreak():
    """Trades executed at the exact same microsecond are ordered deterministically by UUID."""
    same_time = datetime(2026, 1, 1, 10, 0, 0, 0, tzinfo=timezone.utc)

    # id_buy is lexicographically smaller than id_sell
    id_buy = uuid.UUID("00000000-0000-0000-0000-000000000001")
    id_sell = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

    # Even if sell is passed first in input list, sorting by (executed_at, id) places buy first
    trades = [
        make_trade("BTCUSDT", "SELL", "1.0", "60000", same_time, id_sell),
        make_trade("BTCUSDT", "BUY", "1.0", "50000", same_time, id_buy),
    ]

    res = calculate_fifo_pnl(trades, "BTCUSDT")
    assert res.realised_pnl == Decimal("10000.00000000")
    assert res.open_quantity == Decimal("0.00000000")


def test_case_8_many_symbols_processed_independently():
    """Trades from multiple symbols are isolated and do not cross-match."""
    t0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)

    mixed_trades = [
        make_trade("BTCUSDT", "BUY", "1.0", "50000", t0),
        make_trade("ETHUSDT", "BUY", "10.0", "3000", t0),
        make_trade("BTCUSDT", "SELL", "1.0", "55000", t1),
        make_trade("ETHUSDT", "SELL", "5.0", "3500", t1),
    ]

    btc_pnl = calculate_fifo_pnl(mixed_trades, "BTCUSDT")
    eth_pnl = calculate_fifo_pnl(mixed_trades, "ETHUSDT")

    assert btc_pnl.symbol == "BTCUSDT"
    assert btc_pnl.realised_pnl == Decimal("5000.00000000")
    assert btc_pnl.open_quantity == Decimal("0.00000000")

    assert eth_pnl.symbol == "ETHUSDT"
    # (3500 - 3000) * 5 = 2500
    assert eth_pnl.realised_pnl == Decimal("2500.00000000")
    assert eth_pnl.open_quantity == Decimal("5.00000000")


def test_case_9_decimal_exactness_no_float_drift():
    """Demonstrate Decimal exactness on 100 buys of 0.1 @ 0.1 and sells @ 0.2.

    Float arithmetic introduces binary representation drift:
    In float: 0.1 + 0.2 != 0.3 (0.30000000000000004).
    In Decimal: 100 * (0.2 - 0.1) * 0.1 = exactly 1.00000000 with zero drift.
    """
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trades = []

    # 100 sequential buys
    for i in range(100):
        t = datetime.fromtimestamp(base_time.timestamp() + i, tz=timezone.utc)
        trades.append(make_trade("DOGEUSDT", "BUY", "0.10000000", "0.10000000", t))

    # 100 sequential sells
    for i in range(100):
        t = datetime.fromtimestamp(base_time.timestamp() + 100 + i, tz=timezone.utc)
        trades.append(make_trade("DOGEUSDT", "SELL", "0.10000000", "0.20000000", t))

    res = calculate_fifo_pnl(trades, "DOGEUSDT")

    # Hand calculation: 100 matches of qty 0.1 with price diff (0.2 - 0.1 = 0.1)
    # Total PnL = 100 * 0.1 * 0.1 = 1.00000000 exactly
    assert res.realised_pnl == Decimal("1.00000000")
    assert res.total_bought_qty == Decimal("10.00000000")
    assert res.total_sold_qty == Decimal("10.00000000")
    assert res.open_quantity == Decimal("0.00000000")


# ---------------------------------------------------------------------------
# Property-Style Invariant Testing
# ---------------------------------------------------------------------------

def test_case_10_property_invariants_random_trades():
    """Property test: For any random valid trade sequence, verify key invariants:
    1. sum(m.quantity for m in closed_matches) == total_sold_qty
    2. open_quantity == total_bought_qty - total_sold_qty
    3. sum(lot.quantity for lot in open_lots) == open_quantity
    """
    rng = random.Random(42)  # Fixed seed for reproducibility
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    trades = []
    available_qty = Decimal("0")
    time_offset = 0

    for _ in range(200):
        time_offset += 60
        t = datetime.fromtimestamp(base_time.timestamp() + time_offset, tz=timezone.utc)
        # Random price between 10.00 and 1000.00
        price = Decimal(str(rng.randint(1000, 100000))) / Decimal("100")

        # Decide whether to BUY or SELL (can only SELL if available_qty > 0)
        can_sell = available_qty > Decimal("0.01")
        do_sell = can_sell and rng.choice([True, False, False])  # 1/3 chance to sell

        if do_sell:
            # Sell a fraction of available quantity
            sell_qty = (available_qty * Decimal(str(rng.randint(10, 100))) / Decimal("100")).quantize(Decimal("0.00000001"))
            if sell_qty > Decimal("0"):
                trades.append(make_trade("BTCUSDT", "SELL", str(sell_qty), str(price), t))
                available_qty -= sell_qty
        else:
            # Buy between 0.01000000 and 5.00000000
            buy_qty = (Decimal(str(rng.randint(1, 500))) / Decimal("100")).quantize(Decimal("0.00000001"))
            trades.append(make_trade("BTCUSDT", "BUY", str(buy_qty), str(price), t))
            available_qty += buy_qty

    res = calculate_fifo_pnl(trades, "BTCUSDT")

    # Invariant 1: Sum of matched quantities must equal total sold quantity
    matched_qty_sum = sum((m.quantity for m in res.closed_matches), Decimal("0"))
    assert matched_qty_sum == res.total_sold_qty

    # Invariant 2: Open quantity equals total bought minus total sold
    assert res.open_quantity == res.total_bought_qty - res.total_sold_qty

    # Invariant 3: Sum of open lot quantities equals reported open quantity
    open_lots_sum = sum((l.quantity for l in res.open_lots), Decimal("0"))
    assert open_lots_sum == res.open_quantity

