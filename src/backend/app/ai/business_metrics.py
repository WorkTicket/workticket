import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select

from app.ai.audit import AIAuditLog
from app.database import AsyncSessionLocal
from app.jobs.models import Job

logger = logging.getLogger(__name__)


class BusinessMetrics:
    @staticmethod
    async def get_overview(minutes: int = 1440, company_id: str | None = None) -> dict:
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        async with AsyncSessionLocal() as db:
            base_filter = [AIAuditLog.created_at >= cutoff]
            if company_id:
                base_filter.append(AIAuditLog.company_id == company_id)

            total_audit = await db.execute(select(func.count(AIAuditLog.id)).where(*base_filter))
            total_count = total_audit.scalar() or 0

            users_q = await db.execute(
                select(func.count(func.distinct(AIAuditLog.user_id))).where(
                    *base_filter, AIAuditLog.user_id.isnot(None)
                )
            )
            active_users = users_q.scalar() or 0

            tenants_q = await db.execute(
                select(func.count(func.distinct(AIAuditLog.company_id))).where(
                    *base_filter, AIAuditLog.company_id.isnot(None)
                )
            )
            active_tenants = tenants_q.scalar() or 0

            job_filter = [Job.created_at >= cutoff]
            if company_id:
                job_filter.append(Job.company_id == company_id)
            total_jobs = await db.execute(select(func.count(Job.id)).where(*job_filter))
            job_count = total_jobs.scalar() or 0

            return {
                "total_ai_requests": total_count,
                "active_users": active_users,
                "active_tenants": active_tenants,
                "total_jobs_created": job_count,
                "time_window_hours": round(minutes / 60, 1),
            }

    @staticmethod
    async def get_feature_usage(minutes: int = 1440, company_id: str | None = None) -> dict:
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        async with AsyncSessionLocal() as db:
            base_filter = [AIAuditLog.created_at >= cutoff]
            if company_id:
                base_filter.append(AIAuditLog.company_id == company_id)

            rows = await db.execute(
                select(
                    AIAuditLog.request_type,
                    func.count(AIAuditLog.id).label("count"),
                    func.sum(case((AIAuditLog.success.is_(True), 1), else_=0)).label("successes"),
                    func.avg(AIAuditLog.latency_ms).label("avg_latency"),
                    func.max(AIAuditLog.latency_ms).label("max_latency"),
                )
                .where(*base_filter)
                .group_by(AIAuditLog.request_type)
            )
            features = {}
            for row in rows:
                total = row.count or 0
                successes = row.successes or 0
                features[row.request_type] = {
                    "count": total,
                    "success_rate": round(successes / total, 4) if total > 0 else 0,
                    "avg_latency_ms": round(row.avg_latency, 2) if row.avg_latency else 0,
                    "max_latency_ms": round(row.max_latency, 2) if row.max_latency else 0,
                }
            return features

    @staticmethod
    async def get_per_tenant(minutes: int = 1440, company_id: str | None = None) -> list:
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        async with AsyncSessionLocal() as db:
            base_filter = [
                AIAuditLog.created_at >= cutoff,
                AIAuditLog.company_id.isnot(None),
            ]
            if company_id:
                base_filter.append(AIAuditLog.company_id == company_id)

            rows = await db.execute(
                select(
                    AIAuditLog.company_id,
                    func.count(AIAuditLog.id).label("requests"),
                    func.sum(case((AIAuditLog.success.is_(True), 1), else_=0)).label("successes"),
                    func.avg(AIAuditLog.latency_ms).label("avg_latency"),
                    func.count(func.distinct(AIAuditLog.user_id)).label("users"),
                )
                .where(*base_filter)
                .group_by(AIAuditLog.company_id)
                .order_by(func.count(AIAuditLog.id).desc())
            )
            tenants = []
            for row in rows:
                total = row.requests or 0
                successes = row.successes or 0
                tenants.append(
                    {
                        "company_id": str(row.company_id),
                        "total_requests": total,
                        "success_rate": round(successes / total, 4) if total > 0 else 0,
                        "avg_latency_ms": round(row.avg_latency, 2) if row.avg_latency else 0,
                        "active_users": row.users or 0,
                    }
                )
            return tenants

    @staticmethod
    async def get_cost_estimate(minutes: int = 1440, company_id: str | None = None) -> dict:
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        async with AsyncSessionLocal() as db:
            base_filter = [AIAuditLog.created_at >= cutoff]
            if company_id:
                base_filter.append(AIAuditLog.company_id == company_id)

            total_cpu = await db.execute(select(func.sum(AIAuditLog.cpu_time_ms)).where(*base_filter))
            cpu_ms = total_cpu.scalar() or 0

            total_memory = await db.execute(
                select(func.avg(AIAuditLog.memory_estimate_bytes)).where(
                    *base_filter,
                    AIAuditLog.memory_estimate_bytes.isnot(None),
                )
            )
            avg_mem = total_memory.scalar() or 0

            peak_q = await db.execute(
                select(func.max(AIAuditLog.queue_pressure_at_time)).where(
                    *base_filter,
                    AIAuditLog.queue_pressure_at_time.isnot(None),
                )
            )
            peak_queue = peak_q.scalar() or 0

            request_count = await db.execute(select(func.count(AIAuditLog.id)).where(*base_filter))
            total_req = request_count.scalar() or 0

            type_breakdown = await db.execute(
                select(
                    AIAuditLog.request_type,
                    func.count(AIAuditLog.id).label("count"),
                    func.avg(AIAuditLog.latency_ms).label("avg_latency_ms"),
                )
                .where(*base_filter)
                .group_by(AIAuditLog.request_type)
            )

            per_type = {}
            for row in type_breakdown:
                per_type[row.request_type] = {
                    "count": row.count or 0,
                    "avg_latency_ms": round(row.avg_latency_ms, 2) if row.avg_latency_ms else 0,
                }

            return {
                "total_requests": total_req,
                "total_cpu_time_seconds": round(cpu_ms / 1000, 2),
                "avg_cpu_time_per_request_ms": round(cpu_ms / total_req, 2) if total_req > 0 else 0,
                "avg_memory_per_request_bytes": round(avg_mem, 2) if avg_mem else 0,
                "avg_memory_per_request_mb": round(avg_mem / (1024 * 1024), 2) if avg_mem else 0,
                "peak_queue_depth": peak_queue,
                "per_request_type": per_type,
                "estimated_cost_usd": 0.0,
                "note": "Zero-cost AI (Ollama + Whisper local). Cost is CPU/memory only.",
            }

    @staticmethod
    async def get_completion_times(minutes: int = 1440, company_id: str | None = None) -> dict:
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        async with AsyncSessionLocal() as db:
            base_filter = [AIAuditLog.created_at >= cutoff, AIAuditLog.latency_ms > 0]
            if company_id:
                base_filter.insert(1, AIAuditLog.company_id == company_id)

            row = await db.execute(
                select(
                    func.avg(AIAuditLog.latency_ms).label("avg"),
                    func.percentile_cont(0.5).within_group(AIAuditLog.latency_ms).label("p50"),
                    func.percentile_cont(0.95).within_group(AIAuditLog.latency_ms).label("p95"),
                    func.percentile_cont(0.99).within_group(AIAuditLog.latency_ms).label("p99"),
                    func.min(AIAuditLog.latency_ms).label("min"),
                    func.max(AIAuditLog.latency_ms).label("max"),
                ).where(*base_filter)
            )
            r = row.one()
            return {
                "avg_ms": round(r.avg, 2) if r.avg else 0,
                "p50_ms": round(r.p50, 2) if r.p50 else 0,
                "p95_ms": round(r.p95, 2) if r.p95 else 0,
                "p99_ms": round(r.p99, 2) if r.p99 else 0,
                "min_ms": round(r.min, 2) if r.min else 0,
                "max_ms": round(r.max, 2) if r.max else 0,
            }

    @staticmethod
    async def get_all(minutes: int = 1440, company_id: str | None = None) -> dict:
        overview, features, tenants, costs, completion = await asyncio.gather(
            BusinessMetrics.get_overview(minutes, company_id=company_id),
            BusinessMetrics.get_feature_usage(minutes, company_id=company_id),
            BusinessMetrics.get_per_tenant(minutes, company_id=company_id),
            BusinessMetrics.get_cost_estimate(minutes, company_id=company_id),
            BusinessMetrics.get_completion_times(minutes, company_id=company_id),
        )
        return {
            "overview": overview,
            "feature_usage": features,
            "per_tenant": tenants,
            "cost_estimate": costs,
            "completion_times": completion,
            "time_window_minutes": minutes,
        }


business_metrics = BusinessMetrics()
