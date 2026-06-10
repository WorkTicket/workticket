import asyncio
import contextvars
import hashlib
import logging
import os
import re
import threading
import time
from collections import Counter
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import Delete, Update, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings
from app.db.tenant_context import get_current_tenant_id

logger = logging.getLogger(__name__)

settings = get_settings()

_TENANT_SCOPED_TABLES: set[str] = {
    "users",
    "customers",
    "jobs",
    "job_media",
    "ai_outputs",
    "quotes",
    "billing_accounts",
    "usage_ledger",
    "invoices",
    "company_pricing_brains",
    "services",
    "estimates",
    "estimate_line_items",
    "historical_job_data",
    "ai_job_estimates",
    "notifications",
    "push_tokens",
    "analytics_events",
    "idempotency_keys",
    "job_audit_logs",
    "billing_audit_logs",
    "ai_output_feedback",
    "execution_traces",
    "estimate_audit_logs",
    "user_audit_logs",
    "integration_connections",
    "import_jobs",
    "import_logs",
    "mapping_rules",
}

# C-1 FIX: Working tenant isolation via do_orm_execute event.
# Automatically appends company_id = :tenant_id to all SELECT/UPDATE/DELETE
# on tenant-scoped tables when the tenant context is set.
# Skips INSERT (company_id is set explicitly on creation) and DDL statements.
# Skips queries that already have a company_id filter (detected via WHERE clause inspection).
# Skips queries for tables not in _TENANT_SCOPED_TABLES.


def _query_has_company_id(clause) -> bool:
    """Check if query already filters by company_id."""
    try:
        raw = str(clause.compile(compile_kwargs={"literal_binds": False})).lower()
        if "company_id" not in raw:
            return False
        if "company_id = " in raw or "company_id=" in raw or "company_id in" in raw or "company_id IN" in raw:
            return True
        if "company_id =" in raw:
            return True
    except Exception as _e:
        logger.debug("Query company_id check failed: %s", _e)
    return False


def _get_entity_table_name(orm_execute_state) -> str | None:
    """Extract table name from the ORM execute state's entity descriptions."""
    try:
        for desc in orm_execute_state.column_expressions:
            if hasattr(desc, "table") and hasattr(desc.table, "name"):
                return desc.table.name
        for desc in orm_execute_state.column_expressions:
            if hasattr(desc, "entity_namespace"):
                entity = desc.entity_namespace
                if hasattr(entity, "__tablename__"):
                    return entity.__tablename__
    except Exception as _e:
        logger.debug("Failed to extract entity table name (column expressions): %s", _e)
    try:
        statement = orm_execute_state.statement
        if hasattr(statement, "froms") and statement.froms:
            for f in statement.froms:
                if hasattr(f, "name"):
                    return f.name
    except Exception as _e:
        logger.debug("Failed to extract entity table name (froms): %s", _e)
    return None


def _register_tenant_isolation_listener():
    """Register do_orm_execute event to auto-scope company_id on tenant-scoped queries.

    Uses do_orm_execute on Session (sync) to automatically append
    company_id = :tenant_id to all SELECT, UPDATE, DELETE queries on
    tenant-scoped tables when the tenant context is active.
    Events registered on Session propagate to AsyncSession in SQLAlchemy 2.0.
    Skips INSERT, DDL, and queries where company_id is already present.
    """
    try:
        from sqlalchemy.orm import Session as _SyncSession

        @event.listens_for(_SyncSession, "do_orm_execute")
        def _auto_inject_tenant_filter(orm_execute_state):
            tid = get_current_tenant_id()
            if tid is None:
                return

            clause = orm_execute_state.statement
            is_dml = isinstance(clause, (Update, Delete))
            is_select = hasattr(clause, "froms") and clause.froms is not None
            if not is_select and not is_dml:
                return

            table_name = _get_entity_table_name(orm_execute_state)
            if table_name is None or table_name not in _TENANT_SCOPED_TABLES:
                return

            if _query_has_company_id(clause):
                return

            try:
                from sqlalchemy import literal

                tenant_col = literal(tid)
                new_statement = (
                    clause.where(clause.columns.get("company_id") == tenant_col)
                    if hasattr(clause, "columns") and "company_id" in clause.columns
                    else clause
                )
                orm_execute_state.statement = new_statement
            except Exception as _e:
                logger.debug("Failed to inject tenant filter: %s", _e)

        logger.info("Tenant isolation event listener registered for %d tables", len(_TENANT_SCOPED_TABLES))
    except Exception as e:
        logger.error("Failed to register tenant isolation event listener: %s", e)


# Register the tenant isolation listener at module import time
_register_tenant_isolation_listener()


# ---------------------------------------------------------------
# Lazy Engine Initialization
# ---------------------------------------------------------------
# The async engine and session factory are created lazily to allow
# standalone imports (e.g., syntax checking, linting) without a
# running PostgreSQL database. Production/test environments with
# DATABASE_URL set will initialize on first use automatically.
# This also enables SQLite fallback for isolated testing.


