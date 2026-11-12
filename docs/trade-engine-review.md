# Trade Engine Code Review & Hardening Audit

**Target:** `app/services/trade_service.py`, `app/repositories/`  
**Date:** Day 7 Hardening Pass  
**Status:** Complete — All Identified Critical Vulnerabilities Addressed

---

## 1. Race Conditions & Concurrency Analysis

### 1.1 Balance Double-Spending (Checked & Protected)
- **Vulnerability Mechanism**: If two simultaneous BUY requests read `user.virtual_balance = 550` concurrently before either deducts, both could approve trades costing $500 each, resulting in balance underflow ($-\$450$).
- **Protection**: Every trade begins with `UserRepository.get_for_update(user_id)`. PostgreSQL's `SELECT ... FOR UPDATE` acquires an exclusive row-level lock on the `users` row. The second transaction blocks at the database level until the first commits, reading the updated balance.
- **Verification**: Verified by `test_20_simultaneous_buys_insufficient_funds_boundary` across 20 concurrent tasks (exactly 5 succeed, 15 fail with `InsufficientFunds`, ending balance $50).

### 1.2 Holding Position Overselling (Checked & Protected)
- **Vulnerability Mechanism**: A user holding 7 units submits multiple simultaneous SELL orders for 1 unit. If holding reads are non-locking, all 20 orders could see 7 units available, allowing 20 units to be sold.
- **Protection**: The user row lock serializes all trade operations for that user. In addition, `HoldingRepository.get_for_update(user_id, symbol)` locks the position row.
- **Verification**: Verified by `test_20_simultaneous_sells_insufficient_holdings_boundary` (exactly 7 succeed, 13 fail with `InsufficientHoldings`, position cleanly deleted).

### 1.3 Position Creation Collision / Duplicate Row (Checked & Protected)
- **Vulnerability Mechanism**: If two BUY trades for a new symbol execute concurrently for the same user, both might find `holding is None` and attempt `HoldingRepository.create()`, triggering a `uq_holdings_user_symbol` unique constraint violation.
- **Protection**: Because `user_id` is locked first, the second BUY cannot execute its `holding` query until the first BUY has committed its `INSERT` of the holding row. The second BUY finds the existing holding and executes a weighted-average update rather than an insert.

---

## 2. Lock Ordering & Deadlock Prevention

### 2.1 Enforced Global Hierarchy
PostgreSQL detects deadlocks (SQLSTATE `40P01`) when two or more transactions form a circular dependency waiting for locks held by each other.

The trade engine enforces a strict linear lock acquisition hierarchy:
$$\mathbf{Level\ 1:\ User\ Row} \longrightarrow \mathbf{Level\ 2:\ Holding\ Row}$$

- In `buy()`:
  1. `await self._users.get_for_update(user_id)`
  2. `await self._holdings.get_for_update(user_id, symbol)`
- In `sell()`:
  1. `await self._users.get_for_update(user_id)`
  2. `await self._holdings.get_for_update(user_id, symbol)`

### 2.2 Cross-User Concurrency
- Different users have different primary keys on `users` (`id = user_id`) and different foreign keys on `holdings` (`user_id = user_id`).
- Locks on User A never block or conflict with locks on User B.
- **Verification**: Verified by `test_deadlock_check_50_mixed_trades_across_3_users_and_2_symbols` executing 50 overlapping cross-user, cross-symbol trades with zero deadlocks within 10 seconds.

---

## 3. Rounding Behavior & Precision Exactness

### 3.1 Money Math Rules
- **No Float Usage**: Python `float` is strictly prohibited. Binary floating-point representation drift (e.g., $0.1 \times 0.1 = 0.010000000000000002$) is completely eliminated.
- **`QUANT`**: Fixed at `Decimal("0.00000001")` (8 decimal places), matching PostgreSQL's `Numeric(18, 8)`.

### 3.2 Total Value & Balance Rounding
- **BUY Trades**:
  - `total_value = quantize(price * quantity, ROUND_HALF_UP)`
  - Exact stored `total_value` is deducted from `user.virtual_balance`.
