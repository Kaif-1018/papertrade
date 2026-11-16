# Crypto Paper-Trading Simulator

A production-grade, real-time cryptocurrency paper-trading simulator built with **Python 3.12**, **FastAPI**, **PostgreSQL 16**, and **Redis 7**. Users receive a virtual USD starting balance ($10,000) and execute spot trades against live market prices streamed directly from Binance via resilient WebSockets.

---

## Architecture Overview

```mermaid
flowchart TD
    subgraph MarketData ["Market Data Ingestion"]
        Binance["Binance WebSocket API<br/>(Live Ticker Streams)"]
        FeedTask["BinanceFeed Task<br/>(Auto-reconnect with Jitter)"]
        PriceRegistry["In-Memory Price Registry<br/>(Lock-free, Stale Detection)"]
    end

    subgraph CorePlatform ["Application Layer (FastAPI)"]
        MW["Security & Observability Middleware<br/>(RequestID, RateLimit, BodyLimit, SecurityHeaders)"]
        Routers["HTTP REST Routers<br/>(/api/auth, /api/trade, /api/portfolio, /api/admin)"]
        Services["Domain Services & Invariants<br/>(TradeService, AuthService, FIFO PnL, Analytics)"]
        WSHandlers["WebSocket Handlers<br/>(/ws/prices, /ws/portfolio)"]
        WSManager["WebSocket Connection Manager<br/>(Topic Routing, Slow Client Isolation)"]
    end

    subgraph Persistence ["Persistence & Messaging"]
        PG[(PostgreSQL 16)<br/>ACID Transactions, Row-Level Locking, CHECK Constraints]
        RedisStore[(Redis 7)<br/>Rate Limiting ZSets, Pub/Sub Fan-out, Mutex Cache]
    end

    Binance -->|Live Ticks| FeedTask
    FeedTask -->|Direct In-Memory Write| PriceRegistry
    FeedTask -->|PUBLISH market:ticks:{sym}| RedisStore

    Client["Web / Mobile Browser"] -->|HTTP REST| MW
    MW --> Routers
    Routers --> Services
    Services --> PriceRegistry
    Services -->|Pessimistic Row Lock (FOR UPDATE)| PG
    Services -->|Post-Commit Notification| RedisStore

    WSClient["WebSocket Client"] -->|WS Connection| WSHandlers
    WSHandlers --> WSManager
    RedisStore -->|SUBSCRIBE| WSManager
    WSManager -->|Filtered Push| WSClient
```

---

## Key Features

- **Live Market Streaming:** Auto-reconnecting Binance WebSocket feed streaming real-time prices for top crypto pairs (BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, etc.).
- **Deadlock-Free Atomic Trading:** Spot BUY and SELL order execution with pessimistic row-level locking (`SELECT ... FOR UPDATE`) in deterministic topological order (User $\rightarrow$ Holding).
- **Zero Float Drift:** 100% strict `Decimal` financial calculations quantized to 8 decimal places (`Numeric(18, 8)` in PostgreSQL) with institutional rounding conventions.
- **Pure FIFO Realized PnL Engine:** Deterministic lot matching algorithm tracking cost basis, open buy lots, and realized gain/loss per trade and across portfolio lifecycles.
- **Real-Time WebSocket Gateways:**
  - `/ws/prices`: Subscriptions to live symbol ticks and watchlist streams with initial snapshot delivery.
  - `/ws/portfolio`: Authenticated personal portfolio valuation and real-time trade confirmation push.
- **Enterprise-Grade Rate Limiting:** Redis-backed sliding-window rate limiter powered by atomic Lua scripts with fail-closed security for authentication and fail-open resilience for general APIs.
- **Stampede-Protected Leaderboards:** Global competitive rankings cached in Redis with distributed mutex locks to prevent database stampedes under high concurrency.
- **Security Hardening:** Refresh token rotation with family reuse revocation, strict Pydantic v2 schemas (`extra="forbid"`), request body size caps (64 KB), WebSocket frame/message quotas, and comprehensive security headers.
- **Admin Control Plane:** Dynamic pair activation/deactivation synced with live price feeds, portfolio reset with audit trails, and concurrent aggregate platform statistics.

---

## Tech Stack

| Layer | Technology | Purpose |
|---|---|---|
| **Runtime** | Python 3.12 | Modern async runtime with strict type hints |
| **Web Framework** | FastAPI (0.115) + Uvicorn | High-throughput asynchronous REST and WebSocket API |
| **Database** | PostgreSQL 16 (asyncpg + SQLAlchemy 2.0) | Relational persistence, row-level locking, and ACID constraints |
| **Migrations** | Alembic (async) | Database schema versioning and deterministic migrations |
| **Cache & Pub/Sub** | Redis 7 (redis-py async) | Distributed sliding-window rate limiting, pub/sub fan-out, leaderboard cache |
| **Authentication** | PyJWT + bcrypt | Stateless JWT access tokens + rotating refresh token families |
| **Structured Logging** | structlog | Contextual JSON logs with correlation IDs and credential redaction |
| **Containerization** | Docker & Docker Compose | Isolated multi-container local and staging environment |