class _LazySessionFactory:
    """Callable proxy for AsyncSessionLocal that defers to the real factory.

    External modules import AsyncSessionLocal at module level. By making
    it a proxy object (not None), all import references stay live even
    if the real session factory is initialized later.
    """

    def __init__(self):
        self._factory = None

    def __call__(self, *args, **kwargs):
        _ensure_engine()
        return self._factory(*args, **kwargs)

    def _set(self, factory):
        self._factory = factory

    def __bool__(self):
        return self._factory is not None


_engine = None
AsyncSessionLocal = _LazySessionFactory()

_sql_echo = settings.debug

# PgBouncer transaction mode does not support session-level prepared statements.
# Setting prepared_statement_cache_size=0 disables them at the asyncpg driver level.
_asyncpg_args: dict = {}
if settings.pgbouncer_enabled:
    _asyncpg_args["prepared_statement_cache_size"] = 0
    _asyncpg_args["statement_cache_size"] = 0


def _ensure_engine():
    """Create the async engine and session factory on first use.

    Idempotent — subsequent calls are no-ops. In production with
    DATABASE_URL set, this runs transparently on the first DB operation.
    In testing/linting contexts without a database, import succeeds
    and engine creation is deferred until a real DB is available.
    """
    global _engine, AsyncSessionLocal
    if _engine is not None:
        return
    try:
        _engine = create_async_engine(
            settings.database_url,
            echo=_sql_echo,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_use_lifo=True,
            connect_args={
                "server_settings": {"statement_timeout": "30000", "application_name": "workticket-api"},
                **_asyncpg_args,
            }
            if _asyncpg_args
            else {
                "server_settings": {"statement_timeout": "30000", "application_name": "workticket-api"},
            },
        )
        sf = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
        AsyncSessionLocal._set(sf)
        _register_query_monitoring()
        logger.info(
            "Database engine initialized (pool_size=%d, max_overflow=%d)",
            settings.db_pool_size,
            settings.db_max_overflow,
        )
    except Exception as e:
        logger.warning("Database engine initialization deferred: %s", e)
        # Keep _engine=None so subsequent _ensure_engine() calls will retry


def _get_engine():
    """Get the engine, initializing if necessary."""
    _ensure_engine()
    if _engine is None:
        raise RuntimeError("Database engine is not available. Ensure DATABASE_URL is set and PostgreSQL is running.")
    return _engine


def __getattr__(name: str):
    if name == "engine":
        return _get_engine()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --- DB Circuit Breaker ---
# Opens when pool utilization exceeds threshold OR connection errors occur,
# prevents retry amplification during connection exhaustion or DB failure.
# Uses exponential backoff with half-open state to prevent oscillation.
#
# H1-FIX: State is now coordinated via Redis (Lua-based atomic transitions)
# instead of process-local globals. This ensures all replicas share the same
# circuit state, preventing split-brain where one replica allows queries
# while another rejects them. The half-open probe is global: exactly 1 probe
# across all replicas on cooldown expiry.
_circuit_lock = threading.Lock()

_POOL_CIRCUIT_OPEN = False
_POOL_CIRCUIT_LAST_FAILURE = 0.0
_POOL_UTIL_THRESHOLD = 0.85
_POOL_CIRCUIT_CONSECUTIVE_ERRORS = 0
_POOL_CIRCUIT_ERROR_THRESHOLD = 3

# Exponential backoff state
_POOL_CIRCUIT_BASE_COOLDOWN = 30.0
_POOL_CIRCUIT_MAX_COOLDOWN = 300.0
_POOL_CIRCUIT_CURRENT_COOLDOWN = 30.0
_POOL_CIRCUIT_BACKOFF_LEVEL = 0

# Half-open state: allows exactly 1 probe request on cooldown expiry
_POOL_CIRCUIT_HALF_OPEN = False
_POOL_CIRCUIT_HALF_OPEN_PROBED = False

# Redis circuit breaker state keys
_CIRCUIT_REDIS_PREFIX = "db:circuit:breaker"
_CIRCUIT_REDIS_URL = None


def _get_circuit_redis_url() -> str:
    global _CIRCUIT_REDIS_URL
    if _CIRCUIT_REDIS_URL is None:
        _CIRCUIT_REDIS_URL = os.environ.get(
            "REDIS_CACHE_URL",
            os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        )
    return _CIRCUIT_REDIS_URL


def _get_circuit_redis():
    """Get a sync Redis client for circuit breaker coordination."""
    try:
        from app.sync_redis_pool import get_sync_redis

        return get_sync_redis()
    except Exception as _e:
        logger.debug("Failed to get circuit breaker Redis: %s", _e)
        return None


_CIRCUIT_LUA_OPEN = """
    -- H1-FIX: Atomically open the circuit breaker across all replicas.
    -- Stores state with TTL = cooldown so it auto-recovers if no replica
    -- refreshes it. The cooldown and backoff level are stored for accurate
    -- half-open probe timing.
    local key = KEYS[1]
    local cooldown = tonumber(ARGV[1])
    local backoff_level = tonumber(ARGV[2])
    local max_cooldown = tonumber(ARGV[3])
    -- Read current state
    local state = redis.call('HGETALL', key)
    if #state > 0 then
        -- Only escalate cooldown if not already open (prevent race escalation)
        local current_level = tonumber(state[2]) or 0
        if current_level >= backoff_level then
            return {0, current_level, state[4] or cooldown}
        end
    end
    -- Write new state: open=1, backoff_level, cooldown, last_failure=now
    local now = redis.call('TIME')[1]
    redis.call('HMSET', key,
        'open', '1',
        'level', backoff_level,
        'cooldown', cooldown,
        'last_failure', now,
        'half_open', '0',
        'half_open_probed', '0'
    )
    redis.call('EXPIRE', key, math.ceil(max_cooldown * 2))
    return {1, backoff_level, cooldown}
"""

