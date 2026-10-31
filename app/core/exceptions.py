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
    ) -> None:
        if message is not None:
            self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)


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
