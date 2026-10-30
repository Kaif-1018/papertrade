"""Admin router for privileged operations."""

from fastapi import APIRouter, Depends, status

from app.api.deps import require_admin
from app.models.user import User

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get(
    "/stats",
    status_code=status.HTTP_200_OK,
    summary="Get administrative system stats (Admin only)",
)
async def get_admin_stats(
    admin_user: User = Depends(require_admin),
) -> dict[str, str]:
    """Protected endpoint accessible only to users with the ADMIN role."""
    return {
        "status": "ok",
        "admin_email": admin_user.email,
        "message": "Admin access granted",
    }

