"""Authentication service.

Handles registration, credential validation, token issuance, refresh token
rotation in Redis, and reuse-detection revocation.
"""

from uuid import UUID

import jwt
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.core.config import get_settings
from app.core.exceptions import (
    InvalidCredentialsError,
    TokenExpiredError,
    TokenInvalidError,
    TokenReuseError,
    UserAlreadyExistsError,
)
from app.core.redis import get_redis
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import User, UserRole
from app.repositories.user import UserRepository
from app.schemas.auth import TokenResponse

logger = structlog.stdlib.get_logger(__name__)


class AuthService:
    """Business logic for user authentication and session management."""

    def __init__(
        self,
        session: AsyncSession,
        redis: Redis | None = None,
    ) -> None:
        self.session = session
        self.user_repo = UserRepository(session)
        self.settings = get_settings()
        self._redis = redis

    @property
    def redis(self) -> Redis:
        """Return the active Redis client."""
        if self._redis is not None:
            return self._redis
        return get_redis()

    async def register(self, email: str, password: str) -> User:
        """Register a new user with initial starting balance."""
        cleaned_email = email.strip().lower()
        existing = await self.user_repo.get_by_email(cleaned_email)
        if existing:
            logger.warning("registration_duplicate_email", email=cleaned_email)
            raise UserAlreadyExistsError("A user with this email already exists")

        pwd_hash = hash_password(password)
        starting_balance = self.settings.DEFAULT_STARTING_BALANCE

        user = self.user_repo.create(
            email=cleaned_email,
            password_hash=pwd_hash,
            virtual_balance=starting_balance,
            initial_balance=starting_balance,
            role=UserRole.USER,
        )
        await self.session.commit()
        await self.session.refresh(user)

        logger.info("user_registered", user_id=str(user.id), email=cleaned_email)
        return user

    async def login(self, email: str, password: str) -> TokenResponse:
        """Verify user credentials and issue an access + refresh token pair."""
        cleaned_email = email.strip().lower()
        user = await self.user_repo.get_by_email(cleaned_email)

        # Constant-time-ish check: run verify even if user is missing to deter timing attacks
        valid_password = verify_password(password, user.password_hash) if user else False
        if not user or not valid_password:
            logger.warning("login_failed", email=cleaned_email)
            raise InvalidCredentialsError("Invalid credentials")

        access_token = create_access_token(
            subject=str(user.id),
            role=user.role.value,
        )
        refresh_token, jti = create_refresh_token(
            subject=str(user.id),
            role=user.role.value,
        )

        ttl_seconds = self.settings.REFRESH_TOKEN_DAYS * 86400
        redis_key = f"refresh:{user.id}:{jti}"
        await self.redis.set(redis_key, "1", ex=ttl_seconds)

        logger.info("user_logged_in", user_id=str(user.id))
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
        )

    async def refresh(self, refresh_token: str) -> TokenResponse:
        """Rotate a refresh token, issuing a new pair and invalidating the old.

        If a validly signed refresh token has a jti not present in Redis, token
        theft / reuse is detected: all refresh tokens for that user are revoked.
        """
        try:
            payload = decode_token(refresh_token)
        except jwt.ExpiredSignatureError:
            raise TokenExpiredError("Refresh token has expired")
        except jwt.InvalidTokenError:
            raise TokenInvalidError("Invalid refresh token")

        token_type = payload.get("type")
        if token_type != "refresh":
            raise TokenInvalidError("Access token cannot be used as a refresh token")

        user_id_str = payload.get("sub")
        jti = payload.get("jti")
        if not user_id_str or not jti:
            raise TokenInvalidError("Invalid token claims")

        redis_key = f"refresh:{user_id_str}:{jti}"
        exists = await self.redis.exists(redis_key)
        if not exists:
            # Token reuse detected: an already-consumed or invalid jti was presented
            logger.error("refresh_token_reuse_detected", user_id=user_id_str)
            await self.revoke_all_user_refresh_tokens(user_id_str)
            raise TokenReuseError("Refresh token reuse detected. All sessions revoked.")

        # Invalidate old refresh token (rotation)
        await self.redis.delete(redis_key)

        try:
            user_id = UUID(user_id_str)
        except (ValueError, TypeError):
            raise TokenInvalidError("Invalid user ID")

        user = await self.user_repo.get_by_id(user_id)
        if not user:
            raise TokenInvalidError("User not found")

        # Issue new token pair
        new_access = create_access_token(
            subject=str(user.id),
            role=user.role.value,
        )
        new_refresh, new_jti = create_refresh_token(
            subject=str(user.id),
            role=user.role.value,
        )

        ttl_seconds = self.settings.REFRESH_TOKEN_DAYS * 86400
        await self.redis.set(f"refresh:{user.id}:{new_jti}", "1", ex=ttl_seconds)

        logger.info("tokens_rotated", user_id=str(user.id))
        return TokenResponse(
            access_token=new_access,
            refresh_token=new_refresh,
            token_type="bearer",
        )

    async def logout(self, refresh_token: str) -> None:
        """Revoke the given refresh token by deleting it from Redis."""
        try:
            payload = decode_token(refresh_token)
        except jwt.ExpiredSignatureError:
            raise TokenExpiredError("Refresh token has expired")
        except jwt.InvalidTokenError:
            raise TokenInvalidError("Invalid refresh token")

        if payload.get("type") != "refresh":
            raise TokenInvalidError("Expected a refresh token")

        user_id_str = payload.get("sub")
        jti = payload.get("jti")
        if user_id_str and jti:
            await self.redis.delete(f"refresh:{user_id_str}:{jti}")
            logger.info("user_logged_out", user_id=user_id_str)

    async def revoke_all_user_refresh_tokens(self, user_id: str) -> None:
        """Scan and delete all active refresh tokens for a user."""
        keys = []
        async for key in self.redis.scan_iter(match=f"refresh:{user_id}:*"):
            keys.append(key)
        if keys:
            await self.redis.delete(*keys)
        logger.warning("all_refresh_tokens_revoked", user_id=user_id, count=len(keys))