---

## Quick Start

### 1. Prerequisites
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows / macOS) or Docker Engine + Docker Compose (Linux).

### 2. Configure Environment
Clone the repository and copy the environment configuration:
```bash
cp .env.example .env
```

### 3. Build & Run
Start all services in detached mode:
```bash
docker compose up -d --build
```

The API will be available at **http://localhost:8000**.

### 4. Health Check
Verify all services are up and healthy:
```bash
curl http://localhost:8000/health
```
```json
{
  "status": "ok",
  "services": {
    "postgres": "ok",
    "redis": "ok"
  }
}
```

---

## Interactive Demo & Testing Walkthrough

The platform includes a built-in static WebSocket and Trading dashboard for visual end-to-end verification.

1. **Open the Test Page:**
   Navigate to **http://localhost:8000/static/index.html** (or view the Swagger UI at **http://localhost:8000/docs**).
2. **Register a Trader:**
   - Under the Auth panel, click **Register / Login** (or POST `/api/auth/register` with `email="trader@example.com"`, `password="SecretPass123!"`).
   - The user account is provisioned with a default virtual balance of **$10,000.00 USD**.
3. **Connect WebSockets:**
   - Connect to `/ws/prices` to observe live Binance market ticks streaming into the interface.
   - Connect to `/ws/portfolio` (using your Bearer token) to open the real-time private portfolio channel.
4. **Place a Trade:**
   - Execute a BUY order for `0.1` BTCUSDT via the trade form or POST `/api/trade/buy`.
   - **Observe:**
     - The REST call returns 201 Created with the exact execution price and remaining balance.
     - The `/ws/portfolio` connection immediately pushes a `trade_confirmation` frame followed by a `portfolio_update` frame with live unrealized PnL.
5. **Inspect Trade History & PnL:**
   - Call `GET /api/trades` to view keyset-paginated trade receipts.
   - Call `GET /api/analytics/pnl` to see real-time FIFO realized gain/loss breakdowns.

---

## API Overview

### Authentication (`/api/auth`)
| Method | Endpoint | Description | Rate Limit |
|---|---|---|---|
| `POST` | `/api/auth/register` | Register new user with $10,000 balance | 10 / min / IP (Fail closed) |
| `POST` | `/api/auth/login` | Authenticate with email/password; returns tokens | 10 / min / IP (Fail closed) |
| `POST` | `/api/auth/refresh` | Rotate refresh token with reuse detection | 10 / min / IP |
| `POST` | `/api/auth/logout` | Revoke current refresh token family | 120 / min / user |
| `GET` | `/api/auth/me` | Fetch authenticated user profile & balance | 120 / min / user |

### Trading & Portfolio (`/api/trade`, `/api/portfolio`, `/api/trades`)
| Method | Endpoint | Description | Rate Limit |
|---|---|---|---|
| `POST` | `/api/trade/buy` | Buy crypto at live price (locks user $\rightarrow$ holding) | 30 / min / user |
| `POST` | `/api/trade/sell` | Sell crypto holdings at live/last price | 30 / min / user |
| `GET` | `/api/portfolio` | Real-time portfolio valuation & unrealized PnL | 120 / min / user |
| `GET` | `/api/trades` | Keyset-paginated trade execution history | 120 / min / user |
| `GET` | `/api/analytics/pnl` | Detailed FIFO realized & unrealized PnL | 120 / min / user |

### Market Data & Watchlist (`/api/prices`, `/api/watchlist`, `/api/leaderboard`)
| Method | Endpoint | Description | Rate Limit |
|---|---|---|---|
| `GET` | `/api/prices` | Read all latest market prices from registry | 120 / min / IP |
| `GET` | `/api/prices/{symbol}` | Read single symbol price and age | 120 / min / IP |
| `GET` | `/api/watchlist` | Fetch user's saved trading pairs | 120 / min / user |
| `POST` | `/api/watchlist` | Add symbol to watchlist (max 25) | 120 / min / user |
| `DELETE` | `/api/watchlist/{symbol}`| Remove symbol from watchlist | 120 / min / user |
| `GET` | `/api/leaderboard` | Top traders ranked by total portfolio value | 120 / min / user |

### Administration (`/api/admin`) *(Requires Admin Role)*
| Method | Endpoint | Description | Rate Limit |
|---|---|---|---|
| `GET` | `/api/admin/pairs` | List all trading pairs, holders, and feed status | 120 / min / user |
| `POST` | `/api/admin/pairs` | Add trading pair (verified on Binance) | 120 / min / user |
| `DELETE` | `/api/admin/pairs/{symbol}`| Soft-deactivate trading pair | 120 / min / user |
| `POST` | `/api/admin/users/{id}/reset`| Reset user portfolio to initial state with audit | 120 / min / user |
| `GET` | `/api/admin/stats` | Concurrent platform trade volume & active metrics | 120 / min / user |
| `GET` | `/api/admin/metrics` | System operational metrics (requests, WS counts) | 120 / min / user |

---

## WebSocket Protocol