_CIRCUIT_LUA_CHECK = """
    -- H1-FIX: Check circuit state and handle half-open probe globally.
    -- Returns {open, allow_probe, cooldown_remaining, half_open}
    -- open=1: circuit is open, 0: closed
    -- allow_probe=1: this replica may probe (exactly 1 across all replicas)
    local key = KEYS[1]
    local state = redis.call('HGETALL', key)
    if #state == 0 then
        return {0, 0, 0, 0}
    end
    local tbl = {}
    for i = 1, #state, 2 do
        tbl[state[i]] = state[i+1]
    end
    if tbl['open'] ~= '1' then
        return {0, 0, 0, 0}
    end
    local now = redis.call('TIME')[1]
    local last_failure = tonumber(tbl['last_failure']) or 0
    local cooldown = tonumber(tbl['cooldown']) or 30
    local elapsed = now - last_failure
    if elapsed <= cooldown then
        return {1, 0, cooldown - elapsed, tonumber(tbl['half_open']) or 0}
    end
    -- Cooldown expired: enter half-open state
    local half_open = tonumber(tbl['half_open']) or 0
    local half_open_probed = tonumber(tbl['half_open_probed']) or 0
    if half_open == 0 then
        -- Enter half-open state
        redis.call('HSET', key, 'half_open', '1')
        return {1, 1, 0, 1}
    end
    if half_open_probed == 0 then
        -- Allow exactly 1 probe across all replicas
        redis.call('HSET', key, 'half_open_probed', '1')
        return {1, 1, 0, 1}
    end
    -- Probe already used, reset last_failure to extend cooldown
    redis.call('HSET', key, 'last_failure', now)
    return {1, 0, cooldown, 1}
"""

_CIRCUIT_LUA_RESET = """
    -- H1-FIX: Close the circuit breaker (reset to initial state).
    local key = KEYS[1]
    redis.call('DEL', key)
    return 1
"""

# Cache the compiled Lua scripts
_CIRCUIT_OPEN_SCRIPT = None
_CIRCUIT_CHECK_SCRIPT = None
_CIRCUIT_RESET_SCRIPT = None


def _get_lua_scripts(r):
    """Load Lua scripts into Redis and cache the SHA digests."""
    global _CIRCUIT_OPEN_SCRIPT, _CIRCUIT_CHECK_SCRIPT, _CIRCUIT_RESET_SCRIPT
    if _CIRCUIT_OPEN_SCRIPT is None:
        _CIRCUIT_OPEN_SCRIPT = r.script_load(_CIRCUIT_LUA_OPEN)
    if _CIRCUIT_CHECK_SCRIPT is None:
        _CIRCUIT_CHECK_SCRIPT = r.script_load(_CIRCUIT_LUA_CHECK)
    if _CIRCUIT_RESET_SCRIPT is None:
        _CIRCUIT_RESET_SCRIPT = r.script_load(_CIRCUIT_LUA_RESET)
    return _CIRCUIT_OPEN_SCRIPT, _CIRCUIT_CHECK_SCRIPT, _CIRCUIT_RESET_SCRIPT


def _pool_utilization() -> float:
    """Ratio of connections currently checked out vs total available (pool + overflow)."""
    try:
        p = _get_engine().pool
        total = p.size() + p.overflow()
        checked_out = p.checkedout()
        if total <= 0:
            return 0.0
        return checked_out / total
    except Exception as e:
        logger.debug("Failed to compute pool utilization: %s", e)
        return 0.0


def _get_cooldown_with_jitter() -> float:
    """Return current cooldown with jitter: base + random(0, 0.25 * base)."""
    import random

    return _POOL_CIRCUIT_CURRENT_COOLDOWN + random.uniform(0, 0.25 * _POOL_CIRCUIT_CURRENT_COOLDOWN)  # nosec B311


def _reset_circuit_breaker() -> None:
    """Reset the circuit breaker state.

    H1-FIX: Also clears the Redis-coordinated state so all replicas
    recover simultaneously.
    """
    global _POOL_CIRCUIT_OPEN, _POOL_CIRCUIT_LAST_FAILURE, _POOL_CIRCUIT_CONSECUTIVE_ERRORS
    global _POOL_CIRCUIT_CURRENT_COOLDOWN, _POOL_CIRCUIT_BACKOFF_LEVEL
    global _POOL_CIRCUIT_HALF_OPEN, _POOL_CIRCUIT_HALF_OPEN_PROBED
    _POOL_CIRCUIT_OPEN = False
    _POOL_CIRCUIT_CONSECUTIVE_ERRORS = 0
    _POOL_CIRCUIT_CURRENT_COOLDOWN = _POOL_CIRCUIT_BASE_COOLDOWN
    _POOL_CIRCUIT_BACKOFF_LEVEL = 0
    _POOL_CIRCUIT_HALF_OPEN = False
    _POOL_CIRCUIT_HALF_OPEN_PROBED = False
    _report_cooldown_gauge()

    # Clear Redis-coordinated state
    try:
        r = _get_circuit_redis()
        if r:
            sha = _get_lua_scripts(r)[2]
            r.evalsha(sha, 1, _CIRCUIT_REDIS_PREFIX)
    except Exception as e:
        logger.warning("Failed to clear Redis-coordinated circuit breaker state: %s", e)


