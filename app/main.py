"""FastAPI application factory and lifespan manager."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.logging import setup_logging
from app.core.redis import close_redis, init_redis
from app.routers import health

logger = structlog.stdlib.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: logging, Redis.  Shutdown: close Redis."""
    setup_logging()
    logger.info("starting_up")

    await init_redis()
    logger.info("redis_connected")

    yield

    await close_redis()
    logger.info("shutdown_complete")


def create_app() -> FastAPI:
    """Build and return the FastAPI instance."""
    app = FastAPI(
        title="Crypto Trading Simulator",
        description="Paper-trading simulator with live Binance prices",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(health.router)

    return app
