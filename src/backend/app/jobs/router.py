from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.analytics import EVENT_JOB_CREATED, EVENT_JOB_REOPENED, log_event
from app.auth.authorize import require_staff
from app.auth.dependencies import get_current_user
from app.database import get_db
from app.jobs.models import AIProcessingState, Customer, Job, JobStatus, Quote, User
from app.jobs.schemas import (
    CustomerCreate,
    CustomerListResponse,
    CustomerResponse,
    JobCreate,
    JobListResponse,
    JobResponse,
    JobUpdate,
)

router = APIRouter()


@router.post("", response_model=JobResponse, status_code=201, dependencies=[Depends(require_staff)])
async def create_job(
    payload: JobCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = Job(
        company_id=current_user.company_id,
        customer_id=payload.customer_id,
        technician_id=payload.technician_id or current_user.id,
        description=payload.description,
        scheduled_time=payload.scheduled_time or datetime.now(UTC),
        address=payload.address,
    )
    db.add(job)
    await db.flush()
    await db.refresh(
        job,
        [
            "id",
            "company_id",
            "customer_id",
            "technician_id",
            "status",
            "scheduled_time",
            "description",
            "address",
            "created_at",
            "updated_at",
        ],
    )

    await log_event(
        event_name=EVENT_JOB_CREATED,
        user_id=current_user.id,
        company_id=str(current_user.company_id),
        job_id=str(job.id),
        metadata={"customer_id": str(payload.customer_id), "has_description": bool(payload.description)},
    )

    return job


@router.get("", response_model=JobListResponse)
async def list_jobs(
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = (
        select(Job)
        .options(selectinload(Job.technician), selectinload(Job.media))
        .where(Job.company_id == current_user.company_id, Job.is_deleted.is_(False))
    )
    if status:
        query = query.where(Job.status == status)
    if current_user.role == "technician":
        query = query.where(Job.technician_id == current_user.id)

    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # Note: For very large datasets (10K+ rows), consider migrating this
    # endpoint from OFFSET pagination to keyset (cursor-based) pagination
    # using (scheduled_time, id) as the cursor composite key.
    query = query.order_by(Job.scheduled_time.desc(), Job.id.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    jobs = result.scalars().all()

    return JobListResponse(jobs=[JobResponse.model_validate(j) for j in jobs], total=total)


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Job)
        .options(selectinload(Job.technician), selectinload(Job.media), selectinload(Job.ai_outputs))
        .where(Job.id == job_id, Job.company_id == current_user.company_id, Job.is_deleted.is_(False))
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.patch("/{job_id}", response_model=JobResponse, dependencies=[Depends(require_staff)])
async def update_job(
    job_id: UUID,
    payload: JobUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.company_id == current_user.company_id, Job.is_deleted.is_(False))
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    update_data = payload.model_dump(exclude_unset=True)
    if "status" in update_data:
        valid_statuses = {s.value for s in JobStatus}
        if update_data["status"] not in valid_statuses:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status '{update_data['status']}'. Must be one of: {', '.join(sorted(valid_statuses))}",
            )
        # B-1 FIX: Prevent job completion when AI processing is still active.
        # AI processing state and job status are now coupled - a job cannot
        # be marked completed while AI is still queued, reserved, or processing.
        if update_data["status"] == "completed":
            blocked_states = {
                AIProcessingState.queued.value,
                AIProcessingState.reserved.value,
                AIProcessingState.processing.value,
            }
            if job.ai_processing_state in blocked_states:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot complete job while AI processing is in '{job.ai_processing_state}' state. Wait for AI to finish or cancel the AI job first.",
                )
    if "customer_id" in update_data:
        result = await db.execute(
            select(Customer).where(
                Customer.id == update_data["customer_id"], Customer.company_id == current_user.company_id
            )
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Customer not found")
    if "technician_id" in update_data:
        result = await db.execute(
            select(User).where(User.id == update_data["technician_id"], User.company_id == current_user.company_id)
        )
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Technician not found in your company")

    # D-1 FIX: Record immutable audit log entry for job status changes
    previous_status = job.status
    was_completed = previous_status == "completed"
    for key, value in update_data.items():
        setattr(job, key, value)
    if was_completed and job.status != "completed":
        await log_event(
            event_name=EVENT_JOB_REOPENED,
            user_id=current_user.id,
            company_id=str(current_user.company_id),
            job_id=str(job.id),
            metadata={"previous_status": "completed", "new_status": job.status},
        )

    # D-1 FIX: Record immutable audit log for status changes
    if "status" in update_data and update_data["status"] != previous_status:
        from app.jobs.models import JobAuditLog

        audit_entry = JobAuditLog(
            job_id=job.id,
            company_id=job.company_id,
            changed_by_user_id=current_user.id,
            field_name="status",
            old_value=previous_status,
            new_value=job.status,
        )
        db.add(audit_entry)

    await db.flush()
    await db.refresh(job)
    return job


@router.delete("/{job_id}", status_code=204, dependencies=[Depends(require_staff)])
async def delete_job(
    job_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.company_id == current_user.company_id, Job.is_deleted.is_(False))
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    result = await db.execute(
        select(Quote).where(
            Quote.job_id == job_id, Quote.company_id == current_user.company_id, Quote.status == "draft"
        )
    )
    active_quote = result.scalar_one_or_none()
    if active_quote:
        raise HTTPException(status_code=400, detail="Cannot delete job with an active quote. Delete the quote first.")

    job.soft_delete()
    await db.flush()
    return None


@router.post("/customers", response_model=CustomerResponse, status_code=201, dependencies=[Depends(require_staff)])
async def create_customer(
    payload: CustomerCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    customer = Customer(
        company_id=current_user.company_id,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        address=payload.address,
    )
    db.add(customer)
    await db.flush()
    await db.refresh(customer)
    return customer


@router.get("/customers", response_model=CustomerListResponse)
async def list_customers(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = select(Customer).where(Customer.company_id == current_user.company_id).order_by(Customer.name)

    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar()

    # Apply pagination
    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    customers = result.scalars().all()

    return CustomerListResponse(
        customers=[CustomerResponse.model_validate(c) for c in customers],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.delete("/customers/{customer_id}", status_code=204, dependencies=[Depends(require_staff)])
async def delete_customer(
    customer_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Customer).where(Customer.id == customer_id, Customer.company_id == current_user.company_id)
    )
    customer = result.scalar_one_or_none()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")

    job_count = await db.execute(
        select(func.count(Job.id)).where(Job.customer_id == customer_id, Job.company_id == current_user.company_id)
    )
    existing_jobs = job_count.scalar() or 0
    if existing_jobs > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete customer with {existing_jobs} existing job(s). Reassign or delete jobs first.",
        )

    await db.delete(customer)
    await db.flush()
    return None
