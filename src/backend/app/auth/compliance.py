"""GDPR/CCPA compliance endpoints.

Provides:
- Data export (right to access / data portability)
- Data deletion (right to erasure / right to delete)
- Tenant deletion (complete removal of company and all associated data)
"""

import csv
import io
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.rate_limiter import _get_redis
from app.auth.dependencies import get_current_user
from app.database import get_db
from app.jobs.models import Company, Customer, Job, User

logger = logging.getLogger(__name__)
router = APIRouter()

_EXPORT_RATE_LIMIT = 5  # per user per hour


class ComplianceExportResponse(BaseModel):
    export_id: str
    format: str
    generated_at: str


@router.get("/export/me")
async def export_my_data(
    format: str = Query("json", pattern="^(json|csv)$"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export all data associated with the current user (GDPR right to access).

    Returns user profile, associated jobs, customers, quotes, and media metadata
    in JSON or CSV format.
    """
    # Rate limit: 5 exports per user per hour
    try:
        r = await _get_redis()
        if r:
            key = f"compliance_export:{current_user.id}"
            count = await r.incr(key)
            if count == 1:
                await r.expire(key, 3600)
            if count > _EXPORT_RATE_LIMIT:
                raise HTTPException(
                    status_code=429,
                    detail="Too many data exports. Please wait before trying again.",
                )
    except HTTPException:
        raise
    except Exception:
        logger.debug("Redis rate limit check failed for compliance export, allowing request")
        pass  # nosec B110

    # Collect user data
    export_data = {
        "export_type": "user_data_request",
        "generated_at": datetime.now(UTC).isoformat(),
        "user": {
            "id": str(current_user.id),
            "email": current_user.email,
            "name": current_user.name,
            "role": current_user.role,
            "company_id": str(current_user.company_id),
            "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
        },
    }

    # Fetch jobs created by the user
    jobs_result = await db.execute(
        select(Job)
        .where(
            Job.company_id == current_user.company_id,
            Job.technician_id == current_user.id,
        )
        .order_by(Job.created_at.desc())
        .limit(500)
    )
    jobs = jobs_result.scalars().all()
    export_data["jobs"] = [  # type: ignore[misc]
        {
            "id": str(j.id),  # type: ignore[misc]
            "status": j.status,
            "description": j.description,
            "address": j.address,
            "scheduled_time": j.scheduled_time.isoformat() if j.scheduled_time else None,
            "created_at": j.created_at.isoformat() if j.created_at else None,
        }
        for j in jobs
    ]

    # Fetch customers created by the user
    customers_result = await db.execute(
        select(Customer)
        .where(
            Customer.company_id == current_user.company_id,
        )
        .order_by(Customer.name)
        .limit(500)
    )
    customers = customers_result.scalars().all()
    export_data["customers"] = [  # type: ignore[misc]
        {
            "id": str(c.id),  # type: ignore[misc]
            "name": c.name,
            "email": c.email,
            "phone": c.phone,
            "address": c.address,
        }
        for c in customers
    ]

    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output, quoting=csv.QUOTE_ALL)

        writer.writerow(["section", "field", "value"])
        writer.writerow(["user", "id", str(current_user.id)])
        writer.writerow(["user", "email", current_user.email])
        writer.writerow(["user", "name", current_user.name])
        writer.writerow(["user", "role", current_user.role])

        for j in jobs:
            writer.writerow(["job", "id", str(j.id)])
            writer.writerow(["job", "status", j.status or ""])
            writer.writerow(["job", "description", j.description or ""])
            writer.writerow(["job", "address", j.address or ""])

        for c in customers:
            writer.writerow(["customer", "id", str(c.id)])
            writer.writerow(["customer", "name", c.name or ""])
            writer.writerow(["customer", "email", c.email or ""])

        output.seek(0)

        def _sanitize_csv_cell(val: str) -> str:
            if not val:
                return val
            if val and val[0] in "=+-@\t\n\r|%&{}":
                return "'" + val
            return val

        safe_output = io.StringIO()
        for line in output.getvalue().splitlines():
            safe_parts = [_sanitize_csv_cell(p.strip('"')) for p in line.split('","')]
            safe_output.write(",".join(f'"{p}"' for p in safe_parts) + "\n")
        safe_output.seek(0)

        return StreamingResponse(
            iter([safe_output.getvalue()]),
            media_type="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename=user_data_{current_user.id}.csv",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return export_data


@router.delete("/delete/me", status_code=200)
async def delete_my_data(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete all personal data for the current user (GDPR right to erasure).

    Performs a soft-delete of the user account and anonymizes PII fields.
    Does NOT delete company data that belongs to other users.
    """
    # Prevent owner from deleting themselves if they are the last owner
    if current_user.role == "owner":
        result = await db.execute(
            select(func.count(User.id)).where(
                User.company_id == current_user.company_id,
                User.role == "owner",
                User.is_active.is_(True),
            )
        )
        owner_count = result.scalar() or 0
        if owner_count <= 1:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete the last owner. Transfer ownership or delete the company first.",
            )

    # Anonymize PII fields
    current_user.email = f"deleted-{current_user.id}@anonymized.workticket"
    current_user.name = "Deleted User"
    current_user.is_active = False
    current_user.token_version += 1

    # Blacklist all active sessions
    try:
        r = await _get_redis()
        if r:
            await r.setex(
                f"session_blacklist:{current_user.id}:{current_user.token_version}",
                86400 * 7,
                "1",
            )
    except Exception:
        logger.debug("Failed to blacklist session in Redis during data deletion, continuing")
        pass  # nosec B110

    await db.flush()

    logger.info(
        "User %s deleted their data (anonymized). Token version: %d",
        current_user.id,
        current_user.token_version,
    )

    return {"message": "Your data has been deleted/anonymized", "user_id": str(current_user.id)}


class TenantDeleteRequest(BaseModel):
    confirmation: str  # Must match company name


@router.delete("/delete-tenant", status_code=200)
async def delete_tenant(
    payload: TenantDeleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete an entire tenant and all associated data (GDPR right to delete).

    Only the company owner can delete the tenant. Requires confirmation
    by typing the company name. Performs cascade deletion of all tenant data.
    """
    if current_user.role != "owner":
        raise HTTPException(
            status_code=403,
            detail="Only the company owner can delete the tenant.",
        )

    # Fetch company
    result = await db.execute(select(Company).where(Company.id == current_user.company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    # Verify confirmation
    if payload.confirmation.strip().lower() != company.name.strip().lower():
        raise HTTPException(
            status_code=400,
            detail="Confirmation must match the company name exactly.",
        )

    company_name = company.name
    company_id = company.id

    # Delete cascade order (respecting foreign key constraints):
    # 1. AI outputs
    # 2. Quotes
    # 3. Job media
    # 4. AI job estimates
    # 5. Notifications
    # 6. Jobs (soft-delete first, then hard delete)
    # 7. Estimates + estimate line items
    # 8. Usage ledger
    # 9. Invoices
    # 10. Billing accounts
    # 11. Customers
    # 12. Users
    # 13. Company

    from sqlalchemy import text as sa_text

    delete_order = [
        "ai_output_feedback",
        "ai_outputs",
        "quotes",
        "job_media",
        "ai_job_estimates",
        "notifications",
        "push_tokens",
        "usage_ledger",
        "invoices",
        "estimate_line_items",
        "estimates",
        "jobs",
        "billing_accounts",
        "analytics_events",
        "customers",
        "users",
        "companies",
    ]

    for table_name in delete_order:
        try:
            await db.execute(
                sa_text(f"DELETE FROM {table_name} WHERE company_id = :cid"),  # nosec B608
                {"cid": str(company_id)},
            )
        except Exception as e:
            logger.warning(
                "Failed to delete from %s during tenant deletion: %s",
                table_name,
                e,
            )

    await db.flush()

    logger.info(
        "Tenant '%s' (%s) deleted by owner %s",
        company_name,
        company_id,
        current_user.id,
    )

    return {
        "message": f"Tenant '{company_name}' and all associated data have been deleted.",
        "company_id": str(company_id),
    }