def _report_cooldown_gauge() -> None:
    """Report current cooldown to Prometheus gauge."""
    try:
        from app.monitoring.prometheus import _set_db_circuit_cooldown

        _set_db_circuit_cooldown(_POOL_CIRCUIT_CURRENT_COOLDOWN if _POOL_CIRCUIT_OPEN else 0.0)
    except Exception as _e:
        logger.debug("Failed to report cooldown gauge: %s", _e)


def _check_db_circuit() -> None:
    """Raise DBUnavailableError if the DB circuit breaker is open.

    H1-FIX: Uses Redis-coordinated circuit breaker state to ensure all
    replicas share the same circuit state and exactly 1 half-open probe
    is allowed globally across all replicas.

    Implements half-open state: on cooldown expiry, allows exactly 1
    request globally to pass as a probe. Falls back to process-local
    state if Redis is unavailable.
    """
    global _POOL_CIRCUIT_OPEN, _POOL_CIRCUIT_LAST_FAILURE
    global _POOL_CIRCUIT_HALF_OPEN, _POOL_CIRCUIT_HALF_OPEN_PROBED

    # Try Redis-coordinated check first
    try:
        r = _get_circuit_redis()
        if r:
            sha = _get_lua_scripts(r)[1]
            result = r.evalsha(sha, 1, _CIRCUIT_REDIS_PREFIX)
            if result:
                circuit_open = result[0] == 1
                allow_probe = result[1] == 1
                cooldown_remaining = result[2]
                _half_open = result[3] == 1

                if not circuit_open:
                    return  # Circuit is closed globally

                if allow_probe:
                    # V2-FIX: Only probe if a connection is actually available
                    try:
                        pool = _get_engine().pool
                        if pool.checkedin() == 0 and pool.overflow() >= 0:
                            total = pool.total()
                            checkedout = pool.checkedout()
                            if checkedout >= total:
                                logger.warning(
                                    "DB half-open probe skipped — pool exhausted (%d/%d checked out)", checkedout, total
                                )
                                _POOL_CIRCUIT_LAST_FAILURE = time.monotonic()
                                raise _db_unavailable("Pool exhausted — skipping half-open probe")
                    except _db_unavailable.__class__:
                        raise
                    except Exception as _e:
                        logger.debug("Half-open probe pool check failed: %s", _e)
                    logger.info("DB circuit breaker half-open (global): allowing probe request")
                    return

                raise _db_unavailable(
                    f"Database circuit breaker open (global, cooldown={cooldown_remaining:.0f}s remaining)."
                )
    except _db_unavailable.__class__:
        raise
    except Exception as _e:
        logger.debug("Redis-coordinated circuit breaker check failed: %s", _e)

    # Fallback to process-local circuit breaker
    if not _POOL_CIRCUIT_OPEN:
        return
    elapsed = time.monotonic() - _POOL_CIRCUIT_LAST_FAILURE
    cooldown = _get_cooldown_with_jitter()
    if elapsed > cooldown:
        if not _POOL_CIRCUIT_HALF_OPEN:
            _POOL_CIRCUIT_HALF_OPEN = True
            _POOL_CIRCUIT_HALF_OPEN_PROBED = False
            logger.info("DB circuit breaker entering half-open state (local) after %.1fs cooldown", elapsed)
        if not _POOL_CIRCUIT_HALF_OPEN_PROBED:
            _POOL_CIRCUIT_HALF_OPEN_PROBED = True
            logger.info("DB circuit breaker half-open (local): allowing probe request")
            return
        _POOL_CIRCUIT_LAST_FAILURE = time.monotonic()
        raise _db_unavailable(
            "Database pool exhausted or consecutive errors — circuit breaker open (probe used, local). Retry later."
        )
    raise _db_unavailable("Database pool exhausted or consecutive errors — circuit breaker open (local). Retry later.")


