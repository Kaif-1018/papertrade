"""Unit tests for pure portfolio valuation function (app/services/portfolio_service.py).

Verifies:
- Empty portfolio valuation
- Single holding in profit
- Single holding in loss
- Mixed multi-asset holdings
- Missing and stale price handling (valued at cost basis)
- Zero cost basis and zero initial balance division guards
- Exact Decimal precision with hand-calculated expected values
"""

from decimal import Decimal

import pytest

from app.services.portfolio_service import value_portfolio


def test_empty_portfolio():
    """Empty portfolio with no holdings: total value equals cash balance, zero PnL."""
    res = value_portfolio(
        balance=Decimal("10000.00000000"),
        initial_balance=Decimal("10000.00000000"),
        holdings=[],
        prices={},
    )

    assert res.holdings_value == Decimal("0.00000000")
    assert res.cash_balance == Decimal("10000.00000000")
    assert res.total_value == Decimal("10000.00000000")
    assert res.total_pnl == Decimal("0.00000000")
    assert res.total_pnl_percent == Decimal("0.0000")
    assert res.has_stale_prices is False
    assert len(res.holdings) == 0


def test_single_holding_with_profit():
    """Holding in profit:
    Holding: 1.5 BTC @ 60,000.00 USD avg
    Current Price: 70,000.00 USD

    Hand Calculations:
      cost_basis = 1.5 * 60,000 = 90,000.00000000
      market_value = 1.5 * 70,000 = 105,000.00000000
      unrealised_pnl = 105,000 - 90,000 = 15,000.00000000
      pnl_percent = (15,000 / 90,000) * 100 = 16.6666666... -> quantize 16.6667%
      cash_balance = 5,000.00000000
      initial_balance = 95,000.00000000
      total_value = 5,000 + 105,000 = 110,000.00000000
      total_pnl = 110,000 - 95,000 = 15,000.00000000
      total_pnl_percent = (15,000 / 95,000) * 100 = 15.789473... -> quantize 15.7895%
    """
    holdings = [
        {"symbol": "BTCUSDT", "quantity": Decimal("1.50000000"), "avg_buy_price": Decimal("60000.00000000")}
    ]
    prices = {"BTCUSDT": Decimal("70000.00000000")}

    res = value_portfolio(
        balance=Decimal("5000.00000000"),
        initial_balance=Decimal("95000.00000000"),
        holdings=holdings,
        prices=prices,
    )

    h = res.holdings[0]
    assert h.symbol == "BTCUSDT"
    assert h.quantity == Decimal("1.50000000")
    assert h.avg_buy_price == Decimal("60000.00000000")
    assert h.current_price == Decimal("70000.00000000")
    assert h.cost_basis == Decimal("90000.00000000")
    assert h.market_value == Decimal("105000.00000000")
    assert h.unrealised_pnl == Decimal("15000.00000000")
    assert h.pnl_percent == Decimal("16.6667")
    assert h.stale is False

    assert res.holdings_value == Decimal("105000.00000000")
    assert res.total_value == Decimal("110000.00000000")
    assert res.total_pnl == Decimal("15000.00000000")
    assert res.total_pnl_percent == Decimal("15.7895")
    assert res.has_stale_prices is False


def test_single_holding_with_loss():
    """Holding in loss:
    Holding: 4.0 ETH @ 3,000.00 USD avg
    Current Price: 2,500.00 USD

    Hand Calculations:
      cost_basis = 4.0 * 3,000 = 12,000.00000000
      market_value = 4.0 * 2,500 = 10,000.00000000
      unrealised_pnl = 10,000 - 12,000 = -2,000.00000000
      pnl_percent = (-2,000 / 12,000) * 100 = -16.6667%
      cash_balance = 2,000.00000000
      initial_balance = 14,000.00000000
      total_value = 2,000 + 10,000 = 12,000.00000000
      total_pnl = 12,000 - 14,000 = -2,000.00000000
      total_pnl_percent = (-2,000 / 14,000) * 100 = -14.2857%
    """
    holdings = [
        {"symbol": "ETHUSDT", "quantity": Decimal("4.00000000"), "avg_buy_price": Decimal("3000.00000000")}
    ]
    prices = {"ETHUSDT": Decimal("2500.00000000")}

    res = value_portfolio(
        balance=Decimal("2000.00000000"),
        initial_balance=Decimal("14000.00000000"),
        holdings=holdings,
        prices=prices,
    )

    h = res.holdings[0]
    assert h.cost_basis == Decimal("12000.00000000")
    assert h.market_value == Decimal("10000.00000000")
    assert h.unrealised_pnl == Decimal("-2000.00000000")
    assert h.pnl_percent == Decimal("-16.6667")
    assert h.stale is False

    assert res.holdings_value == Decimal("10000.00000000")
    assert res.total_value == Decimal("12000.00000000")
    assert res.total_pnl == Decimal("-2000.00000000")
    assert res.total_pnl_percent == Decimal("-14.2857")


