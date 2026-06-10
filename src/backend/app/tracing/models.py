import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.analytics.events import redact_metadata
from app.database import AsyncSessionLocal, Base

logger = logging.getLogger(__name__)


class ExecutionTrace(Base):
    __tablename__ = "execution_traces"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trace_id = Column(UUID(as_uuid=True), nullable=False)
    job_id = Column(UUID(as_uuid=True), nullable=True)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="SET NULL"), nullable=True)
    step_name = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False)
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Float, nullable=True)
    trace_metadata = Column("metadata", JSONB, nullable=True)
    error_message = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_execution_traces_trace_id", "trace_id"),
        Index("ix_execution_traces_job_id", "job_id"),
        Index("ix_execution_traces_step_name", "step_name"),
        Index("ix_execution_traces_started_at", "started_at"),
    )


_TRACE_RETENTION_DAYS = 30


async def cleanup_old_traces():
    try:
        from datetime import datetime, timedelta

        from sqlalchemy import delete

        cutoff = datetime.now(UTC) - timedelta(days=_TRACE_RETENTION_DAYS)
        async with AsyncSessionLocal() as db:
            result = await db.execute(delete(ExecutionTrace).where(ExecutionTrace.started_at < cutoff))
            await db.commit()
            logger.info("Cleaned up %s execution traces older than %d days", result.rowcount, _TRACE_RETENTION_DAYS)
    except Exception as e:
        logger.error("Failed to cleanup execution traces: %s", e)


def get_or_create_trace(request: dict | None = None, job_id: str | None = None, company_id: str | None = None) -> str:
    """Get trace_id from request context or create a new one."""
    if request and hasattr(request, "state") and hasattr(request.state, "correlation_id"):
        return request.state.correlation_id
    if request and "X-Correlation-ID" in request.headers:  # type: ignore[attr-defined]
        return request.headers["X-Correlation-ID"]
    return str(uuid.uuid4())


async def record_trace(
    trace_id: str,
    step_name: str,
    status: str = "started",
    job_id: str | None = None,
    company_id: str | None = None,
    duration_ms: float | None = None,
    metadata: dict | None = None,
    error_message: str | None = None,
):
    if not company_id:
        logger.error("record_trace called without company_id for trace=%s step=%s", trace_id, step_name)
        return
    try:
        async with AsyncSessionLocal() as db:
            entry = ExecutionTrace(
                trace_id=trace_id,
                job_id=job_id,
                company_id=company_id,
                step_name=step_name,
                status=status,
                duration_ms=round(duration_ms, 2) if duration_ms else None,
                trace_metadata=redact_metadata(metadata or {}),
                error_message=error_message[:2000] if error_message else None,
                completed_at=datetime.now(UTC) if status in ("completed", "failed") else None,
            )
            db.add(entry)
            await db.commit()
    except Exception as e:
        logger.error("Failed to write execution trace: %s", e)
