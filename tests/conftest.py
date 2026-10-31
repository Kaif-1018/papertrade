"""Shared test fixtures.

The test suite connects to the separate `crypto_trading_test` database
(postgres_test service, port 5433).  Each test function gets a fresh
transaction that is rolled back at the end — no teardown SQL needed.
"""

import asyncio
from collections.abc import AsyncGenerator
import json

from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    AsyncTransaction,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
import websockets
from websockets.server import WebSocketServerProtocol

from alembic import command
from alembic.config import Config

from app.api.deps import get_db
from app.core.config import get_settings
from app.core.redis import close_redis, get_redis, init_redis
from app.main import create_app
from app.models.base import Base  # noqa: F401 — ensures metadata is populated
import app.models  # noqa: F401 — registers all tables on Base.metadata


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(scope="session")
def test_engine(settings):
    """Async engine pointing at the *test* database.

    Using NullPool ensures each connection is created in the caller's active
    event loop, avoiding asyncpg cross-loop Future attachment errors.
    """
    engine = create_async_engine(
        settings.TEST_DATABASE_URL,
        poolclass=NullPool,
        echo=False,
    )
    yield engine


@pytest.fixture(scope="session", autouse=True)
def apply_migrations(settings):
    """Apply all Alembic migrations to the test DB once per session."""
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", settings.TEST_DATABASE_URL)

    command.upgrade(alembic_cfg, "head")
    yield
    command.downgrade(alembic_cfg, "base")


# ---------------------------------------------------------------------------
# Per-test transaction rollback — keeps tests isolated without DROP/CREATE
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def db_conn(test_engine) -> AsyncGenerator[AsyncConnection, None]:
    """Yield a connection whose transaction is always rolled back."""
    async with test_engine.connect() as conn:
        txn: AsyncTransaction = await conn.begin()
        yield conn
        await txn.rollback()


@pytest_asyncio.fixture()
async def db_session(db_conn: AsyncConnection) -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession bound to the rollback-wrapped connection."""
    session_factory = async_sessionmaker(
        bind=db_conn,
        class_=AsyncSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    async with session_factory() as session:
        yield session


# ---------------------------------------------------------------------------
# Redis and HTTP Client fixtures for integration tests
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def test_redis() -> AsyncGenerator[Redis, None]:
    """Ensure Redis is connected and cleared before and after each test."""
    await init_redis()
    client = get_redis()
    await client.flushdb()
    yield client
    await client.flushdb()
    await close_redis()


@pytest_asyncio.fixture()
async def app_instance(db_session: AsyncSession, test_redis: Redis):
    """FastAPI test app instance with overridden DB and Redis dependencies."""
    app = create_app()

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = lambda: test_redis
    return app


@pytest_asyncio.fixture()
async def client(app_instance) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client bound to the test FastAPI application."""
    async with AsyncClient(
        transport=ASGITransport(app=app_instance),
        base_url="http://test",
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Mock Binance WebSocket Server Fixture
# ---------------------------------------------------------------------------

class MockBinanceServer:
    """In-memory WebSocket server emulating Binance ticker feeds."""

    def __init__(self) -> None:
        self.server: Any = None
        self.port: int = 0
        self.connections: set[WebSocketServerProtocol] = set()
        self.received_messages: list[str] = []
        self._connected_event = asyncio.Event()

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def handler(self, websocket: WebSocketServerProtocol) -> None:
        self.connections.add(websocket)
        self._connected_event.set()
        try:
            async for raw in websocket:
                text = raw if isinstance(raw, str) else raw.decode("utf-8")
                self.received_messages.append(text)
                try:
                    data = json.loads(text)
                    if "method" in data and "id" in data:
                        ack = json.dumps({"result": None, "id": data["id"]})
                        await websocket.send(ack)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self.connections.discard(websocket)

    async def broadcast_tick(
        self,
        symbol: str,
        price: str,
        epoch_ms: int = 1710000000000,
    ) -> None:
        """Broadcast a ticker frame matching Binance combined stream format."""
        payload = {
            "stream": f"{symbol.lower()}@ticker",
            "data": {
                "e": "24hrTicker",
                "E": epoch_ms,
                "s": symbol.upper(),
                "c": price,
            },
        }
        await self.broadcast(json.dumps(payload))

    async def broadcast(self, raw: str) -> None:
        """Send a raw text frame to all active client connections."""
        if self.connections:
            await asyncio.gather(
                *(c.send(raw) for c in list(self.connections)),
                return_exceptions=True,
            )

    async def drop_connections(
        self, code: int = 1000, reason: str = "Mock drop"
    ) -> None:
        """Force-close all connected clients."""
        active = list(self.connections)
        self.connections.clear()
        for c in active:
            try:
                await c.close(code=code, reason=reason)
            except Exception:
                pass

    async def wait_for_connection(self, timeout: float = 3.0) -> None:
        """Wait until at least one client has connected."""
        if not self.connections:
            self._connected_event.clear()
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)


@pytest_asyncio.fixture()
async def mock_binance() -> AsyncGenerator[MockBinanceServer, None]:
    """Yields a running local mock Binance WebSocket server on an ephemeral port."""
    mock = MockBinanceServer()
    server = await websockets.serve(mock.handler, "127.0.0.1", 0)
    mock.server = server
    mock.port = server.sockets[0].getsockname()[1]

    yield mock

    await mock.drop_connections()
    server.close()
    await server.wait_closed()
