from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.rate_limiter import rate_limiter
from app.analytics.events import AnalyticsEvent, log_event
from app.analytics.pagination import (
    CursorPaginatedResponse,
    CursorPaginationParams,
    PaginatedResponse,
    PaginationParams,
    decode_cursor,
    encode_cursor,
)
from app.analytics.queries import get_edit_rate_analysis, get_tttj, get_voice_impact_comparison
from app.auth.dependencies import get_current_user
from app.database import get_db, get_db_readonly
from app.jobs.models import User

router = APIRouter()


class AnalyticsEventRequest(BaseModel):
    event_name: str
    job_id: str | None = None
    client_timestamp: datetime | None = None
    metadata: dict | None = None


class AIOutputEditRequest(BaseModel):
    job_id: str
    edited_fields: list[str]
    new_values: dict = {}
    technician_id: str | None = None


@router.post("/events", status_code=201)
async def record_event(
    payload: AnalyticsEventRequest,
    current_user: User = Depends(get_current_user),
):
    allowed = await rate_limiter.check_user(str(current_user.id) + ":analytics")
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many analytics events")

    await log_event(
        event_name=payload.event_name,
        user_id=current_user.id,
        company_id=str(current_user.company_id),
        job_id=payload.job_id,
        client_timestamp=payload.client_timestamp,
        metadata=payload.metadata,
    )
    return {"status": "ok"}


