"""Command Line Interface for management commands."""

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import async_session_factory
from app.core.security import hash_password
from app.models.user import User, UserRole


async def create_admin(email: str, password: str) -> None:
    """Create or update a user to have the ADMIN role."""
    settings = get_settings()
    email_clean = email.strip().lower()

    async with async_session_factory() as session:
        result = await session.execute(select(User).where(User.email == email_clean))
        existing_user = result.scalar_one_or_none()

        if existing_user is not None:
            existing_user.role = UserRole.ADMIN
            existing_user.password_hash = hash_password(password)
            await session.commit()
            print(f"User '{email_clean}' updated to ADMIN successfully.")
            return

        new_admin = User(
            email=email_clean,
            password_hash=hash_password(password),
            role=UserRole.ADMIN,
            virtual_balance=settings.DEFAULT_STARTING_BALANCE,
            initial_balance=settings.DEFAULT_STARTING_BALANCE,
        )
        session.add(new_admin)
        await session.commit()
        print(f"Admin user '{email_clean}' created successfully.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto Trading Simulator CLI")
    subparsers = parser.add_subparsers(dest="command")

    admin_parser = subparsers.add_parser("create-admin", help="Seed or promote an admin user")
    admin_parser.add_argument(
        "--email",
        default="admin@example.com",
        help="Admin email (default: admin@example.com)",
    )
    admin_parser.add_argument(
        "--password",
        default="Admin123456!",
        help="Admin password (default: Admin123456!)",
    )

    args = parser.parse_args()
    if args.command == "create-admin":
        asyncio.run(create_admin(email=args.email, password=args.password))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

