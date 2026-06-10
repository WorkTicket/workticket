import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.rate_limiter import _get_redis, rate_limiter
from app.auth.dependencies import ClerkIdentity, get_clerk_identity, get_current_user
from app.billing.models import BillingAccount, Invoice, UsageLedger
from app.billing.quota_engine import quota_engine
from app.database import get_db
from app.jobs.models import AIOutput, Company, Customer, Job, Quote, User

logger = logging.getLogger(__name__)

router = APIRouter()

_RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local ttl = tonumber(ARGV[1])
local count = redis.call('INCR', key)
if count == 1 then
    redis.call('EXPIRE', key, ttl)
end
return count
"""


class ClerkAuthPayload(BaseModel):
    user_id: str
    email: str
    name: str
    company_name: str


@router.get("/registration-status")
async def registration_status(
    identity: ClerkIdentity = Depends(get_clerk_identity),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == identity.user_id))
    user = result.scalar_one_or_none()
    if user:
        return {
            "registered": True,
            "user_id": user.id,
            "company_id": str(user.company_id),
            "role": user.role,
        }
    return {"registered": False, "user_id": identity.user_id}


@router.post("/register")
async def register(
    request: Request,
    payload: ClerkAuthPayload,
    db: AsyncSession = Depends(get_db),
    identity: ClerkIdentity = Depends(get_clerk_identity),
):
    # IP/domain rate limits (pre-auth anti-abuse, independent of user identity)
    client_ip = request.client.host if request.client else "unknown"
    try:
        r = await _get_redis()
    except Exception:
        r = None

    if r:
        ip_key = f"register_ip:{client_ip}"
        ip_current = await r.eval(_RATE_LIMIT_SCRIPT, 1, ip_key, 3600)
        if ip_current > 10:
            logger.warning("IP rate limit hit on register: %s", client_ip)
            raise HTTPException(status_code=429, detail="Too many registration attempts from this IP. Try again later.")

        company_key = f"register_company:{payload.company_name.lower().strip()}"
        company_current = await r.eval(_RATE_LIMIT_SCRIPT, 1, company_key, 3600)
        if company_current > 5:
            logger.warning("Company-specific rate limit hit on register: %s", payload.company_name)
            raise HTTPException(
                status_code=429, detail="Too many registration attempts for this company. Try again later."
            )

        email_domain = payload.email.split("@")[-1].lower() if "@" in payload.email else "unknown"
        _disposable_domains = {
            "mailinator.com",
            "guerrillamail.com",
            "tempmail.com",
            "temp-mail.org",
            "10minutemail.com",
            "throwaway.email",
            "yopmail.com",
            "getnada.com",
            "sharklasers.com",
            "maildrop.cc",
            "trashmail.com",
            "mailnator.com",
            "dispostable.com",
            "mailcatch.com",
            "spamgourmet.com",
            "fakeinbox.com",
            "tempinbox.com",
            "mintemail.com",
            "mytrashmail.com",
            "mailexpire.com",
        }
        if email_domain in _disposable_domains:
            logger.warning("Disposable email domain blocked: %s", email_domain)
            raise HTTPException(
                status_code=400, detail="Disposable email addresses are not allowed. Use a permanent email address."
            )

        domain_key = f"register_domain:{email_domain}"
        domain_current = await r.eval(_RATE_LIMIT_SCRIPT, 1, domain_key, 86400)
        if domain_current > 50:
            logger.warning("Email domain rate limit hit on register: %s", email_domain)
            raise HTTPException(
                status_code=429, detail="Too many registrations from this email domain. Try again later."
            )

    if identity.user_id != payload.user_id:
        raise HTTPException(status_code=403, detail="Authenticated user_id does not match registration payload")

    # Per-user rate limit (after identity verification to prevent
    # attackers from exhausting global rate limit tokens with invalid user_ids).
    allowed = await rate_limiter.check_global()
    if not allowed:
        logger.warning("Rate limit hit on register: %s", payload.email)
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")

    result = await db.execute(select(Company).where(Company.name == payload.company_name))
    company = result.scalar_one_or_none()
    if not company:
        try:
            async with db.begin_nested():
                company = Company(name=payload.company_name, subscription_plan="free", trade_type="hvac")
                db.add(company)
            await quota_engine.get_or_create_account(db, company.id, "free")
        except IntegrityError as e:
            result = await db.execute(select(Company).where(Company.name == payload.company_name))
            existing = result.scalar_one_or_none()
            if existing:
                company = existing
            else:
                raise HTTPException(status_code=409, detail="Company name conflict, please retry") from e

    result = await db.execute(select(User).where(User.id == payload.user_id))
    existing_user = result.scalar_one_or_none()
    if existing_user:
        raise HTTPException(status_code=409, detail="User already registered")

    user = User(
        id=payload.user_id,
        email=payload.email,
        name=payload.name,
        company_id=company.id,
        # NOTE: First user in a company is always assigned "owner" role.
        # This is intentional for self-serve onboarding. Consider an
        # invitation-based system for production multi-user scenarios.
        role="owner",
    )
    db.add(user)
    await db.flush()
    return {"user_id": user.id, "company_id": str(company.id), "role": user.role}


@router.get("/me")
async def get_me(current_user: User = Depends(get_current_user)):
    return {
        "user_id": current_user.id,
        "email": current_user.email,
        "name": current_user.name,
        "role": current_user.role,
        "company_id": str(current_user.company_id),
    }


class DeactivateUserPayload(BaseModel):
    user_id: str


@router.post("/deactivate", status_code=200)
async def deactivate_user(
    payload: DeactivateUserPayload,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.id != payload.user_id and current_user.role not in ["admin", "owner"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to deactivate this user",
        )

    result = await db.execute(select(User).where(User.id == payload.user_id))
    user_to_deactivate = result.scalar_one_or_none()

    if not user_to_deactivate:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if user_to_deactivate.company_id != current_user.company_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to deactivate user from another company",
        )

    user_to_deactivate.is_active = False
    user_to_deactivate.token_version += 1

    try:
        r = await _get_redis()
        if r:
            await r.setex(f"session_blacklist:{payload.user_id}:{user_to_deactivate.token_version}", 86400, "1")
    except Exception:
        pass

    await db.flush()

    logger.info(
        f"User {user_to_deactivate.id} deactivated by {current_user.id}. Token version incremented to {user_to_deactivate.token_version}"
    )

    return {"message": "User deactivated successfully", "user_id": user_to_deactivate.id}


@router.get("/export-my-data")
async def export_my_data(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export all data belonging to the authenticated user (GDPR/CCPA data portability).

    Returns a JSON object containing all user records: profile, jobs, quotes,
    AI outputs, invoices, and billing account info across their company tenant.
    """

    user_id = current_user.id
    company_id = current_user.company_id

    export_data = {
        "exported_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "user": {
            "id": current_user.id,
            "email": current_user.email,
            "name": current_user.name,
            "role": current_user.role,
            "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
        },
        "company": None,
        "jobs": [],
        "quotes": [],
        "ai_outputs": [],
        "billing": None,
    }

    # Company info
    company_result = await db.execute(select(Company).where(Company.id == company_id))
    company = company_result.scalar_one_or_none()
    if company:
        export_data["company"] = {
            "name": company.name,
            "subscription_plan": company.subscription_plan,
            "trade_type": company.trade_type,
            "created_at": company.created_at.isoformat() if company.created_at else None,
        }

    # Jobs
    jobs_result = await db.execute(
        select(Job)
        .where(
            Job.company_id == company_id,
            Job.technician_id == user_id,
        )
        .order_by(Job.created_at.desc())
        .limit(500)
    )
    for j in jobs_result.scalars().all():
        export_data["jobs"].append(
            {
                "id": str(j.id),
                "status": j.status,
                "description": j.description,
                "address": j.address,
                "scheduled_time": j.scheduled_time.isoformat() if j.scheduled_time else None,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
        )

    # Quotes
    quotes_result = await db.execute(
        select(Quote)
        .where(
            Quote.company_id == company_id,
        )
        .order_by(Quote.created_at.desc())
        .limit(500)
    )
    for q in quotes_result.scalars().all():
        export_data["quotes"].append(
            {
                "id": str(q.id),
                "job_id": str(q.job_id) if q.job_id else None,
                "status": q.status,
                "total_amount": float(q.total_amount) if q.total_amount else None,
                "created_at": q.created_at.isoformat() if q.created_at else None,
            }
        )

    # AI outputs
    ai_result = await db.execute(
        select(AIOutput)
        .where(
            AIOutput.company_id == company_id,
        )
        .order_by(AIOutput.created_at.desc())
        .limit(500)
    )
    for a in ai_result.scalars().all():
        export_data["ai_outputs"].append(
            {
                "id": str(a.id),
                "job_id": str(a.job_id),
                "output_type": a.output_type,
                "confidence_score": a.confidence_score,
                "model_used": a.model_used,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
        )

    # Billing account
    billing_result = await db.execute(select(BillingAccount).where(BillingAccount.company_id == company_id))
    billing = billing_result.scalar_one_or_none()
    if billing:
        invoices_result = await db.execute(
            select(Invoice)
            .where(
                Invoice.company_id == company_id,
            )
            .order_by(Invoice.created_at.desc())
            .limit(100)
        )
        export_data["billing"] = {
            "plan": billing.plan,
            "monthly_quota_acu": float(billing.monthly_quota_acu) if billing.monthly_quota_acu else 0,
            "acu_remaining": float(billing.acu_remaining) if billing.acu_remaining else 0,
            "acu_debt": float(billing.acu_debt) if hasattr(billing, "acu_debt") and billing.acu_debt else 0,
            "invoices": [],
        }
        for inv in invoices_result.scalars().all():
            export_data["billing"]["invoices"].append(
                {
                    "id": str(inv.id),
                    "stripe_invoice_id": inv.stripe_invoice_id if hasattr(inv, "stripe_invoice_id") else None,
                    "amount_due": float(inv.amount_due) if inv.amount_due else 0,
                    "status": inv.status if hasattr(inv, "status") else "unknown",
                    "created_at": inv.created_at.isoformat() if inv.created_at else None,
                }
            )

    logger.info("Data export completed for user %s (company=%s)", user_id, company_id)
    return export_data


@router.get("/export-tenant-data")
async def export_tenant_data(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export all data for the current tenant (owner/admin only).

    Returns a comprehensive JSON export of all company data including
    all users, jobs, customers, quotes, invoices, AI outputs, and billing records.
    """
    if current_user.role not in ("owner", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only owners and admins can export tenant data",
        )

    company_id = current_user.company_id

    export_data = {
        "exported_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "exported_by": current_user.id,
        "company": None,
        "users": [],
        "customers": [],
        "jobs": [],
        "quotes": [],
        "ai_outputs": [],
        "billing": None,
        "usage_ledger": [],
    }

    # Company
    company_result = await db.execute(select(Company).where(Company.id == company_id))
    company = company_result.scalar_one_or_none()
    if company:
        export_data["company"] = {
            "id": str(company.id),
            "name": company.name,
            "subscription_plan": company.subscription_plan,
            "trade_type": company.trade_type,
            "created_at": company.created_at.isoformat() if company.created_at else None,
        }

    # Users
    users_result = await db.execute(select(User).where(User.company_id == company_id).order_by(User.created_at.desc()))
    for u in users_result.scalars().all():
        export_data["users"].append(
            {
                "id": u.id,
                "name": u.name,
                "email": u.email,
                "role": u.role,
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
        )

    # Customers
    customers_result = await db.execute(
        select(Customer).where(Customer.company_id == company_id).order_by(Customer.name)
    )
    for c in customers_result.scalars().all():
        export_data["customers"].append(
            {
                "id": str(c.id),
                "name": c.name,
                "email": c.email,
                "phone": c.phone,
            }
        )

    # Jobs (all)
    jobs_result = await db.execute(
        select(Job).where(Job.company_id == company_id).order_by(Job.created_at.desc()).limit(1000)
    )
    for j in jobs_result.scalars().all():
        export_data["jobs"].append(
            {
                "id": str(j.id),
                "customer_id": str(j.customer_id) if j.customer_id else None,
                "technician_id": j.technician_id,
                "status": j.status,
                "description": j.description,
                "address": j.address,
                "ai_processing_state": j.ai_processing_state,
                "scheduled_time": j.scheduled_time.isoformat() if j.scheduled_time else None,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
        )

    # Quotes
    quotes_result = await db.execute(
        select(Quote).where(Quote.company_id == company_id).order_by(Quote.created_at.desc()).limit(1000)
    )
    for q in quotes_result.scalars().all():
        export_data["quotes"].append(
            {
                "id": str(q.id),
                "job_id": str(q.job_id) if q.job_id else None,
                "status": q.status,
                "total_amount": float(q.total_amount) if q.total_amount else None,
                "created_at": q.created_at.isoformat() if q.created_at else None,
            }
        )

    # AI outputs
    ai_result = await db.execute(
        select(AIOutput).where(AIOutput.company_id == company_id).order_by(AIOutput.created_at.desc()).limit(1000)
    )
    for a in ai_result.scalars().all():
        export_data["ai_outputs"].append(
            {
                "id": str(a.id),
                "job_id": str(a.job_id),
                "output_type": a.output_type,
                "confidence_score": a.confidence_score,
                "model_used": a.model_used,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
        )

    # Billing
    billing_result = await db.execute(select(BillingAccount).where(BillingAccount.company_id == company_id))
    billing = billing_result.scalar_one_or_none()
    if billing:
        invoices_result = await db.execute(
            select(Invoice).where(Invoice.company_id == company_id).order_by(Invoice.created_at.desc()).limit(500)
        )
        export_data["billing"] = {
            "plan": billing.plan,
            "monthly_quota_acu": float(billing.monthly_quota_acu) if billing.monthly_quota_acu else 0,
            "acu_remaining": float(billing.acu_remaining) if billing.acu_remaining else 0,
            "invoices": [],
        }
        for inv in invoices_result.scalars().all():
            export_data["billing"]["invoices"].append(
                {
                    "id": str(inv.id),
                    "amount_due": float(inv.amount_due) if inv.amount_due else 0,
                    "status": inv.status if hasattr(inv, "status") else "unknown",
                    "created_at": inv.created_at.isoformat() if inv.created_at else None,
                }
            )

    # Usage ledger
    usage_result = await db.execute(
        select(UsageLedger)
        .where(UsageLedger.company_id == company_id)
        .order_by(UsageLedger.created_at.desc())
        .limit(500)
    )
    for u_entry in usage_result.scalars().all():
        export_data["usage_ledger"].append(
            {
                "id": str(u_entry.id),
                "acu_amount": float(u_entry.acu_amount) if u_entry.acu_amount else 0,
                "operation": u_entry.operation if hasattr(u_entry, "operation") else None,
                "description": u_entry.description if hasattr(u_entry, "description") else None,
                "created_at": u_entry.created_at.isoformat() if u_entry.created_at else None,
            }
        )

    logger.info("Tenant data export completed for company %s by user %s", company_id, current_user.id)
    return export_data
