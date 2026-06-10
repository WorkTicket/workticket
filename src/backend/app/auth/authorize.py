"""RBAC authorization dependency classes and decorators.

Provides centralized role-checking for FastAPI endpoints.
Replaces inline role checks (current_user.role not in (...)) with
reusable, auditable authorization gates.

Usage:
    from app.auth.authorize import RequireRoles, require_admin, require_staff, require_owner

    # As a FastAPI dependency (recommended - idiomatic FastAPI):
    @router.post("", dependencies=[Depends(require_admin)])
    async def admin_endpoint(...): ...

    # Or use dependency passthrough on endpoint signature:
    @router.post("")
    async def staff_endpoint(..., _: None = Depends(require_staff)): ...

    # Convenience aliases:
    #   require_admin  = RequireRoles("admin", "owner")
    #   require_owner  = RequireRoles("owner")
    #   require_staff  = RequireRoles("admin", "owner", "dispatcher")
"""

import logging
from collections.abc import Callable
from functools import wraps

from fastapi import Depends, HTTPException, Request, status

from app.auth.dependencies import get_current_user
from app.jobs.models import User, UserRole

logger = logging.getLogger(__name__)


class RequireRoles:
    """FastAPI dependency class that gates access by user role.

    Checks the authenticated user's role against the allowed set.
    Raises 401 if unauthenticated, 403 if unauthorized.

    Example:
        @router.post("/admin/refund", dependencies=[Depends(RequireRoles("owner", "admin"))])
        async def admin_refund(...): ...
    """

    def __init__(self, *allowed_roles: str):
        self.allowed_roles = set(allowed_roles)

    async def __call__(self, current_user: User = Depends(get_current_user)) -> None:
        if current_user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if current_user.role not in self.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current_user.role}' is not authorized for this operation",
            )


# Convenience aliases matching UserRole enum values
require_admin = RequireRoles(UserRole.admin.value, UserRole.owner.value)
require_owner = RequireRoles(UserRole.owner.value)
require_staff = RequireRoles(UserRole.admin.value, UserRole.owner.value, UserRole.dispatcher.value)
require_any_authenticated = RequireRoles(
    UserRole.owner.value,
    UserRole.admin.value,
    UserRole.dispatcher.value,
    UserRole.technician.value,
)


def require_roles_decorator(*allowed_roles: str) -> Callable:
    """Decorator to gate endpoints by user role.

    Wraps an endpoint function so that current_user (resolved by FastAPI
    dependency injection) is checked against allowed_roles before the
    endpoint executes. Behaves identically to the RequireRoles dependency
    class but is applied as a decorator.

    Use the dependency class (RequireRoles with Depends) for new code;
    this decorator exists for backward compatibility and cases where
    wrapping the function directly is preferred.
    """
    allowed_set = set(allowed_roles)

    def decorator(endpoint_func: Callable) -> Callable:
        @wraps(endpoint_func)
        async def wrapper(*args, **kwargs):
            current_user: User = kwargs.get("current_user")
            if current_user is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if current_user.role not in allowed_set:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Role '{current_user.role}' is not authorized for this operation",
                )
            return await endpoint_func(*args, **kwargs)

        return wrapper

    return decorator


# Route tag to role mapping for global RBAC enforcement middleware
_ROUTE_TAG_ROLE_MAP = {
    "admin": {UserRole.admin.value, UserRole.owner.value},
    "staff": {UserRole.admin.value, UserRole.owner.value, UserRole.dispatcher.value},
    "billing": {UserRole.admin.value, UserRole.owner.value, UserRole.dispatcher.value},
}


async def enforce_route_rbac(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> None:
    """Global RBAC enforcement based on route tags.

    Checks the route's OpenAPI tags against the user's role.
    Routes tagged "admin" require admin/owner.
    Routes tagged "staff" require staff (admin/owner/dispatcher).
    Routes tagged "public" are exempt.
    Falls back to require_any_authenticated for all other routes under /api/v1.
    """
    route = getattr(request, "scope", {}).get("route")
    if route is None:
        return

    tags = set(getattr(route, "tags", []) or [])
    path = getattr(route, "path", "")

    if "public" in tags:
        return

    for tag, allowed in _ROUTE_TAG_ROLE_MAP.items():
        if tag in tags:
            if current_user.role not in allowed:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Route tag '{tag}' requires one of roles: {', '.join(sorted(allowed))}",
                )
            return

    # For all other API routes, require at minimum an authenticated user
    if path.startswith("/api/") and current_user.role not in {
        UserRole.owner.value,
        UserRole.admin.value,
        UserRole.dispatcher.value,
        UserRole.technician.value,
    }:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{current_user.role}' not authorized",
        )
