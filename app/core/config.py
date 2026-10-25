"""Application settings loaded from environment variables."""

from decimal import Decimal
from functools import cached_property

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """All config is read from .env / environment variables."""

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://crypto:crypto@postgres:5432/crypto_trading"

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # JWT
    JWT_SECRET: str = "change-me-to-a-random-secret"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_MINUTES: int = 30
    REFRESH_TOKEN_DAYS: int = 7

    # Trading
    DEFAULT_STARTING_BALANCE: Decimal = Decimal("10000")
    BINANCE_WS_URL: str = "wss://stream.binance.com:9443/ws"

    # Stored as a raw CSV string to avoid pydantic-settings JSON coercion
    TRADING_PAIRS: str = "BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,ADAUSDT,DOGEUSDT,DOTUSDT"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
    }

    @property
    def trading_pairs_list(self) -> list[str]:
        """Split the CSV into a proper list."""
        return [p.strip() for p in self.TRADING_PAIRS.split(",") if p.strip()]

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
