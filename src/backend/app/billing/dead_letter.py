import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Column, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


def _utcnow():
    return datetime.now(UTC)


class DeadLetterJob(Base):
    __tablename__ = "dead_letter_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(UUID(as_uuid=True), nullable=False)
    company_id = Column(UUID(as_uuid=True), nullable=False)
    user_id = Column(String(255), nullable=True)
    task_name = Column(String(100), nullable=False)
    error_message = Column(Text, nullable=True)
    failure_category = Column(String(50), nullable=True)
    last_state = Column(String(20), nullable=True)
    retry_count = Column(Integer, default=0)
    trace_id = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    # TTL for automatic cleanup (30 days)
    expires_at = Column(DateTime(timezone=True), default=lambda: _utcnow() + timedelta(days=30))

    __table_args__ = (
        Index("ix_dead_letter_jobs_job_id", "job_id"),
        Index("ix_dead_letter_jobs_company_id", "company_id"),
        Index("ix_dead_letter_jobs_created_at", "created_at"),
        Index("ix_dead_letter_jobs_expires_at", "expires_at"),
    )
