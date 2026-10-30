"""Authentication router."""

from fastapi import APIRouter, Depends, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.redis import get_redis
from app.models.user import User
from app.schemas.auth import (
    RefreshTokenRequest,
    TokenResponse,
    UserLoginRequest,
    UserRegisterRequest,
)
from app.schemas.user import UserResponse
from app.services.auth_service import AuthService

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
)
async def register(
    data: UserRegisterRequest,
    session: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> UserResponse:
    """Register a new user with default initial balance."""
    service = AuthService(session, redis)
    user = await service.register(email=data.email, password=data.password)
    return UserResponse.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="User login",
)
async def login(
    data: UserLoginRequest,
    session: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> TokenResponse:
    """Authenticate user and return an access and refresh token pair."""
    service = AuthService(session, redis)
    tokens = await service.login(email=data.email, password=data.password)
    return tokens


@router.post(
    "/refresh",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Refresh token rotation",
)
async def refresh(
    data: RefreshTokenRequest,
    session: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> TokenResponse:
    """Rotate a refresh token for a new access and refresh token pair."""
    service = AuthService(session, redis)
    tokens = await service.refresh(refresh_token=data.refresh_token)
    return tokens


@router.post(
    "/logout",
    status_code=status.HTTP_200_OK,
    summary="User logout",
)
async def logout(
    data: RefreshTokenRequest,
    session: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, str]:
    """Revoke a refresh token."""
    service = AuthService(session, redis)
    await service.logout(refresh_token=data.refresh_token)
    return {"message": "Logged out successfully"}


@router.get(
    "/me",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Get current user profile",
)
async def me(
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    """Return the currently authenticated user."""
    return UserResponse.model_validate(current_user)

