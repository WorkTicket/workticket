import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy import func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.dead_letter import DeadLetterJob

logger = logging.getLogger(__name__)


async def get_dlq_summary(db: AsyncSession, company_id: str | None = None, hours: int = 24) -> dict:
    query = select(
        sa_func.count(DeadLetterJob.id).label("total"),
        sa_func.count(DeadLetterJob.id)
        .filter(DeadLetterJob.created_at >= datetime.now(UTC) - timedelta(hours=hours))
        .label("recent"),
    )
    if company_id:
        query = query.where(DeadLetterJob.company_id == company_id)
    result = await db.execute(query)
    row = result.one()
    return {
        "total": row.total or 0,
        "recent_24h": row.recent or 0,
        "since_hours": hours,
    }


async def get_dlq_entries(
    db: AsyncSession,
    company_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    query = select(DeadLetterJob).order_by(DeadLetterJob.created_at.desc()).limit(limit).offset(offset)
    if company_id:
        query = query.where(DeadLetterJob.company_id == company_id)
    result = await db.execute(query)
    entries = [
        {
            "id": str(entry.id),
            "job_id": str(entry.job_id),
            "company_id": str(entry.company_id),
            "user_id": entry.user_id,
            "task_name": entry.task_name,
            "failure_category": entry.failure_category,
            "last_state": entry.last_state,
            "retry_count": entry.retry_count,
            "trace_id": entry.trace_id,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
        }
        for entry in result.scalars().all()
    ]
    return entries


async def cleanup_expired_dlq(db: AsyncSession) -> int:
    result = await db.execute(delete(DeadLetterJob).where(DeadLetterJob.expires_at <= datetime.now(UTC)))
    await db.commit()
    count = result.rowcount
    if count:
        logger.info("Cleaned up %d expired dead letter entries", count)
    return count


async def get_dlq_by_failure_category(db: AsyncSession, hours: int = 24) -> list[dict]:
    cutoff = datetime.now(UTC) - timedelta(hours=hours)
    query = (
        select(
            DeadLetterJob.failure_category,
            sa_func.count(DeadLetterJob.id).label("count"),
        )
        .where(DeadLetterJob.created_at >= cutoff)
        .group_by(DeadLetterJob.failure_category)
        .order_by(sa_func.count(DeadLetterJob.id).desc())
    )
    result = await db.execute(query)
    return [{"category": row.failure_category or "unknown", "count": row.count} for row in result.all()]
