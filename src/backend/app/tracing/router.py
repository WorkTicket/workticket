import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.jobs.models import User
from app.tracing.models import ExecutionTrace

logger = logging.getLogger(__name__)
router = APIRouter()


class TraceStepResponse(BaseModel):
    id: str
    trace_id: str
    job_id: str | None = None
    company_id: str | None = None
    step_name: str
    status: str
    started_at: str
    completed_at: str | None = None
    duration_ms: float | None = None
    metadata: dict | None = None
    error_message: str | None = None

    model_config = ConfigDict(from_attributes=True)


class TraceResponse(BaseModel):
    trace_id: str
    job_id: str | None = None
    steps: list[TraceStepResponse]
    total_duration_ms: float | None = None
    status: str


@router.get("/traces/{trace_id}", response_model=TraceResponse)
async def get_trace(
    trace_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ExecutionTrace)
        .where(ExecutionTrace.trace_id == trace_id, ExecutionTrace.company_id == current_user.company_id)
        .order_by(ExecutionTrace.started_at)
    )
    steps = result.scalars().all()
    if not steps:
        raise HTTPException(status_code=404, detail="Trace not found")

    total = None
    completed = [s for s in steps if s.duration_ms is not None]
    if completed:
        total = round(sum(s.duration_ms for s in completed), 2)

    statuses = {s.status for s in steps}
    if "failed" in statuses:
        overall = "failed"
    elif "started" in statuses:
        overall = "in_progress"
    else:
        overall = "completed"

    return TraceResponse(
        trace_id=str(trace_id),
        job_id=str(steps[0].job_id) if steps[0].job_id else None,
        steps=[_step_to_response(s) for s in steps],
        total_duration_ms=total,
        status=overall,
    )


@router.get("/traces/job/{job_id}", response_model=list[TraceResponse])
async def get_job_traces(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(ExecutionTrace)
        .where(ExecutionTrace.job_id == job_id, ExecutionTrace.company_id == current_user.company_id)
        .order_by(ExecutionTrace.started_at)
    )
    steps = result.scalars().all()
    if not steps:
        return []

    trace_ids = {str(s.trace_id) for s in steps}
    traces = []
    for tid in trace_ids:
        trace_steps = [s for s in steps if str(s.trace_id) == tid]
        trace_steps.sort(key=lambda s: s.started_at)
        completed = [s for s in trace_steps if s.duration_ms is not None]
        total = round(sum(s.duration_ms for s in completed), 2) if completed else None
        statuses = {s.status for s in trace_steps}
        if "failed" in statuses:
            overall = "failed"
        elif "started" in statuses:
            overall = "in_progress"
        else:
            overall = "completed"
        traces.append(
            TraceResponse(
                trace_id=tid,
                job_id=str(job_id),
                steps=[_step_to_response(s) for s in trace_steps],
                total_duration_ms=total,
                status=overall,
            )
        )
    return traces


@router.get("/traces", response_model=list[TraceResponse])
async def list_recent_traces(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    subq = (
        select(
            ExecutionTrace.trace_id,
        )
        .where(
            ExecutionTrace.company_id == current_user.company_id,
        )
        .group_by(
            ExecutionTrace.trace_id,
        )
        .order_by(
            desc(ExecutionTrace.started_at),
        )
        .limit(limit)
        .subquery()
    )

    result = await db.execute(
        select(ExecutionTrace)
        .where(ExecutionTrace.trace_id.in_(select(subq.c.trace_id)))
        .order_by(ExecutionTrace.started_at)
    )
    steps = result.scalars().all()

    trace_ids = {str(s.trace_id) for s in steps}
    traces = []
    for tid in trace_ids:
        trace_steps = [s for s in steps if str(s.trace_id) == tid]
        trace_steps.sort(key=lambda s: s.started_at)
        completed = [s for s in trace_steps if s.duration_ms is not None]
        total = round(sum(s.duration_ms for s in completed), 2) if completed else None
        traces.append(
            TraceResponse(
                trace_id=tid,
                job_id=str(trace_steps[0].job_id) if trace_steps[0].job_id else None,
                steps=[_step_to_response(s) for s in trace_steps],
                total_duration_ms=total,
                status=trace_steps[-1].status,
            )
        )
    return traces


@router.get("/traces/metrics/summary")
async def get_trace_metrics(
    minutes: int = Query(60, ge=5, le=43200),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import func as sa_func

    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    base = [ExecutionTrace.company_id == current_user.company_id, ExecutionTrace.started_at >= cutoff]

    step_stats = await db.execute(
        select(
            ExecutionTrace.step_name,
            sa_func.count(ExecutionTrace.id).label("total"),
            sa_func.sum(sa_func.cast(ExecutionTrace.status == "failed", sa_func.Integer)).label("failures"),
            sa_func.avg(ExecutionTrace.duration_ms).label("avg_ms"),
            sa_func.percentile_cont(0.5).within_group(ExecutionTrace.duration_ms).label("p50_ms"),
            sa_func.percentile_cont(0.95).within_group(ExecutionTrace.duration_ms).label("p95_ms"),
            sa_func.percentile_cont(0.99).within_group(ExecutionTrace.duration_ms).label("p99_ms"),
            sa_func.max(ExecutionTrace.duration_ms).label("max_ms"),
        )
        .where(*base, ExecutionTrace.duration_ms.isnot(None))
        .group_by(ExecutionTrace.step_name)
    )

    steps = {}
    for row in step_stats:
        total = row.total or 0
        failures = row.failures or 0
        steps[row.step_name] = {
            "total": total,
            "failures": failures,
            "failure_rate": round(failures / total, 4) if total > 0 else 0,
            "avg_ms": round(row.avg_ms, 2) if row.avg_ms else 0,
            "p50_ms": round(row.p50_ms, 2) if row.p50_ms else 0,
            "p95_ms": round(row.p95_ms, 2) if row.p95_ms else 0,
            "p99_ms": round(row.p99_ms, 2) if row.p99_ms else 0,
            "max_ms": round(row.max_ms, 2) if row.max_ms else 0,
        }

    total_traces = await db.execute(select(sa_func.count(sa_func.distinct(ExecutionTrace.trace_id))).where(*base))
    trace_count = total_traces.scalar() or 0

    failed_traces = await db.execute(
        select(sa_func.count(sa_func.distinct(ExecutionTrace.trace_id))).where(*base, ExecutionTrace.status == "failed")
    )
    failed_count = failed_traces.scalar() or 0

    return {
        "time_window_minutes": minutes,
        "total_executions": trace_count,
        "failed_executions": failed_count,
        "overall_failure_rate": round(failed_count / trace_count, 4) if trace_count > 0 else 0,
        "per_step": steps,
    }


def _step_to_response(s: ExecutionTrace) -> TraceStepResponse:
    return TraceStepResponse(
        id=str(s.id),
        trace_id=str(s.trace_id),
        job_id=str(s.job_id) if s.job_id else None,
        company_id=str(s.company_id) if s.company_id else None,
        step_name=s.step_name,
        status=s.status,
        started_at=s.started_at.isoformat() if s.started_at else "",
        completed_at=s.completed_at.isoformat() if s.completed_at else None,
        duration_ms=s.duration_ms,
        metadata=s.trace_metadata,
        error_message=s.error_message,
    )
