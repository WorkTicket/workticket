"""PostgreSQL Row-Level Security integration.

Sets the `app.current_tenant_id` runtime configuration parameter on each
database session to enable PostgreSQL RLS policy enforcement as a
defense-in-depth layer on top of the ORM-level tenant isolation.

The RLS policies (Alembic migrations 027, 033) use:
    USING (company_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
    WITH CHECK (company_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)

This means:
- When app.current_tenant_id IS SET to a valid UUID: rows are filtered to that tenant
- When app.current_tenant_id IS NULL/empty: ALL queries return 0 rows (strict blocking)
  This replaced the old auto-bypass behavior (migration 033 fix).
- Bypass mode: SET app.bypass_rls = 'true' enables internal operations
  (startup cleanup, beat tasks, migrations) to work without tenant context.

Blocking mode:
- RLS_BLOCK_UUID is the nil UUID (00000000-0000-0000-0000-000000000000).
  Setting app.current_tenant_id to this value blocks ALL queries because
  no row's company_id will match the nil UUID. This is used as a safety
  default in get_db() to ensure that endpoints using get_db() without
  proper authentication cannot access any data.

Usage:
    from app.db.rls import (
        set_rls_tenant_context, set_rls_block_context, set_rls_bypass_context,
        clear_rls_tenant_context, RLS_BLOCK_UUID,
    )

    # In FastAPI dependency that sets the tenant:
    set_rls_tenant_context(session, company_id)

    # For internal/background operations that need cross-tenant access:
    set_rls_bypass_context(session)

    # After the request:
    clear_rls_tenant_context(session)
"""

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

RLS_BLOCK_UUID = UUID("00000000-0000-0000-0000-000000000000")
RLS_BLOCK_UUID_STR = str(RLS_BLOCK_UUID)


async def set_rls_tenant_context(session: AsyncSession, tenant_id: UUID) -> None:
    """Set the RLS tenant context on a database session.

    Must be called after the session is created and before any queries
    on tenant-scoped tables. This causes PostgreSQL RLS policies to
    restrict all subsequent queries on this session to the given tenant.

    Args:
        session: An active AsyncSession
        tenant_id: The UUID of the company/tenant to scope to
    """
    try:
        await session.execute(text(f"SET app.current_tenant_id = '{tenant_id}'"))
    except Exception as e:
        logger.debug(
            "Failed to set RLS tenant context for %s: %s",
            tenant_id,
            e,
        )


async def set_rls_block_context(session: AsyncSession) -> None:
    """Set RLS context to the nil UUID, blocking ALL tenant-scoped queries.

    This is used as a safety default in get_db() when no tenant context
    has been set by the authentication dependency. The nil UUID won't match
    any row's company_id, so all tenant-scoped queries return zero rows.
    """
    try:
        await session.execute(text(f"SET app.current_tenant_id = '{RLS_BLOCK_UUID_STR}'"))
    except Exception as e:
        logger.debug("Failed to set RLS block context: %s", e)


async def set_rls_bypass_context(session: AsyncSession) -> None:
    """Set RLS bypass context, allowing cross-tenant access.

    Should only be used by internal/background operations (beat tasks,
    maintenance jobs, migrations) that legitimately need to access
    data across all tenants. Never exposed to API endpoints.
    """
    try:
        await session.execute(text("SET app.bypass_rls = 'true'"))
    except Exception as e:
        logger.debug("Failed to set RLS bypass context: %s", e)


async def clear_rls_tenant_context(session: AsyncSession) -> None:
    """Clear the RLS tenant context on a database session.

    Resets both app.current_tenant_id and app.bypass_rls to defaults.
    With the strict RLS policy (migration 033), an unset tenant context
    blocks ALL queries (returns 0 rows) rather than allowing bypass.
    Should be called after the tenant-scoped work is done to prevent
    session reuse issues.
    """
    try:
        await session.execute(text("RESET app.current_tenant_id"))
        await session.execute(text("RESET app.bypass_rls"))
    except Exception as e:
        logger.debug("Failed to clear RLS tenant context: %s", e)


async def get_rls_tenant_context(session: AsyncSession) -> str | None:
    """Get the current RLS tenant context from a database session.

    Returns the tenant ID string or None if not set.
    """
    try:
        result = await session.execute(text("SHOW app.current_tenant_id"))
        val = result.scalar()
        if val and val.strip():
            return val.strip()  # type: ignore[no-any-return]
    except Exception as e:
        logger.debug("Failed to read RLS tenant context: %s", e)
    return None
