"""Shared FastAPI dependencies.

Provides get_db, get_current_user, require_role, and require_admin.
"""

from collections.abc import Callable
from uuid import UUID

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import ForbiddenError, TokenExpiredError, TokenInvalidError
from app.core.security import decode_token
from app.models.user import User, UserRole
from app.repositories.user import UserRepository

security = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    session: AsyncSession = Depends(get_db),
) -> User:
    """Extract, decode, and validate the JWT Bearer token, loading the User."""
    if credentials is None or not credentials.credentials:
        raise TokenInvalidError("Authentication credentials were not provided")

    token = credentials.credentials.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise TokenExpiredError("Token has expired")
    except jwt.InvalidTokenError:
        raise TokenInvalidError("Invalid token")

    if payload.get("type") != "access":
        raise TokenInvalidError("Token type must be access")

    user_id_str = payload.get("sub")
    if not user_id_str:
        raise TokenInvalidError("Token subject is missing")

    try:
        user_id = UUID(user_id_str)
    except (ValueError, TypeError):
        raise TokenInvalidError("Invalid user ID in token")

    user_repo = UserRepository(session)
    user = await user_repo.get_by_id(user_id)
    if user is None:
        raise TokenInvalidError("User not found")

    return user


def require_role(*roles: UserRole | str) -> Callable:
    """Dependency factory checking that the current user has one of the allowed roles."""
    allowed_roles = {
        r.value if isinstance(r, UserRole) else str(r) for r in roles
    }

    async def role_checker(
        current_user: User = Depends(get_current_user),
    ) -> User:
        user_role = (
            current_user.role.value
            if isinstance(current_user.role, UserRole)
            else str(current_user.role)
        )
        if user_role not in allowed_roles:
            raise ForbiddenError("You do not have permission to access this resource")
        return current_user

    return role_checker


require_admin = require_role(UserRole.ADMIN)

__all__ = [
    "get_current_user",
    "get_db",
    "require_admin",
    "require_role",
]
