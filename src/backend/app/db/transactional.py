import asyncio
import functools
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import (
    MAX_RETRIES,
    RETRY_BACKOFF,
    AsyncSessionLocal,
    DBUnavailableError,
    _check_db_circuit,
    _record_pool_exhaustion,
    is_retryable_error,
)
from app.db.rls import clear_rls_tenant_context, set_rls_block_context, set_rls_bypass_context, set_rls_tenant_context
from app.db.tenant_context import get_current_tenant_id

logger = logging.getLogger(__name__)


async def _propagate_rls_context(source_session: AsyncSession, target_session: AsyncSession):
    """Propagate RLS context from source session to target session."""
    try:
        bypass_val = None
        try:
            bypass_res = await source_session.execute(__import__("sqlalchemy").text("SHOW app.bypass_rls"))
            bypass_val = bypass_res.scalar()
        except Exception:
            pass
        if bypass_val and bypass_val.strip().lower() == "true":
            await set_rls_bypass_context(target_session)
            return
        tenant_id = get_current_tenant_id()
        if tenant_id:
            await set_rls_tenant_context(target_session, tenant_id)
        else:
            await set_rls_block_context(target_session)
    except Exception as _e:
        logger.debug("Failed to propagate RLS context to transactional session: %s", _e)


def transactional(max_retries: int = MAX_RETRIES):
    """Decorator that wraps an async FastAPI route handler with a managed transaction.

    Provides:
    - Session creation and injection with RLS context propagation
    - Auto-commit on success / rollback on exception
    - Retry logic for transient DB errors
    - Proper session cleanup in all cases

    CRITICAL FIX: Uses the existing session from get_db() (with RLS context)
    instead of creating a bare session that bypasses Row-Level Security.
    Falls back to creating a new session with RLS propagation if no session
    exists in kwargs.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            _check_db_circuit()
            last_exc = None
            original_db = kwargs.get("db")
            for attempt in range(max_retries + 1):
                if original_db is not None:
                    session = original_db
                else:
                    session = AsyncSessionLocal()
                    tenant_id = get_current_tenant_id()
                    if tenant_id:
                        await set_rls_tenant_context(session, tenant_id)
                    else:
                        await set_rls_block_context(session)
                try:
                    kwargs["db"] = session
                    result = await func(*args, **kwargs)
                    await session.commit()
                    return result
                except Exception as exc:
                    await session.rollback()
                    exc_str = str(exc)
                    if "timeout" in exc_str.lower() and "pool" in exc_str.lower():
                        _record_pool_exhaustion()
                        raise DBUnavailableError("Database connection pool exhausted") from exc
                    if attempt < max_retries and is_retryable_error(exc):
                        last_exc = exc
                        await asyncio.sleep(RETRY_BACKOFF[attempt])
                        continue
                    raise
                finally:
                    if original_db is None:
                        await clear_rls_tenant_context(session)
                        await session.close()
            if last_exc:
                raise DBUnavailableError("DB operation failed after retries") from last_exc

        return wrapper

    return decorator