- **SELL Trades**:
  - `total_value = quantize(price * quantity, ROUND_DOWN)`
  - Exact stored `total_value` is credited to `user.virtual_balance`.
  - Uses `ROUND_DOWN` (in the platform's favor) to prevent fractional cent/satoshi creation on liquidation.
- **Holding Weighted Average Buy Price**:
  $$\text{new\_avg} = \operatorname{quantize}\left(\frac{Q_{\text{old}} \cdot P_{\text{old}} + Q_{\text{new}} \cdot P_{\text{new}}}{Q_{\text{old}} + Q_{\text{new}}}, \text{ROUND\_HALF\_UP}\right)$$
  - Quantized to 8 decimal places with standard `ROUND_HALF_UP`.
  - On SELL, `avg_buy_price` is **never modified**, preserving original acquisition cost basis for FIFO accounting.

### 3.3 Conservation of Money Invariant
For any user across arbitrary sequences of trades:
$$\text{virtual\_balance} + \sum \text{BUY.total\_value} - \sum \text{SELL.total\_value} \equiv \text{initial\_balance}$$
Verified by 600 property-style invariant steps across 3 random seeds in `test_trade_invariants.py`.

---

## 4. Error Paths & Transaction Lifecycle

### 4.1 Transaction Leakage Audit
- Every database-mutating block in `TradeService` wraps operations in:
  ```python
  try:
      ...
      await self._session.commit()
  except Exception:
      await self._session.rollback()
      raise
  ```
- **Guaranteed Rollback**: Any unhandled error (repository failure, DB constraint breach, serialization error, connection timeout) immediately triggers `await self._session.rollback()`.
- **Pre-Lock Rejections**: Validation of symbol, quantity, and price staleness occurs *before* touching the database session. Rejections at this stage hold zero database connections and zero transaction slots.

---

## 5. Security & Exploit Analysis

### 5.1 Negative Quantity Exploit (Bypass Defense)
- **Vulnerability**: If an attacker submits a negative quantity (e.g., $Q = -5$) to `buy()`, the cost calculation `total = price * (-5)` yields a negative cost. The deduction `balance -= (-total)` would **credit** money to the attacker's balance, creating an infinite balance exploit.
- **Status**: **FIXED in `TradeService`**. Service-level `_validate_quantity()` strictly enforces $Q > 0$.

### 5.2 Extreme Quantity Numeric Overflow
- **Vulnerability**: Submitting astronomical quantities (e.g., $10^{50}$) could cause overflow in `Numeric(18, 8)` columns, producing unhandled database errors or database denial-of-service.
- **Status**: **FIXED in `TradeService`**. Service-level `_validate_quantity()` rejects $Q > 1{,}000{,}000$.

### 5.3 Sub-Satoshi Precision Stuffing (>8 Decimal Places)
- **Vulnerability**: Quantities with $>8$ decimal places (e.g., `0.000000001`) would either be silently truncated or trigger DB column overflow.
- **Status**: **FIXED in `TradeService`**. Rejects any Decimal where the exponent exceeds $-8$.

### 5.4 Stale Price Arbitrage
- **Vulnerability**: If WebSocket connection to Binance drops or lags, execution could occur at historical prices, allowing latency arbitrage.
- **Status**: **PREVENTED**. `get_price(symbol)` checks `(now - entry.received_at).total_seconds() > PRICE_STALE_SECONDS` (10s) and raises `StalePrice`.

---

## 6. Audit Findings & Fixes Summary

| ID | Issue | Severity | Action Taken | Rationale / Notes |
|---|---|---|---|---|
| **BUG-01** | Missing quantity validation in `TradeService` | **Critical** | **FIXED** | Added `_validate_quantity(quantity)` checking $Q > 0$, $Q \le 1{,}000{,}000$, and max 8 decimal places. |
| **BUG-02** | Unsupported symbol check deferred to `get_price` | **Low** | **FIXED** | Added `_validate_symbol(symbol)` checking `is_supported(symbol)` first to raise `SymbolNotSupported`. |
| **BUG-03** | `test_sell_creates_trade_record` timestamp tie | **Medium** | **FIXED** | In PostgreSQL `func.now()` is identical within the same transaction; fixed test to assert both BUY and SELL types exist. |
| **NOT-FIXED-01** | Read committed isolation vs Serializable | **Informational** | **Kept as-is** | Explicit `SELECT ... FOR UPDATE` on both user and holding rows provides strict serialization semantics without the retry overhead of PostgreSQL `SERIALIZABLE` transaction mode. |
| **NOT-FIXED-02** | Slippage / Market Order price limits | **Informational** | **Deferred to Day 8+** | Paper-trading simulator executes as instant market orders at latest tick; limit orders and slippage protection will be evaluated with order types. |

