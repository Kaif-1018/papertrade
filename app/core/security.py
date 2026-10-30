"""JWT helpers and password hashing.

Tokens carry sub, role, type (access|refresh), jti, iat, exp.
"""

from datetime import datetime, timedelta, timezone
import uuid

import bcrypt

# Fix passlib 1.7.4 compatibility with bcrypt >= 4.0.0
if not hasattr(bcrypt, "__about__"):
    class _About:
        __version__ = getattr(bcrypt, "__version__", "4.0.0")

    bcrypt.__about__ = _About()  # type: ignore[attr-defined]

import jwt
from passlib.context import CryptContext

from app.core.config import get_settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Return a bcrypt hash of *password*."""
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    """Verify *plain* against *hashed*."""
    return pwd_context.verify(plain, hashed)


def create_access_token(subject: str, role: str, jti: str | None = None) -> str:
    """Create a short-lived access JWT."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    token_jti = jti or str(uuid.uuid4())
    payload = {
        "sub": subject,
        "role": role,
        "type": "access",
        "jti": token_jti,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(
    subject: str, role: str, jti: str | None = None
) -> tuple[str, str]:
    """Create a longer-lived refresh JWT and return (token, jti)."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    token_jti = jti or str(uuid.uuid4())
    payload = {
        "sub": subject,
        "role": role,
        "type": "refresh",
        "jti": token_jti,
        "iat": now,
        "exp": now + timedelta(days=settings.REFRESH_TOKEN_DAYS),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, token_jti


def decode_token(token: str) -> dict:
    """Decode and verify a JWT. Raises jwt.exceptions on failure."""
    settings = get_settings()
    return jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
    )
