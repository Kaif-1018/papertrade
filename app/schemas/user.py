"""User Pydantic schemas."""

from datetime import datetime
from decimal import Decimal
import uuid

from pydantic import BaseModel, ConfigDict, field_serializer

from app.models.user import UserRole


class UserResponse(BaseModel):
    """User response model omitting sensitive password hashes.

    Money fields are serialized as strings with exact 8-decimal precision.
    """

    id: uuid.UUID
    email: str
    role: UserRole
    virtual_balance: Decimal
    initial_balance: Decimal
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("virtual_balance", "initial_balance", mode="plain")
    def serialize_money(self, v: Decimal) -> str:
        """Serialize Decimals as 8-decimal strings in JSON."""
        return f"{v:.8f}"