def _record_pool_exhaustion() -> None:
    """Open the DB circuit breaker when pool is near exhaustion or on connection errors.

    H1-FIX: Coordinates state via Redis so all replicas share the same
    exponential backoff and half-open timing. Falls back to process-local
    state if Redis is unavailable.

    Implements exponential backoff: 30s -> 60s -> 120s -> 300s (capped).
    """
    global _POOL_CIRCUIT_OPEN, _POOL_CIRCUIT_LAST_FAILURE
    global _POOL_CIRCUIT_CURRENT_COOLDOWN, _POOL_CIRCUIT_BACKOFF_LEVEL
    global _POOL_CIRCUIT_HALF_OPEN, _POOL_CIRCUIT_HALF_OPEN_PROBED

    _POOL_CIRCUIT_BACKOFF_LEVEL += 1
    _POOL_CIRCUIT_CURRENT_COOLDOWN = min(
        _POOL_CIRCUIT_BASE_COOLDOWN * (2 ** (_POOL_CIRCUIT_BACKOFF_LEVEL - 1)), _POOL_CIRCUIT_MAX_COOLDOWN
    )

    # Try Redis-coordinated open
    try:
        r = _get_circuit_redis()
        if r:
            sha = _get_lua_scripts(r)[0]
            result = r.evalsha(
                sha,
                1,
                _CIRCUIT_REDIS_PREFIX,
                _POOL_CIRCUIT_CURRENT_COOLDOWN,
                _POOL_CIRCUIT_BACKOFF_LEVEL,
                _POOL_CIRCUIT_MAX_COOLDOWN,
            )
            if result and result[0] == 1:
                logger.error(
                    "DB circuit breaker OPEN (global) — pool utilization %.0f%%, consecutive errors %d, cooldown=%.1fs (level %d)",
                    _pool_utilization() * 100,
                    _POOL_CIRCUIT_CONSECUTIVE_ERRORS,
                    _POOL_CIRCUIT_CURRENT_COOLDOWN,
                    _POOL_CIRCUIT_BACKOFF_LEVEL,
                )
                _report_cooldown_gauge()
                return
    except Exception as _e:
        logger.debug("Redis-coordinated circuit breaker open failed: %s", _e)

    # Fallback to process-local state
    _POOL_CIRCUIT_OPEN = True
    _POOL_CIRCUIT_LAST_FAILURE = time.monotonic()
    _POOL_CIRCUIT_HALF_OPEN = False
    _POOL_CIRCUIT_HALF_OPEN_PROBED = False
    util = _pool_utilization() * 100
    logger.error(
        "DB circuit breaker OPEN (local) — pool utilization %.0f%%, consecutive errors %d, cooldown=%.1fs (level %d)",
        util,
        _POOL_CIRCUIT_CONSECUTIVE_ERRORS,
        _POOL_CIRCUIT_CURRENT_COOLDOWN,
        _POOL_CIRCUIT_BACKOFF_LEVEL,
    )
    _report_cooldown_gauge()


def _record_db_error() -> None:
    """Track consecutive DB errors (connection failures, query timeouts, deadlocks)."""
    global _POOL_CIRCUIT_CONSECUTIVE_ERRORS
    _POOL_CIRCUIT_CONSECUTIVE_ERRORS += 1
    if _POOL_CIRCUIT_CONSECUTIVE_ERRORS >= _POOL_CIRCUIT_ERROR_THRESHOLD:
        logger.error("DB circuit breaker triggered by %d consecutive errors", _POOL_CIRCUIT_CONSECUTIVE_ERRORS)
        _record_pool_exhaustion()


class DBUnavailableError(Exception):
    """Raised when the database is unavailable (pool exhausted or connection failed)."""



def _db_unavailable(detail: str) -> DBUnavailableError:
    return DBUnavailableError(detail)


_readonly_engine = None
_readonly_sessionmaker = None
_readonly_engine_init_done = False

# SC2: Read replica lag tracking
_read_replica_lag_seconds: float = 0.0
_read_replica_last_check: float = 0.0
_READ_REPLICA_LAG_CHECK_INTERVAL = 30.0  # seconds between checks


def _ensure_readonly_engine():
    global _readonly_engine, _readonly_sessionmaker, _readonly_engine_init_done
    if not _readonly_engine_init_done:
        url = settings.database_readonly_url or settings.database_url
        _readonly_engine = create_async_engine(
            url,
            echo=_sql_echo,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_use_lifo=True,
            connect_args=_asyncpg_args if _asyncpg_args else {},
        )
        _readonly_sessionmaker = async_sessionmaker(_readonly_engine, class_=AsyncSession, expire_on_commit=False)
        _readonly_engine_init_done = True


# SC-3: Read Replica Routing Annotation
# Endpoint categories that SHOULD use get_db_readonly() via read replica:
#   - Analytics queries (aggregations, dashboards, reporting)
#   - Usage history (billing usage ledger reads)
#   - Cost drift / reconciliation reports
#   - Trace history (tracing/router.py read endpoints)
#   - Audit queries (ai/audit.py, estimates/audit.py read operations)
#   - AI output retrieval (read-only historical AI results)
#
# Usage: Replace `db: AsyncSession = Depends(get_db)` with
#        `db: AsyncSession = Depends(get_db_readonly)` for read-only endpoints.
#        For internal (non-FastAPI) read-only queries, use `get_readonly_session()`.

# V2-FIX: Eager init at import time to avoid lazy-init in sync Celery context.
# Wrapped in try/except so imports don't fail when the database is unavailable
# (e.g., during Docker build or before migration). The engine will be lazily
# initialized on first use instead.
try:
    _ensure_readonly_engine()
except Exception as e:
    logger.warning("Read-only engine eager init deferred: %s", e)


def get_readonly_engine():
    _ensure_readonly_engine()
    return _readonly_engine


def get_readonly_session() -> AsyncSession:
    _ensure_readonly_engine()
    return _readonly_sessionmaker()


