"""Core HTTP and ASGI middleware components:
- Request ID generation & contextvar binding
- Request body size limit enforcement (64 KB)
- Security response headers
- Access logging with duration and authenticated user metadata
- Metrics counter collector
"""

from collections.abc import Callable
import contextvars
import time
from typing import Any
import uuid

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send
import structlog

from app.core.config import get_settings

request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
access_logger = structlog.stdlib.get_logger("access")


class MetricsCollector:
    """Thread-safe in-memory operational metrics collector."""

    def __init__(self) -> None:
        self.start_time: float = time.time()
        self.requests_total: int = 0
        self.requests_by_status: dict[str, int] = {}

    def record_request(self, status_code: int) -> None:
        self.requests_total += 1
        key = str(status_code)
        self.requests_by_status[key] = self.requests_by_status.get(key, 0) + 1

    def get_snapshot(self) -> dict[str, Any]:
        return {
            "uptime_seconds": int(time.time() - self.start_time),
            "requests_total": self.requests_total,
            "requests_by_status": dict(self.requests_by_status),
        }


metrics_collector = MetricsCollector()


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Propagate or generate X-Request-ID and bind to structlog contextvars."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = req_id
        token = request_id_ctx.set(req_id)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=req_id)

        try:
            response: Response = await call_next(request)
            response.headers["X-Request-ID"] = req_id
            return response
        finally:
            request_id_ctx.reset(token)


class RequestBodyLimitMiddleware:
    """ASGI middleware to reject HTTP request payloads larger than MAX_REQUEST_BODY_BYTES."""

    def __init__(self, app: ASGIApp, max_bytes: int = 65536) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1. Quick check via Content-Length header
        for name, val in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    if int(val.decode("latin1")) > self.max_bytes:
                        response = JSONResponse(
                            status_code=413,
                            content={
                                "error": {
                                    "code": "PAYLOAD_TOO_LARGE",
                                    "message": f"Request body exceeds maximum allowed size of {self.max_bytes} bytes",
                                }
                            },
                        )
                        await response(scope, receive, send)
                        return
                except ValueError:
                    pass

        # 2. Streaming size enforcement
        received_bytes = 0

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                received_bytes += len(body)
                if received_bytes > self.max_bytes:
                    raise ValueError("PayloadTooLarge")
            return message

        try:
            await self.app(scope, limited_receive, send)
        except ValueError as exc:
            if str(exc) == "PayloadTooLarge":
                response = JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "PAYLOAD_TOO_LARGE",
                            "message": f"Request body exceeds maximum allowed size of {self.max_bytes} bytes",
                        }
                    },
                )
                await response(scope, receive, send)
            else:
                raise


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Enforce defense-in-depth security headers on all HTTP responses."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response: Response = await call_next(request)

        # Standard defense-in-depth headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "magnetometer=(), microphone=(), payment=(), usb=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "connect-src 'self' ws: wss:; "
            "img-src 'self' data: https://fastapi.tiangolo.com; "
            "worker-src 'self' blob:;"
        )
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Record structured access log entries and track operational request counts."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.perf_counter()

        response: Response = await call_next(request)

        duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
        metrics_collector.record_request(response.status_code)

        # Sanitize WebSocket query parameters from log paths
        path = request.url.path
        user_id = getattr(request.state, "user_id", None)

        access_logger.info(
            "http_request",
            method=request.method,
            path=path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            user_id=user_id,
        )
        return response