def test_mixed_holdings_valuation():
    """Mixed holdings: one profit, one loss, one break-even."""
    holdings = [
        {"symbol": "BTCUSDT", "quantity": Decimal("1.00000000"), "avg_buy_price": Decimal("50000.00000000")},
        {"symbol": "ETHUSDT", "quantity": Decimal("10.00000000"), "avg_buy_price": Decimal("3000.00000000")},
    ]
    prices = {
        "BTCUSDT": Decimal("60000.00000000"),  # +10,000 profit
        "ETHUSDT": Decimal("2800.00000000"),   # -2,000 loss
    }

    res = value_portfolio(
        balance=Decimal("1000.00000000"),
        initial_balance=Decimal("81000.00000000"),
        holdings=holdings,
        prices=prices,
    )

    assert len(res.holdings) == 2
    # Holdings are sorted alphabetically
    btc = res.holdings[0]
    eth = res.holdings[1]

    assert btc.symbol == "BTCUSDT"
    assert btc.unrealised_pnl == Decimal("10000.00000000")

    assert eth.symbol == "ETHUSDT"
    assert eth.unrealised_pnl == Decimal("-2000.00000000")

    # Total holdings value: 60,000 + 28,000 = 88,000
    assert res.holdings_value == Decimal("88000.00000000")
    assert res.total_value == Decimal("89000.00000000")
    assert res.total_pnl == Decimal("8000.00000000")
    # Total PnL %: (8,000 / 81,000) * 100 = 9.8765%
    assert res.total_pnl_percent == Decimal("9.8765")


def test_stale_or_missing_price_valued_at_cost_basis():
    """Missing or stale price must be valued at cost basis with stale=True."""
    holdings = [
        {"symbol": "SOLUSDT", "quantity": Decimal("50.00000000"), "avg_buy_price": Decimal("150.00000000")}
    ]
    # Price is None (simulating unavailable or stale registry entry)
    prices = {"SOLUSDT": None}

    res = value_portfolio(
        balance=Decimal("2500.00000000"),
        initial_balance=Decimal("10000.00000000"),
        holdings=holdings,
        prices=prices,
    )

    h = res.holdings[0]
    assert h.stale is True
    assert h.current_price is None
    # 50 * 150 = 7500
    assert h.cost_basis == Decimal("7500.00000000")
    assert h.market_value == Decimal("7500.00000000")
    assert h.unrealised_pnl == Decimal("0.00000000")
    assert h.pnl_percent == Decimal("0.0000")

    assert res.has_stale_prices is True
    assert res.holdings_value == Decimal("7500.00000000")
    assert res.total_value == Decimal("10000.00000000")
    assert res.total_pnl == Decimal("0.00000000")


def test_zero_cost_basis_guard():
    """Zero cost basis (e.g. promotional gift holding with avg_buy_price 0).

    Must guard against division by zero in pnl_percent calculation and return None.
    """
    holdings = [
        {"symbol": "DOGEUSDT", "quantity": Decimal("1000.00000000"), "avg_buy_price": Decimal("0.00000000")}
    ]
    prices = {"DOGEUSDT": Decimal("0.15000000")}

    res = value_portfolio(
        balance=Decimal("50.00000000"),
        initial_balance=Decimal("50.00000000"),
        holdings=holdings,
        prices=prices,
    )

    h = res.holdings[0]
    assert h.cost_basis == Decimal("0.00000000")
    assert h.market_value == Decimal("150.00000000")
    assert h.unrealised_pnl == Decimal("150.00000000")
    # Division by zero guarded -> None
    assert h.pnl_percent is None


def test_zero_initial_balance_guard():
    """Zero initial balance must guard against division by zero in total_pnl_percent."""
    res = value_portfolio(
        balance=Decimal("500.00000000"),
        initial_balance=Decimal("0.00000000"),
        holdings=[],
        prices={},
    )

    assert res.total_value == Decimal("500.00000000")
    assert res.total_pnl == Decimal("500.00000000")
    assert res.total_pnl_percent is None


def test_decimal_exactness_tiny_amounts():
    """Verify precision with tiny quantity:
    Holding: 0.00000001 BTC @ 67123.45678901
    Current Price: 68987.65432109

    Hand Calculation:
      quantity = 0.00000001 (10^-8)
      cost_basis = 0.00000001 * 67123.45678901 = 0.0006712345678901 -> quantize 0.00067123
      market_value = 0.00000001 * 68987.65432109 = 0.0006898765432109 -> quantize 0.00068988
      unrealised_pnl = 0.00068988 - 0.00067123 = 0.00001865
    """
    holdings = [
        {
            "symbol": "BTCUSDT",
            "quantity": Decimal("0.00000001"),
            "avg_buy_price": Decimal("67123.45678901"),
        }
    ]
    prices = {"BTCUSDT": Decimal("68987.65432109")}

    res = value_portfolio(
        balance=Decimal("100.00000000"),
        initial_balance=Decimal("100.00000000"),
        holdings=holdings,
        prices=prices,
    )

    h = res.holdings[0]
    assert h.cost_basis == Decimal("0.00067123")
    assert h.market_value == Decimal("0.00068988")
    assert h.unrealised_pnl == Decimal("0.00001865")

