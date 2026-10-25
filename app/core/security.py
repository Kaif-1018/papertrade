"""JWT helpers and password hashing.

Tokens carry sub, role, type (access|refresh), jti, iat, exp.
"""

from datetime import datetime, timedelta, timezone

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


def create_access_token(subject: str, role: str, jti: str) -> str:
    """Create a short-lived access JWT."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "role": role,
        "type": "access",
        "jti": jti,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_MINUTES),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(subject: str, role: str, jti: str) -> str:
    """Create a longer-lived refresh JWT."""
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "role": role,
        "type": "refresh",
        "jti": jti,
        "iat": now,
        "exp": now + timedelta(days=settings.REFRESH_TOKEN_DAYS),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and verify a JWT. Raises jwt.exceptions on failure."""
    settings = get_settings()
    return jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
    )
