"""FastAPI application factory and lifespan manager."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
import structlog

from app.core.config import get_settings
from app.core.exceptions import AppException
from app.core.logging import setup_logging
from app.core.redis import close_redis, init_redis
from app.routers import admin, auth, health, prices
from app.services.price_feed import PriceFeedService

logger = structlog.stdlib.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: logging, Redis, price feed. Shutdown: stop feed, close Redis."""
    setup_logging()
    logger.info("starting_up")

    await init_redis()
    logger.info("redis_connected")

    settings = get_settings()
    feed_service: PriceFeedService | None = None
    feed_task: asyncio.Task[None] | None = None

    if settings.ENABLE_PRICE_FEED:
        feed_service = PriceFeedService(
            url_base=settings.BINANCE_WS_URL,
            symbols=settings.trading_pairs_list,
        )
        feed_task = asyncio.create_task(feed_service.run())
        app.state.price_feed = feed_service
        app.state.price_feed_task = feed_task
        logger.info("price_feed_task_started")

    yield

    if feed_service is not None:
        await feed_service.stop()

    if feed_task is not None:
        feed_task.cancel()
        try:
            await feed_task
        except asyncio.CancelledError:
            pass

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

    # Global Exception Handlers
    @app.exception_handler(AppException)
    async def handle_app_exception(
        request: Request, exc: AppException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_exception(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors()
        message = errors[0].get("msg", "Validation error") if errors else "Validation error"
        if errors and "loc" in errors[0]:
            loc_str = " -> ".join(str(loc) for loc in errors[0]["loc"] if loc != "body")
            if loc_str:
                message = f"{loc_str}: {message}"
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "VALIDATION_ERROR", "message": message}},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code_map = {
            401: "UNAUTHORIZED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
        }
        code = code_map.get(exc.status_code, "HTTP_ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": str(exc.detail)}},
        )

    # Routers
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(prices.router)

    return app