### 1. Market Price Stream (`/ws/prices?token={jwt}`)
- **Connection Quota:** Max 20 connection handshakes per minute per IP.
- **Message Quota:** Max 30 messages per 10-second rolling window; max 16 KB payload.
- **Actions:**
  - `{"action": "subscribe", "symbol": "BTCUSDT"}`
  - `{"action": "unsubscribe", "symbol": "BTCUSDT"}`
  - `{"action": "subscribe_watchlist"}`
  - `{"action": "ping"}` $\rightarrow$ returns `{"type": "pong"}`
- **Tick Frame:**
  ```json
  {
    "type": "price_tick",
    "symbol": "BTCUSDT",
    "price": "50230.50000000",
    "timestamp": 1700000000.123
  }
  ```

### 2. Private Portfolio Stream (`/ws/portfolio?token={jwt}`)
- **Snapshot on Connect:** Pushes full initial portfolio balance and holdings.
- **Trade Confirmation Frame:**
  ```json
  {
    "type": "trade_confirmation",
    "trade": {
      "id": "e7b1a2c3-4d5e-6f7a-8b9c-0d1e2f3a4b5c",
      "symbol": "BTCUSDT",
      "trade_type": "BUY",
      "quantity": "0.10000000",
      "price": "50230.50000000",
      "total_value": "5023.05000000",
      "executed_at": "2026-11-16T12:00:00.000Z"
    },
    "balance": "4976.95000000"
  }
  ```
- **Portfolio Update Frame:**
  ```json
  {
    "type": "portfolio_update",
    "virtual_balance": "4976.95000000",
    "total_portfolio_value": "10000.00000000",
    "total_pnl": "0.00000000",
    "holdings": [...]
  }
  ```

---

## Architectural & Design Decisions

1. **Decimal Everywhere (Zero Floats):**
   Financial systems must never use IEEE 754 binary floats due to representation drift. Every price, balance, and calculation uses Python's `Decimal` quantized to 8 decimal places matching PostgreSQL `Numeric(18, 8)`. Rounding modes follow institutional rules (`ROUND_HALF_UP` for debits, `ROUND_DOWN` for credits to users).
2. **Pessimistic Row Locking & Lock Order:**
   To prevent double-spend attacks and negative balances under extreme concurrency, trades acquire `SELECT ... FOR UPDATE` row locks. Locks are acquired in a strictly deterministic topological sequence (`users` row first, `holdings` row second). This completely eliminates database deadlocks.
3. **Decoupled In-Memory Price Registry vs Database:**
   Live ticks update an in-memory lock-free registry (`PriceRegistry`) directly from the WebSocket feed. Transactions read the latest valid price from memory without touching the database, keeping trade execution latency below 10 ms.
4. **Failure-Isolated Post-Commit Notifications:**
   WebSocket notifications (trade confirmations and portfolio updates) are dispatched *after* the PostgreSQL transaction commits. Notifications run in failure-isolated background tasks via Redis Pub/Sub; a transient network failure on a client WebSocket never rolls back an executed trade.
5. **Redis Pub/Sub Fan-Out:**
   Prices and events are published to Redis channels, allowing the application to scale horizontally across multiple Uvicorn workers. Each worker manages its own local client sockets without cross-process contention.
6. **Keyset (Cursor-Based) Pagination:**
   Trade history queries use `(executed_at, id)` keyset tuples rather than SQL `OFFSET / LIMIT`. This prevents performance degradation on large datasets and eliminates duplicate/skipped records when new trades occur while paging.
7. **Pure FIFO Accounting Function:**
   Realized PnL is computed via a pure, stateless function that matches sell orders against chronologically sorted buy lots. This isolates complex accounting math from database side effects and makes financial logic fully unit-testable.
8. **Stampede-Protected Leaderboard Cache:**
   The leaderboard query aggregates balances and holdings across all platform users. It is cached in Redis with a 30-second TTL and guarded by a distributed mutex lock to prevent database query stampedes during traffic spikes.
9. **Refresh Token Rotation with Reuse Revocation:**
   Refresh tokens are strictly single-use and tied to a family UUID. If a consumed refresh token is presented again (indicating token theft), the entire family is revoked immediately, locking out the adversary.

---

## Development & Testing

All testing commands run inside the isolated Docker container environment.

```bash
# Run complete test suite (241+ tests)
make test

# Run tests with verbose output
make test-verbose

# Run linting check (ruff)
make lint

# Apply database migrations
make migrate

# Generate new migration
make makemigrations m="add new index"

# Promote a user to admin
make seed-admin email="admin@example.com"
```

---

## Limitations & Future Roadmap

- **Simulated Order Book:** Currently executes orders at the top-of-book market ticker price. Future versions can simulate slippage and depth using full L2 order books.
- **Limit & Stop-Loss Orders:** The execution engine currently supports immediate spot market orders. An asynchronous order matching engine could evaluate resting limit and stop-loss orders against tick streams.
- **Asymmetric Key JWTs:** Production enterprise deployments can upgrade from HMAC-SHA256 (`HS256`) to asymmetric RS256/EdDSA keys managed by an external KMS.
