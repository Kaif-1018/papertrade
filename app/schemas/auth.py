"""Authentication request and response schemas."""

import re
from pydantic import BaseModel, Field, field_validator

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserRegisterRequest(BaseModel):
    """Payload for user registration."""

    email: str = Field(..., description="User's email address")
    password: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="Password must be at least 8 characters",
    )

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if not EMAIL_REGEX.match(cleaned):
            raise ValueError("Invalid email format")
        return cleaned


class UserLoginRequest(BaseModel):
    """Payload for user login."""

    email: str = Field(..., description="User's email address")
    password: str = Field(..., description="User's password")

    @field_validator("email")
    @classmethod
    def clean_email(cls, v: str) -> str:
        return v.strip().lower()


class RefreshTokenRequest(BaseModel):
    """Payload for token refresh and logout."""

    refresh_token: str = Field(..., description="JWT refresh token")


class TokenResponse(BaseModel):
    """Response containing access and refresh tokens."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"