async def check_read_replica_lag() -> float:
    """SC2: Check replication lag between primary and read replica.

    Compares the current WAL LSN position on both the primary and the read
    replica (if configured). Returns the lag in seconds, or 0 if no replica
    is configured or the check fails.

    The lag value is pushed to a Prometheus gauge for alerting.
    """
    global _read_replica_lag_seconds, _read_replica_last_check
    now = time.time()
    if now - _read_replica_last_check < _READ_REPLICA_LAG_CHECK_INTERVAL:
        return _read_replica_lag_seconds

    _read_replica_last_check = now
    url = settings.database_readonly_url or settings.database_url
    if url == settings.database_url:
        _read_replica_lag_seconds = 0.0
        return 0.0

    try:
        primary_engine = _get_engine()
        _ensure_readonly_engine()
        replica_engine = get_readonly_engine()

        async with primary_engine.connect() as p_conn:
            p_result = await p_conn.execute(text("SELECT pg_current_wal_lsn()::text"))
            primary_lsn = p_result.scalar()

        async with replica_engine.connect() as r_conn:
            r_result = await r_conn.execute(text("SELECT pg_last_wal_replay_lsn()::text"))
            replica_lsn = r_result.scalar()

        if primary_lsn and replica_lsn:
            async with replica_engine.connect() as r_conn:
                lag_result = await r_conn.execute(
                    text("SELECT EXTRACT(EPOCH FROM (pg_last_wal_receive_lsn() - pg_last_wal_replay_lsn()))")
                )
                lag = lag_result.scalar()
                _read_replica_lag_seconds = float(lag) if lag else 0.0
            logger.debug("Read replica lag: %.2f seconds", _read_replica_lag_seconds)
        else:
            _read_replica_lag_seconds = 0.0
    except Exception as e:
        logger.debug("Read replica lag check failed: %s", e)
        _read_replica_lag_seconds = 0.0

    try:
        from app.monitoring.prometheus import set_read_replica_lag

        set_read_replica_lag(_read_replica_lag_seconds)
    except Exception as e:
        logger.debug("Failed to report read replica lag metric: %s", e)


async def get_read_replica_safe_session() -> AsyncSession:
    """SC2: Get a read replica session, falling back to primary if lag is too high.

    If read replica lag exceeds 30 seconds, routes the query to the primary
    database instead to ensure fresh data for latency-sensitive operations.
    """
    lag = await check_read_replica_lag()
    if lag > 30.0:
        logger.warning("Read replica lag %.0fs exceeds threshold — routing to primary", lag)
        return AsyncSessionLocal()
    return get_readonly_session()


_celery_engine = None
_beat_engine = None
_beat_sessionmaker = None


