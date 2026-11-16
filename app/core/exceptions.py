"""Custom application exceptions and error types.

All custom exceptions inherit from AppException, specifying status_code,
error code string, and default message.
"""


class AppException(Exception):
    """Base application exception."""

    status_code: int = 500
    code: str = "INTERNAL_SERVER_ERROR"
    message: str = "An unexpected error occurred"

    def __init__(
        self,
        message: str | None = None,
        code: str | None = None,
        status_code: int | None = None,
        details: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details
        self.headers = headers
        super().__init__(self.message)


class RateLimitExceeded(AppException):
    """Raised when an IP or user exceeds the permitted request rate."""

    status_code = 429
    code = "RATE_LIMITED"
    message = "Too many requests. Please wait before retrying."

    def __init__(
        self,
        message: str | None = None,
        retry_after: int = 60,
        limit: int = 60,
        remaining: int = 0,
        reset: int = 0,
        details: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        merged_headers = {
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(reset),
        }
        if headers:
            merged_headers.update(headers)
        super().__init__(
            message=message or self.message,
            code=self.code,
            status_code=self.status_code,
            details=details,
            headers=merged_headers,
        )


class InvalidCredentialsError(AppException):
    """Raised when email/password verification fails."""

    status_code = 401
    code = "INVALID_CREDENTIALS"
    message = "Invalid email or password"


class TokenExpiredError(AppException):
    """Raised when an access or refresh token has expired."""

    status_code = 401
    code = "TOKEN_EXPIRED"
    message = "Token has expired"


class TokenInvalidError(AppException):
    """Raised when a token is malformed, has invalid claims, or fails validation."""

    status_code = 401
    code = "TOKEN_INVALID"
    message = "Invalid token"


class TokenReuseError(AppException):
    """Raised when an unlisted/rotated refresh token is reused, triggering full revocation."""

    status_code = 401
    code = "TOKEN_REUSE_DETECTED"
    message = "Refresh token reuse detected. All sessions revoked."


class UserAlreadyExistsError(AppException):
    """Raised on user registration with duplicate email."""

    status_code = 409
    code = "USER_ALREADY_EXISTS"
    message = "A user with this email already exists"


class ForbiddenError(AppException):
    """Raised when an authenticated user lacks permission/role."""

    status_code = 403
    code = "FORBIDDEN"
    message = "You do not have permission to access this resource"


class NotFoundError(AppException):
    """Raised when a requested resource is not found."""

    status_code = 404
    code = "NOT_FOUND"
    message = "Resource not found"


# ---------------------------------------------------------------------------
# Day 4: Price Feed & Registry Exceptions
# ---------------------------------------------------------------------------

class SymbolNotSupported(AppException):
    """Raised when a requested trading symbol is not in TRADING_PAIRS."""

    status_code = 404
    code = "SYMBOL_NOT_SUPPORTED"
    message = "Trading symbol not supported"


class PriceUnavailable(AppException):
    """Raised when a requested symbol has not received any price tick yet."""

    status_code = 503
    code = "PRICE_UNAVAILABLE"
    message = "Price is currently unavailable for this symbol"


class StalePrice(AppException):
    """Raised when the price for a symbol is older than PRICE_STALE_SECONDS."""

    status_code = 503
    code = "STALE_PRICE"
    message = "Price data is stale"


# ---------------------------------------------------------------------------
# Day 6: Trade Execution Exceptions
# ---------------------------------------------------------------------------

class InsufficientFunds(AppException):
    """Raised when a BUY trade requires more than the user's available balance."""

    status_code = 422
    code = "INSUFFICIENT_FUNDS"
    message = "Insufficient funds for this trade"


class InsufficientHoldings(AppException):
    """Raised when a SELL trade exceeds the user's holding quantity."""

    status_code = 422
    code = "INSUFFICIENT_HOLDINGS"
    message = "Insufficient holdings for this trade"


class InvalidQuantity(AppException):
    """Raised when a trade quantity violates business rules."""

    status_code = 422
    code = "INVALID_QUANTITY"
    message = "Invalid trade quantity"


# ---------------------------------------------------------------------------
# Day 10: Watchlist Exceptions
# ---------------------------------------------------------------------------

class WatchlistFullError(AppException):
    """Raised when a user attempts to exceed WATCHLIST_MAX_ITEMS."""

    status_code = 422
    code = "WATCHLIST_FULL"
    message = "Watchlist has reached maximum capacity"

    def __init__(self, max_items: int) -> None:
        super().__init__(
            message=f"Watchlist has reached maximum capacity of {max_items} items",
            code="WATCHLIST_FULL",
            status_code=422,
            details={"max": max_items},
        )


class NotInWatchlistError(AppException):
    """Raised when removing a symbol that is not in the user's watchlist."""

    status_code = 404
    code = "NOT_IN_WATCHLIST"
    message = "Symbol is not in your watchlist"


# ---------------------------------------------------------------------------
# Day 11: Trade History & Keyset Pagination Exceptions
# ---------------------------------------------------------------------------

class InvalidCursorError(AppException):
    """Raised when a pagination cursor cannot be decoded or is malformed."""

    status_code = 422
    code = "INVALID_CURSOR"
    message = "Invalid pagination cursor"


class InvalidDateRangeError(AppException):
    """Raised when from > to date filter range or date is invalid."""

    status_code = 422
    code = "INVALID_DATE_RANGE"
    message = "Date range is invalid: 'from' must be earlier than or equal to 'to'"


# ---------------------------------------------------------------------------
# Day 13: Admin & Trading Pair Management Exceptions
# ---------------------------------------------------------------------------

class PairExistsError(AppException):
    """Raised when adding a trading pair that already exists and is active."""

    status_code = 409
    code = "PAIR_EXISTS"
    message = "Trading pair already exists and is active"


class PairHasHoldingsError(AppException):
    """Raised when attempting to deactivate a trading pair that users currently hold without force=true."""

    status_code = 409
    code = "PAIR_HAS_HOLDINGS"
    message = "Cannot deactivate trading pair while users still hold open positions"

    def __init__(self, holders_count: int) -> None:
        super().__init__(
            message="Cannot deactivate trading pair while users still hold open positions",
            code="PAIR_HAS_HOLDINGS",
            status_code=409,
            details={"holders_count": holders_count},
        )


class PairNotFoundError(AppException):
    """Raised when a requested trading pair is not found."""

    status_code = 404
    code = "PAIR_NOT_FOUND"
    message = "Trading pair not found"


class BinanceUnreachableError(AppException):
    """Raised when Binance public REST API cannot be reached during symbol verification."""

    status_code = 503
    code = "BINANCE_UNREACHABLE"
    message = "Binance public API cannot be reached"


class InvalidBinanceSymbolError(AppException):
    """Raised when a symbol does not exist on Binance."""

    status_code = 422
    code = "INVALID_BINANCE_SYMBOL"
    message = "Symbol does not exist on Binance"

