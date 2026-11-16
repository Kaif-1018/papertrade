"""Application settings loaded from environment variables."""

from decimal import Decimal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All config is read from .env / environment variables."""

    # Environment & Deployment
    ENVIRONMENT: str = "development"  # development, test, staging, production
    TRUST_PROXY: bool = False
    LOG_FORMAT: str = "console"  # console, json
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://crypto:crypto@postgres:5432/crypto_trading"
    TEST_DATABASE_URL: str = "postgresql+asyncpg://crypto:crypto@postgres_test:5432/crypto_trading_test"

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # JWT
    JWT_SECRET: str = "change-me-to-a-random-secret"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_MINUTES: int = 30
    REFRESH_TOKEN_DAYS: int = 7

    # Trading & Price Feed
    DEFAULT_STARTING_BALANCE: Decimal = Decimal("10000")
    BINANCE_WS_URL: str = "wss://stream.binance.com/stream"
    PRICE_STALE_SECONDS: int = 10
    FEED_WATCHDOG_SECONDS: float = 15.0
    ENABLE_PRICE_FEED: bool = True

    # Security & Size Limits
    MAX_REQUEST_BODY_BYTES: int = 65536  # 64 KB
    WS_MAX_MESSAGE_BYTES: int = 16384  # 16 KB
    WS_MAX_MESSAGES_PER_WINDOW: int = 30
    WS_MESSAGE_WINDOW_SECONDS: float = 10.0
    WS_CONNECT_LIMIT_PER_MINUTE: int = 20

    # Stored as a raw CSV string to avoid pydantic-settings JSON coercion
    TRADING_PAIRS: str = "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,DOTUSDT"
    ALLOWED_ORIGINS: str = "*"
    WS_MAX_SUBSCRIPTIONS_PER_CLIENT: int = 20
    PORTFOLIO_REFRESH_INTERVAL_SECONDS: float = 5.0
    WATCHLIST_MAX_ITEMS: int = 25

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }

    @model_validator(mode="after")
    def validate_environment_security(self) -> "Settings":
        """Refuse to boot in non-dev environments with weak or placeholder JWT secret."""
        if self.ENVIRONMENT not in ("development", "test"):
            if len(self.JWT_SECRET) < 32 or self.JWT_SECRET in (
                "change-me-to-a-random-secret",
                "local-dev-secret-do-not-use-in-production",
            ):
                raise ValueError(
                    "JWT_SECRET must be at least 32 characters and cannot use default "
                    f"placeholders in non-development environment '{self.ENVIRONMENT}'."
                )
        return self

    @property
    def trading_pairs_list(self) -> list[str]:
        """Split the CSV into a proper list of uppercase symbols."""
        return [p.strip().upper() for p in self.TRADING_PAIRS.split(",") if p.strip()]

    @property
    def allowed_origins_list(self) -> list[str]:
        """Split ALLOWED_ORIGINS CSV into list."""
        if self.ALLOWED_ORIGINS.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    @field_validator("DEFAULT_STARTING_BALANCE", mode="before")
    @classmethod
    def parse_balance(cls, v: object) -> Decimal:
        """Build Decimal from string — never from float."""
        if isinstance(v, float):
            raise ValueError("DEFAULT_STARTING_BALANCE must not be a float")
        return Decimal(str(v))


def get_settings() -> Settings:
    """Factory so tests can monkeypatch before import time."""
    return Settings()