def get_celery_engine():
    global _celery_engine
    if _celery_engine is None:
        # HIGH-8 FIX: Reduced pool_size from 10 to 5, max_overflow from 5 to 2
        # to prevent PostgreSQL connection exhaustion. With 4 worker types + beat
        # and 5 replicas, total connections were ~260 against max_connections=100.
        _celery_engine = create_async_engine(
            settings.database_url,
            echo=_sql_echo,
            pool_size=max(2, settings.celery_pool_size // 2),
            max_overflow=2,
            pool_timeout=5,
            pool_pre_ping=True,
            pool_recycle=3600,
            # M-5 FIX: Add statement_timeout to Celery engine sessions
            connect_args={
                **_asyncpg_args,
                "server_settings": {"statement_timeout": "30000", "application_name": "workticket-celery"},
            }
            if _asyncpg_args
            else {"server_settings": {"statement_timeout": "30000", "application_name": "workticket-celery"}},
        )
    return _celery_engine


def get_beat_engine():
    global _beat_engine, _beat_sessionmaker
    if _beat_engine is None:
        # SC-5: Share pool configuration with main engine
        _beat_engine = create_async_engine(
            settings.database_url,
            echo=_sql_echo,
            pool_size=5,
            max_overflow=5,
            pool_timeout=settings.db_pool_timeout,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_use_lifo=True,
            connect_args={
                **_asyncpg_args,
                "server_settings": {"application_name": "workticket-beat"},
            }
            if _asyncpg_args
            else {"server_settings": {"application_name": "workticket-beat"}},
        )
        _beat_sessionmaker = async_sessionmaker(_beat_engine, class_=AsyncSession, expire_on_commit=False)
    return _beat_engine


# SC-5: Consolidated to use the main engine's sessionmaker with proper pool sizing.
# Celery workers now share the main connection pool rather than creating a separate engine.
def get_celery_session() -> AsyncSession:
    return AsyncSessionLocal()


def get_beat_session() -> AsyncSession:
    get_beat_engine()
    return _beat_sessionmaker()


def get_db_pool_metrics() -> dict[str, Any]:
    pool = _get_engine().pool
    total = pool.size() + pool.overflow()
    checked_out = pool.checkedout()
    return {
        "size": pool.size(),
        "checkedin": pool.checkedin(),
        "checkedout": checked_out,
        "overflow": pool.overflow(),
        "total": total,
        "utilization_pct": round((checked_out / total * 100), 1) if total > 0 else 0,
        "circuit_breaker_open": _POOL_CIRCUIT_OPEN,
    }


class Base(DeclarativeBase):
    pass


RETRYABLE_PG_CODES = {"40001", "40P01", "55P03"}
MAX_RETRIES = 2
RETRY_BACKOFF = [0.1, 0.5]


def is_retryable_error(exc: Exception) -> bool:
    exc_str = str(exc)
    return any(code in exc_str for code in RETRYABLE_PG_CODES)


async def run_celery_tx_with_retry(
    coro_factory,
    max_retries: int = 3,
    retry_backoff: list | None = None,
):
    """Run a Celery DB operation with automatic retry on serialization failures.

    H-4 FIX: Celery tasks using AsyncSessionLocal() directly do not benefit from
    the serialization retry logic built into the FastAPI get_db() dependency.
    This utility wraps any async callable inside an AsyncSessionLocal session
    with retry on PostgreSQL serialization failures (40001/40P01/55P03).

    Usage:
        await run_celery_tx_with_retry(lambda db: my_operation(db))

    Args:
        coro_factory: async callable taking an AsyncSession and returning a value
        max_retries: maximum number of retry attempts (default 3)
        retry_backoff: list of backoff seconds per retry attempt (default [0.1, 0.5, 1.0])
    """
    if retry_backoff is None:
        retry_backoff = [0.1, 0.5, 1.0]
    last_exc = None
    for attempt in range(max_retries):
        async with AsyncSessionLocal() as db:
            try:
                result = await coro_factory(db)
                await db.commit()
                return result
            except Exception as exc:
                await db.rollback()
                if is_retryable_error(exc) and attempt < max_retries - 1:
                    last_exc = exc
                    await asyncio.sleep(retry_backoff[min(attempt, len(retry_backoff) - 1)])
                    continue
                raise
    raise last_exc if last_exc else RuntimeError("DB retry exhausted")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    _check_db_circuit()
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        session = AsyncSessionLocal()
        tid = None
        try:
            # RLS: Always set RLS tenant context for defense-in-depth.
            # Default to nil UUID (blocks all queries) to prevent data access
            # when no tenant context has been set by the auth dependency.
            tid = get_current_tenant_id()
            if tid is not None:
                try:
                    from app.db.rls import set_rls_tenant_context

                    await set_rls_tenant_context(session, tid)
                except Exception as _rls_e:
                    logger.debug("Failed to set RLS context: %s", _rls_e)
            else:
                try:
                    from app.db.rls import set_rls_block_context

                    await set_rls_block_context(session)
                except Exception as _rls_e:
                    logger.debug("Failed to set RLS block context: %s", _rls_e)
            yield session
            await session.commit()
            _reset_circuit_breaker()
            return
        except Exception as exc:
            await session.rollback()
            exc_str = str(exc)
            # Pool timeout (SQLAlchemy TimeoutError) — open circuit breaker, fail fast
            if "timeout" in exc_str.lower() and "pool" in exc_str.lower():
                _record_pool_exhaustion()
                raise DBUnavailableError("Database connection pool exhausted") from exc
            if is_retryable_error(exc):
                _record_db_error()
                if attempt < MAX_RETRIES:
                    last_exc = exc
                    await asyncio.sleep(RETRY_BACKOFF[attempt])
                    continue
            raise
        finally:
            # RLS: Clear tenant context before returning session to pool
            try:
                from app.db.rls import clear_rls_tenant_context

                await clear_rls_tenant_context(session)
            except Exception as e:
                logger.debug("Failed to clear RLS context in get_db: %s", e)
            await session.close()
    if last_exc:
        raise DBUnavailableError("DB operation failed after retries") from last_exc


async def get_db_with_refresh() -> AsyncGenerator[AsyncSession, None]:
    _check_db_circuit()
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        session = AsyncSessionLocal()
        tid = None
        try:
            tid = get_current_tenant_id()
            if tid is not None:
                try:
                    from app.db.rls import set_rls_tenant_context

                    await set_rls_tenant_context(session, tid)
                except Exception as _rls_e:
                    logger.debug("Failed to set RLS context: %s", _rls_e)
            else:
                try:
                    from app.db.rls import set_rls_block_context

                    await set_rls_block_context(session)
                except Exception as _rls_e:
                    logger.debug("Failed to set RLS block context: %s", _rls_e)
            yield session
            await session.commit()
            await session.expire_all()
            _reset_circuit_breaker()
            return
        except Exception as exc:
            await session.rollback()
            exc_str = str(exc)
            if "timeout" in exc_str.lower() and "pool" in exc_str.lower():
                _record_pool_exhaustion()
                raise DBUnavailableError("Database connection pool exhausted") from exc
            if attempt < MAX_RETRIES and is_retryable_error(exc):
                last_exc = exc
                await asyncio.sleep(RETRY_BACKOFF[attempt])
                continue
            raise
        finally:
            try:
                from app.db.rls import clear_rls_tenant_context

                await clear_rls_tenant_context(session)
            except Exception as e:
                logger.debug("Failed to clear RLS context in get_db_with_refresh: %s", e)
            await session.close()
    if last_exc:
        raise DBUnavailableError("DB operation failed after retries") from last_exc


async def get_db_readonly() -> AsyncGenerator[AsyncSession, None]:
    _check_db_circuit()
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        session = get_readonly_session()
        tid = None
        try:
            tid = get_current_tenant_id()
            if tid is not None:
                try:
                    from app.db.rls import set_rls_tenant_context

                    await set_rls_tenant_context(session, tid)
                except Exception as _rls_e:
                    logger.debug("Failed to set RLS context on readonly: %s", _rls_e)
            else:
                try:
                    from app.db.rls import set_rls_block_context

                    await set_rls_block_context(session)
                except Exception as _rls_e:
                    logger.debug("Failed to set RLS block context on readonly: %s", _rls_e)
            yield session
            await session.commit()
            _reset_circuit_breaker()
            return
        except Exception as exc:
            await session.rollback()
            exc_str = str(exc)
            if "timeout" in exc_str.lower() and "pool" in exc_str.lower():
                _record_pool_exhaustion()
                raise DBUnavailableError("Database connection pool exhausted") from exc
            if attempt < MAX_RETRIES and is_retryable_error(exc):
                last_exc = exc
                await asyncio.sleep(RETRY_BACKOFF[attempt])
                continue
            raise
        finally:
            try:
                from app.db.rls import clear_rls_tenant_context

                await clear_rls_tenant_context(session)
            except Exception as e:
                logger.debug("Failed to clear RLS context in get_db_readonly: %s", e)
            await session.close()
    if last_exc:
        raise DBUnavailableError("DB read-only operation failed after retries") from last_exc


async def _validate_tenant_scoped_tables(engine) -> int:
    """At startup, detect all tables with company_id column that are NOT in
    _TENANT_SCOPED_TABLES. Logs warnings on drift. Returns 0 if no drift found.

    In production mode (settings.debug == False), refuses to start if
    tables are missing from the tenant isolation set, preventing data leaks.
    """
    from app.config import get_settings

    _settings = get_settings()

    async with engine.connect() as conn:
        result = await conn.execute(
            text("""
            SELECT table_name FROM information_schema.columns
            WHERE table_schema = 'public'
              AND column_name = 'company_id'
              AND table_name NOT IN ('companies', 'stripe_webhook_events')
            ORDER BY table_name
        """)
        )
        db_tables = {row[0] for row in result}

    missing_from_set = db_tables - _TENANT_SCOPED_TABLES
    missing_from_db = _TENANT_SCOPED_TABLES - db_tables

    if missing_from_set:
        msg = (
            f"TENANT ISOLATION DRIFT DETECTED: {len(missing_from_set)} tables have company_id "
            f"but are NOT in _TENANT_SCOPED_TABLES: {sorted(missing_from_set)}. "
            f"These tables have NO ORM-level tenant isolation!"
        )
        logger.critical(msg)
        if not _settings.debug:
            raise RuntimeError(f"Tenant isolation drift: {sorted(missing_from_set)}")
        logger.warning("Running in debug mode — refusing to stop. Fix _TENANT_SCOPED_TABLES before production deploy.")

    if missing_from_db:
        logger.warning(
            "Stale entries in _TENANT_SCOPED_TABLES (no company_id column in DB): %s",
            sorted(missing_from_db),
        )

    logger.info("Tenant isolation table scan: %d tables verified, %d drift", len(db_tables), len(missing_from_set))
    return len(missing_from_set)


# ---------------------------------------------------------------
# N+1 Query Detection & Slow Query Logging
# ---------------------------------------------------------------
# ContextVar-based per-request query counting and slow query logging.
# Event listeners are registered lazily via _register_query_monitoring()
# called from _ensure_engine() after the engine is initialized.

_n1_query_counter: contextvars.ContextVar[Counter | None] = contextvars.ContextVar("_n1_query_counter", default=None)
_n1_threshold = 20

_N1_SKIP_PATTERNS = re.compile(r"(ROLLBACK|COMMIT|BEGIN|SAVEPOINT|RELEASE)", re.IGNORECASE)


def reset_n1_counter():
    _n1_query_counter.set(Counter())


def get_n1_warnings() -> list[str]:
    counter = _n1_query_counter.get()
    return [
        f"N+1 detected: {query[:120]}... ({count}x)" for query, count in counter.most_common() if count > _n1_threshold
    ]


def _register_query_monitoring():
    """Register N+1 detection and slow query logging event listeners.

    Called from _ensure_engine() after the engine is initialized.
    Must NOT be called before the engine exists.
    """
    eng = _get_engine()

    @event.listens_for(eng.sync_engine, "before_cursor_execute")
    def _n1_before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if _N1_SKIP_PATTERNS.match(statement.strip() if statement else ""):
            return
        start_time = time.monotonic()
        context._workticket_query_start = start_time
        context._workticket_statement = statement

    @event.listens_for(eng.sync_engine, "after_cursor_execute")
    def _n1_after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        start_time = getattr(context, "_workticket_query_start", None)
        if start_time is None:
            return

        duration_ms = (time.monotonic() - start_time) * 1000

        # N+1 detection
        if not _N1_SKIP_PATTERNS.match(statement.strip() if statement else ""):
            normalized = re.sub(r"'(?:[^'\\]|\\.)'", "?", str(statement))
            normalized = re.sub(r"\d+", "N", normalized)
            query_hash = hashlib.sha256(normalized.encode()).hexdigest()[:12]
            counter = _n1_query_counter.get()
            counter[query_hash] += 1

        # Slow query logging (> 500ms)
        if duration_ms > 500:
            logger.warning(
                "Slow query (%.1fms): %s | params=%s",
                duration_ms,
                str(statement)[:200],
                str(parameters)[:100] if parameters else "none",
            )
