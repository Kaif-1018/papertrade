"""Day 4 — Price feed message parsing unit tests.

Verifies pure parser function parse_ticker_message against Binance combined
stream payloads, malformed data, invalid numbers, and edge cases.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json

from app.services.price_feed import ParsedTick, parse_ticker_message


def test_parse_valid_ticker_json_string():
    """Valid Binance combined stream ticker JSON parses into a ParsedTick."""
    payload = json.dumps(
        {
            "stream": "btcusdt@ticker",
            "data": {
                "e": "24hrTicker",
                "E": 1710000000000,
                "s": "BTCUSDT",
                "c": "67500.50000000",
            },
        }
    )
    tick = parse_ticker_message(payload)
    assert tick is not None
    assert isinstance(tick, ParsedTick)
    assert tick.symbol == "BTCUSDT"
    assert tick.price == Decimal("67500.50000000")
    assert tick.event_time == datetime.fromtimestamp(1710000000, tz=timezone.utc)


def test_parse_valid_ticker_bytes_input():
    """Bytes input decodes and parses correctly."""
    payload = json.dumps(
        {
            "stream": "ethusdt@ticker",
            "data": {
                "e": "24hrTicker",
                "E": 1710000000123,
                "s": "ethusdt",
                "c": "3450.25",
            },
        }
    ).encode("utf-8")
    tick = parse_ticker_message(payload)
    assert tick is not None
    assert tick.symbol == "ETHUSDT"
    assert tick.price == Decimal("3450.25")


def test_parse_malformed_json():
    """Unparsable JSON string returns None without raising."""
    assert parse_ticker_message("not valid json {{") is None


def test_parse_missing_required_fields():
    """Payloads missing s, c, or E return None."""
    # Missing price
    p1 = json.dumps({"data": {"s": "BTCUSDT", "E": 1710000000000}})
    assert parse_ticker_message(p1) is None

    # Missing symbol
    p2 = json.dumps({"data": {"c": "100.0", "E": 1710000000000}})
    assert parse_ticker_message(p2) is None

    # Missing event time
    p3 = json.dumps({"data": {"s": "BTCUSDT", "c": "100.0"}})
    assert parse_ticker_message(p3) is None


def test_parse_negative_or_zero_price():
    """Negative or zero prices are rejected as invalid."""
    # Negative price
    neg = json.dumps(
        {"data": {"s": "BTCUSDT", "c": "-10.00", "E": 1710000000000}}
    )
    assert parse_ticker_message(neg) is None

    # Zero price
    zero = json.dumps(
        {"data": {"s": "BTCUSDT", "c": "0.00000000", "E": 1710000000000}}
    )
    assert parse_ticker_message(zero) is None


def test_parse_non_numeric_price():
    """Non-numeric string in price field returns None."""
    payload = json.dumps(
        {"data": {"s": "BTCUSDT", "c": "one_hundred", "E": 1710000000000}}
    )
    assert parse_ticker_message(payload) is None


def test_parse_non_ticker_event():
    """Non-ticker payloads (e.g. depth or book) missing ticker fields return None."""
    payload = json.dumps(
        {
            "stream": "btcusdt@depth",
            "data": {
                "e": "depthUpdate",
                "E": 1710000000000,
                "b": [["67000.00", "1.5"]],
            },
        }
    )
    assert parse_ticker_message(payload) is None


def test_parse_non_dict_payload():
    """JSON arrays or scalars return None."""
    assert parse_ticker_message(json.dumps([1, 2, 3])) is None
    assert parse_ticker_message(json.dumps("hello")) is None

