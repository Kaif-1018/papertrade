"""Application settings loaded from environment variables."""

from decimal import Decimal

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All config is read from .env / environment variables."""

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

    # Stored as a raw CSV string to avoid pydantic-settings JSON coercion
    TRADING_PAIRS: str = "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,DOTUSDT"
    ALLOWED_ORIGINS: str = "*"
    WS_MAX_SUBSCRIPTIONS_PER_CLIENT: int = 20

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }

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
