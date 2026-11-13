"""WebSocket authentication and Origin validation.

Security Note:
Passing JWT tokens in query parameters (?token=<jwt>) is standard for browser
WebSocket clients because the native JavaScript WebSocket API does not allow
setting custom Authorization headers. However, query parameters are susceptible
to appearing in HTTP server access logs and proxy logs. Production reverse proxies
and access loggers MUST sanitize or redact query strings for /ws/ routes.

Handshake Close Strategy:
We choose **accept-then-close** with code 1008 (Policy Violation).
Under RFC 6455 and browser WebSocket implementations, closing *before* accept causes
browser clients to trigger a generic 1006 (Abnormal Closure) error, hiding the close
code and reason string from event.code and event.reason. By accepting the handshake
first and immediately issuing a Close frame, clients receive the exact 1008 code
and the human-readable explanation to trigger re-authentication.
"""

from uuid import UUID

from fastapi import WebSocket
import jwt

from app.core.config import get_settings
from app.core.security import decode_token


class WebSocketAuthError(Exception):
    """Raised when WebSocket authentication or Origin check fails."""

    def __init__(self, reason: str, code: int = 1008) -> None:
        self.reason = reason
        self.code = code
        super().__init__(reason)


async def authenticate_websocket_with_claims(
    websocket: WebSocket,
) -> tuple[UUID, str]:
    """Validate Origin and JWT access token from query parameters.

    Returns a tuple of (user_id: UUID, role: str).
    Closes the connection with code 1008 and raises WebSocketAuthError on failure.
    """
    settings = get_settings()

    # 1. Validate Origin header
    origin = websocket.headers.get("origin")
    if origin and settings.allowed_origins_list != ["*"]:
        if origin not in settings.allowed_origins_list:
            await websocket.accept()
            await websocket.close(code=1008, reason="Origin not allowed")
            raise WebSocketAuthError("Origin not allowed")

    # 2. Extract token from query parameter ?token=<access_token>
    token = websocket.query_params.get("token")
    if not token:
        await websocket.accept()
        await websocket.close(code=1008, reason="Missing authentication token")
        raise WebSocketAuthError("Missing authentication token")

    # 3. Decode and verify JWT signature & expiration
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        await websocket.accept()
        await websocket.close(code=1008, reason="Token has expired")
        raise WebSocketAuthError("Token has expired")
    except jwt.InvalidTokenError:
        await websocket.accept()
        await websocket.close(code=1008, reason="Invalid token")
        raise WebSocketAuthError("Invalid token")

    # 4. Require access token type
    if payload.get("type") != "access":
        await websocket.accept()
        await websocket.close(code=1008, reason="Token type must be access")
        raise WebSocketAuthError("Token type must be access")

    user_id_str = payload.get("sub")
    if not user_id_str:
        await websocket.accept()
        await websocket.close(code=1008, reason="Token subject is missing")
        raise WebSocketAuthError("Token subject is missing")

    try:
        user_id = UUID(user_id_str)
    except (ValueError, TypeError):
        await websocket.accept()
        await websocket.close(code=1008, reason="Invalid user ID in token")
        raise WebSocketAuthError("Invalid user ID in token")

    role = str(payload.get("role", "USER")).upper()
    return user_id, role


async def authenticate_websocket(websocket: WebSocket) -> UUID:
    """Validate Origin and JWT access token from query parameters.

    Returns the authenticated user's UUID.
    Closes the connection with code 1008 and raises WebSocketAuthError on failure.
    """
    user_id, _ = await authenticate_websocket_with_claims(websocket)
    return user_id

