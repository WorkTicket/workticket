import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, case, func, select

from app.analytics.events import AnalyticsEvent
from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

EVENT_TIME = func.coalesce(AnalyticsEvent.client_timestamp, AnalyticsEvent.timestamp)


async def get_tttj(
    company_id: str | None = None,
    days: int = 90,
    limit: int | None = None,
    offset: int | None = 0,
) -> list[dict]:
    cutoff = datetime.now(UTC) - timedelta(days=days)

    created = AnalyticsEvent.__table__.alias("created")
    approved = AnalyticsEvent.__table__.alias("approved")

    approved_time = func.coalesce(approved.c.client_timestamp, approved.c.timestamp)
    created_time = func.coalesce(created.c.client_timestamp, created.c.timestamp)

    base_join = and_(
        approved.c.event_name == "job_approved_without_changes",
        approved.c.company_id == created.c.company_id,
        approved.c.job_id == created.c.job_id,
    )
    created_filter = and_(
        created.c.event_name == "job_created",
        created.c.timestamp >= cutoff,
    )
    if company_id:
        created_filter = and_(created_filter, created.c.company_id == company_id)

    delta = func.extract("epoch", approved_time) - func.extract("epoch", created_time)

    stmt = (
        select(
            created.c.company_id,
            func.avg(delta).label("avg_tttj_seconds"),
            func.percentile_cont(0.5).within_group(delta).label("median_tttj_seconds"),
            func.count().label("sample_size"),
        )
        .select_from(created.join(approved, base_join))
        .where(created_filter)
        .group_by(created.c.company_id)
        .order_by(created.c.company_id)
    )

    if limit:
        stmt = stmt.limit(limit).offset(offset or 0)

    async with AsyncSessionLocal() as db:
        result = await db.execute(stmt)
        rows = [
            {
                "company_id": str(row.company_id),
                "avg_tttj_hours": round(row.avg_tttj_seconds / 3600, 2) if row.avg_tttj_seconds else 0,
                "median_tttj_hours": round(row.median_tttj_seconds / 3600, 2) if row.median_tttj_seconds else 0,
                "sample_size": row.sample_size or 0,
            }
            for row in result
        ]
        return rows


async def get_edit_rate_analysis(
    company_id: str | None = None,
    days: int = 90,
) -> dict:
    cutoff = datetime.now(UTC) - timedelta(days=days)

    async with AsyncSessionLocal() as db:
        ai_filter = [AnalyticsEvent.event_name == "ai_output_generated", AnalyticsEvent.timestamp >= cutoff]
        if company_id:
            ai_filter.append(AnalyticsEvent.company_id == company_id)

        jobs_with_ai = (
            select(
                AnalyticsEvent.job_id,
                AnalyticsEvent.company_id,
            )
            .where(
                *ai_filter,
            )
            .group_by(AnalyticsEvent.job_id, AnalyticsEvent.company_id)
            .subquery()
        )

        total_jobs = await db.execute(select(func.count()).select_from(jobs_with_ai))
        total_with_ai = total_jobs.scalar() or 0

        edited_filter = [AnalyticsEvent.event_name == "ai_output_edited", AnalyticsEvent.timestamp >= cutoff]
        if company_id:
            edited_filter.append(AnalyticsEvent.company_id == company_id)

        jobs_edited = (
            select(
                AnalyticsEvent.job_id,
            )
            .where(
                *edited_filter,
            )
            .group_by(AnalyticsEvent.job_id)
            .subquery()
        )

        edited_count = await db.execute(select(func.count()).select_from(jobs_edited))
        total_edited = edited_count.scalar() or 0

        field_breakdown = await db.execute(
            select(
                func.jsonb_array_elements_text(AnalyticsEvent.event_metadata["edited_fields"]).label("field_name"),
                func.count().label("count"),
            )
            .where(
                *edited_filter,
                AnalyticsEvent.event_metadata["edited_fields"].isnot(None),
            )
            .group_by(func.jsonb_array_elements_text(AnalyticsEvent.event_metadata["edited_fields"]))
            .order_by(func.count().desc())
        )

        edits_per_job = await db.execute(
            select(
                func.avg(select(func.count()).where(*edited_filter).group_by(AnalyticsEvent.job_id).subquery().c.count)
            )
        )
        avg_edits = edits_per_job.scalar() or 0

        return {
            "total_jobs_with_ai_output": total_with_ai,
            "jobs_with_edits": total_edited,
            "percent_edited": round(total_edited / total_with_ai * 100, 2) if total_with_ai > 0 else 0,
            "avg_edits_per_edited_job": round(avg_edits, 2),
            "most_edited_fields": [{"field": r.field_name, "count": r.count} for r in field_breakdown],
        }


