"""UserRepository — DB access for the users table."""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole
from app.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    """Queries scoped to the users table."""

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(User, session)

    async def get_by_email(self, email: str) -> User | None:
        """Return the user with *email*, or None."""
        result = await self._session.execute(
            select(User).where(User.email == email)
        )
        return result.scalar_one_or_none()

    def create(
        self,
        email: str,
        password_hash: str,
        virtual_balance: Decimal,
        initial_balance: Decimal,
        role: UserRole = UserRole.USER,
    ) -> User:
        """Stage a new User instance for INSERT (does not commit)."""
        user = User(
            email=email,
            password_hash=password_hash,
            role=role,
            virtual_balance=virtual_balance,
            initial_balance=initial_balance,
        )
        self.add(user)
        return user