@router.post("/events/edit-ai-output", status_code=201)
async def record_ai_output_edit(
    payload: AIOutputEditRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import json

    from app.analytics.events import EVENT_AI_OUTPUT_EDITED
    from app.jobs.models import AIOutput

    fields_changed = payload.edited_fields
    new_values = payload.new_values or {}

    original = None
    result = await db.execute(
        select(AIOutput)
        .where(
            AIOutput.job_id == payload.job_id,
            AIOutput.company_id == current_user.company_id,
            AIOutput.output_type == "job_analysis",
        )
        .order_by(AIOutput.created_at.desc())
    )
    stored = result.scalar_one_or_none()
    if stored and stored.json_result:
        original = json.loads(stored.json_result)

    max_magnitude = "small"
    for field in fields_changed:
        orig_val = original.get(field) if original else None
        new_val = new_values.get(field)
        if (
            orig_val is not None
            and new_val is not None
            and isinstance(orig_val, (int, float))
            and isinstance(new_val, (int, float))
        ):
            if orig_val > 0:
                ratio = abs(new_val - orig_val) / orig_val
            else:
                ratio = abs(new_val - orig_val) if new_val != orig_val else 0
            if ratio > 0.5:
                max_magnitude = "large"
            elif ratio > 0.1 and max_magnitude != "large":
                max_magnitude = "medium"

    await log_event(
        event_name=EVENT_AI_OUTPUT_EDITED,
        user_id=current_user.id,
        company_id=str(current_user.company_id),
        job_id=payload.job_id,
        metadata={
            "edited_fields": fields_changed,
            "new_values": new_values,
            "field_count": len(fields_changed),
            "magnitude": max_magnitude,
            "technician_id": payload.technician_id,
        },
    )
    return {"status": "ok"}


class EventCountResponse(BaseModel):
    event_name: str
    count: int


@router.get("/events/counts")
async def get_event_counts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_readonly),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
):
    base = (
        select(
            AnalyticsEvent.event_name,
            func.count(AnalyticsEvent.id).label("count"),
        )
        .where(AnalyticsEvent.company_id == current_user.company_id)
        .group_by(AnalyticsEvent.event_name)
    )

    count_query = select(func.count()).select_from(base.subquery())
    total = (await db.execute(count_query)).scalar() or 0

    result = await db.execute(base.order_by(desc("count")).offset((page - 1) * page_size).limit(page_size))
    rows = result.all()
    total_pages = (total + page_size - 1) // page_size
    return {
        "events": [{"event_name": r.event_name, "count": r.count} for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.get("/queries/tttj")
async def query_tttj(
    days: int = Query(90, ge=1, le=365),
    current_user: User = Depends(get_current_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    result = await get_tttj(
        company_id=str(current_user.company_id),
        days=days,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    return {"tttj": result, "time_window_days": days, "page": page, "page_size": page_size}


@router.get("/queries/edit-rate")
async def query_edit_rate(
    days: int = Query(90, ge=1, le=365),
    current_user: User = Depends(get_current_user),
):
    result = await get_edit_rate_analysis(company_id=str(current_user.company_id), days=days)
    return {"edit_analysis": result, "time_window_days": days}


@router.get("/queries/voice-impact")
async def query_voice_impact(
    days: int = Query(90, ge=1, le=365),
    current_user: User = Depends(get_current_user),
):
    result = await get_voice_impact_comparison(company_id=str(current_user.company_id), days=days)
    return {"voice_impact": result, "time_window_days": days}


@router.get("/events", response_model=PaginatedResponse[dict])
async def list_analytics_events(
    pagination: PaginationParams = Depends(),
    event_name: str | None = Query(None),
    user_id: str | None = Query(None),
    job_id: UUID | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_readonly),
):
    # Base query
    query = select(AnalyticsEvent).where(AnalyticsEvent.company_id == current_user.company_id)

    # Apply filters
    if event_name:
        query = query.where(AnalyticsEvent.event_name == event_name)
    if user_id:
        query = query.where(AnalyticsEvent.user_id == user_id)
    if job_id:
        query = query.where(AnalyticsEvent.job_id == job_id)

    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # Apply pagination and ordering
    query = (
        query.order_by(AnalyticsEvent.timestamp.desc())
        .offset((pagination.page - 1) * pagination.page_size)
        .limit(pagination.page_size)
    )

    # Execute query
    result = await db.execute(query)
    events = result.scalars().all()

    # Convert to dict for response
    event_dicts = []
    for event in events:
        event_dict = {
            "id": str(event.id),
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
            "client_timestamp": event.client_timestamp.isoformat() if event.client_timestamp else None,
            "event_name": event.event_name,
            "user_id": event.user_id,
            "company_id": str(event.company_id),
            "job_id": str(event.job_id) if event.job_id else None,
            "metadata": event.event_metadata,
        }
        event_dicts.append(event_dict)

    # Calculate total pages
    total_pages = (total + pagination.page_size - 1) // pagination.page_size

    return PaginatedResponse(
        items=event_dicts, total=total, page=pagination.page, page_size=pagination.page_size, total_pages=total_pages
    )


@router.get("/events/cursor", response_model=CursorPaginatedResponse[dict])
async def list_analytics_events_cursor(
    pagination: CursorPaginationParams = Depends(),
    event_name: str | None = Query(None),
    user_id: str | None = Query(None),
    job_id: UUID | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_readonly),
):
    """SC1: Cursor-based analytics event listing using opaque cursor for performant pagination.

    Uses timestamp+id based cursors to avoid the performance penalty of large
    OFFSET values on time-series data. Supports bidirectional traversal.
    """
    query = select(AnalyticsEvent).where(AnalyticsEvent.company_id == current_user.company_id)

    if event_name:
        query = query.where(AnalyticsEvent.event_name == event_name)
    if user_id:
        query = query.where(AnalyticsEvent.user_id == user_id)
    if job_id:
        query = query.where(AnalyticsEvent.job_id == job_id)

    # Decode cursor for keyset pagination
    if pagination.cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(pagination.cursor)
            if pagination.direction == "next":
                query = query.where(
                    (AnalyticsEvent.timestamp < cursor_ts)
                    | ((AnalyticsEvent.timestamp == cursor_ts) & (AnalyticsEvent.id < cursor_id))
                )
            else:
                query = query.where(
                    (AnalyticsEvent.timestamp > cursor_ts)
                    | ((AnalyticsEvent.timestamp == cursor_ts) & (AnalyticsEvent.id > cursor_id))
                )
        except ValueError as e:
            raise HTTPException(status_code=400, detail="Invalid cursor format") from e

    query = query.order_by(
        desc(AnalyticsEvent.timestamp),
        desc(AnalyticsEvent.id),
    ).limit(pagination.page_size + 1)  # +1 to detect has_more

    result = await db.execute(query)
    events = result.scalars().all()

    has_more = len(events) > pagination.page_size
    if has_more:
        events = events[: pagination.page_size]

    # Reorder if prev direction
    if pagination.direction == "prev":
        events = list(reversed(events))

    event_dicts = [
        {
            "id": str(event.id),
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
            "client_timestamp": event.client_timestamp.isoformat() if event.client_timestamp else None,
            "event_name": event.event_name,
            "user_id": event.user_id,
            "company_id": str(event.company_id),
            "job_id": str(event.job_id) if event.job_id else None,
            "metadata": event.event_metadata,
        }
        for event in events
    ]

    next_cursor = None
    prev_cursor = None
    if event_dicts:
        last = events[-1] if pagination.direction == "next" else events[0]
        first = events[0] if pagination.direction == "next" else events[-1]
        if has_more or pagination.cursor:
            next_cursor = encode_cursor(last.timestamp, str(last.id)) if last.timestamp else None
        if pagination.cursor:
            prev_cursor = encode_cursor(first.timestamp, str(first.id)) if first.timestamp else None

    return CursorPaginatedResponse(
        items=event_dicts,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
        page_size=pagination.page_size,
        has_more=has_more,
    )
