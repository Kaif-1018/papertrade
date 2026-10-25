# Crypto Trading Simulator

Paper-trading simulator where users get a virtual USD balance and trade crypto at live Binance prices.

## Stack

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.0 (async + asyncpg)
- **Database:** PostgreSQL 16
- **Cache:** Redis 7
- **Auth:** PyJWT + bcrypt
- **Migrations:** Alembic (async)
- **Infrastructure:** Docker Compose

## Quick Start

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/Mac) or Docker Engine + Compose (Linux)

### Run the stack

```bash
# 1. Copy the example env file
cp .env.example .env    # or: copy .env.example .env  (Windows CMD)

# 2. Build and start all services
docker compose up --build
```

The API will be available at **http://localhost:8000**.

### Verify it works

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{
  "status": "ok",
  "services": {
    "postgres": "ok",
    "redis": "ok"
  }
}
```

### Useful commands

```bash
# Run tests
docker compose exec api pytest -q

# Apply Alembic migrations
docker compose exec api alembic upgrade head

# Generate a new migration
docker compose exec api alembic revision --autogenerate -m "describe change"

# Stop everything
docker compose down

# Stop and remove volumes (full reset)
docker compose down -v
```

## Project Structure

```
app/
  main.py              # App factory + lifespan
  core/                # Config, security, database, redis, logging
  models/              # SQLAlchemy models
  schemas/             # Pydantic request/response models
  repositories/        # DB access only, no business logic
  services/            # Business logic
  routers/             # HTTP and WebSocket endpoints
  api/deps.py          # Shared FastAPI dependencies
tests/                 # Test suite
alembic/               # Database migrations
```

## Configuration

All settings are loaded from environment variables (`.env` file). See [`.env.example`](.env.example) for the full list.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://crypto:crypto@postgres:5432/crypto_trading` | Async Postgres connection string |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection string |
| `JWT_SECRET` | — | Secret key for JWT signing (change in production!) |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm |
| `ACCESS_TOKEN_MINUTES` | `30` | Access token lifetime |
| `REFRESH_TOKEN_DAYS` | `7` | Refresh token lifetime |
| `DEFAULT_STARTING_BALANCE` | `10000` | Virtual USD balance for new users |
| `BINANCE_WS_URL` | `wss://stream.binance.com:9443/ws` | Binance WebSocket endpoint |
| `TRADING_PAIRS` | `BTCUSDT,ETHUSDT,...` | Comma-separated trading pairs |