async def get_voice_impact_comparison(
    company_id: str | None = None,
    days: int = 90,
) -> dict:
    cutoff = datetime.now(UTC) - timedelta(days=days)

    voice_filter = [AnalyticsEvent.event_name == "voice_used", AnalyticsEvent.timestamp >= cutoff]
    all_filter = [AnalyticsEvent.event_name == "ai_output_generated", AnalyticsEvent.timestamp >= cutoff]
    if company_id:
        voice_filter.append(AnalyticsEvent.company_id == company_id)
        all_filter.append(AnalyticsEvent.company_id == company_id)

    async with AsyncSessionLocal() as db:
        voice_jobs = (
            select(
                AnalyticsEvent.job_id,
                AnalyticsEvent.company_id,
            )
            .where(
                *voice_filter,
            )
            .group_by(AnalyticsEvent.job_id, AnalyticsEvent.company_id)
            .subquery()
        )

        all_jobs = (
            select(
                AnalyticsEvent.job_id,
                AnalyticsEvent.company_id,
            )
            .where(
                *all_filter,
            )
            .group_by(AnalyticsEvent.job_id, AnalyticsEvent.company_id)
            .subquery()
        )

        voice_label = case(
            (all_jobs.c.job_id.in_(select(voice_jobs.c.job_id)), "voice"),
            else_="non_voice",
        ).label("group")

        stats = await db.execute(
            select(
                voice_label,
                func.count(func.distinct(all_jobs.c.job_id)).label("job_count"),
            )
            .select_from(all_jobs)
            .group_by(voice_label)
        )

        result = {}
        for row in stats:
            group = row.group
            job_count = row.job_count

            subq_filter = [AnalyticsEvent.job_id.in_(select(all_jobs.c.job_id))]
            if company_id:
                subq_filter.append(AnalyticsEvent.company_id == company_id)

            edited = await db.execute(
                select(func.count(func.distinct(AnalyticsEvent.job_id))).where(
                    AnalyticsEvent.event_name == "ai_output_edited",
                    AnalyticsEvent.timestamp >= cutoff,
                    *subq_filter,
                    AnalyticsEvent.job_id.in_(
                        select(all_jobs.c.job_id).where(
                            case((all_jobs.c.job_id.in_(select(voice_jobs.c.job_id)), True), else_=False)
                            == (group == "voice")
                        )
                    ),
                )
            )
            edited_count = edited.scalar() or 0

            approved_without = await db.execute(
                select(func.count(func.distinct(AnalyticsEvent.job_id))).where(
                    AnalyticsEvent.event_name == "job_approved_without_changes",
                    AnalyticsEvent.timestamp >= cutoff,
                    *subq_filter,
                    AnalyticsEvent.job_id.in_(
                        select(all_jobs.c.job_id).where(
                            case((all_jobs.c.job_id.in_(select(voice_jobs.c.job_id)), True), else_=False)
                            == (group == "voice")
                        )
                    ),
                )
            )
            approved_without_count = approved_without.scalar() or 0

            approved_with = await db.execute(
                select(func.count(func.distinct(AnalyticsEvent.job_id))).where(
                    AnalyticsEvent.event_name == "job_approved_with_changes",
                    AnalyticsEvent.timestamp >= cutoff,
                    *subq_filter,
                    AnalyticsEvent.job_id.in_(
                        select(all_jobs.c.job_id).where(
                            case((all_jobs.c.job_id.in_(select(voice_jobs.c.job_id)), True), else_=False)
                            == (group == "voice")
                        )
                    ),
                )
            )
            approved_with_count = approved_with.scalar() or 0

            result[group] = {
                "job_count": job_count,
                "edit_rate": round(edited_count / job_count * 100, 2) if job_count > 0 else 0,
                "approval_rate_without_changes": round(approved_without_count / job_count * 100, 2)
                if job_count > 0
                else 0,
                "approval_rate_with_changes": round(approved_with_count / job_count * 100, 2) if job_count > 0 else 0,
            }

        return result
