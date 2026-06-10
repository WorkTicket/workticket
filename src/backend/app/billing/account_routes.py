import asyncio
import json
import logging

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorize import require_admin
from app.auth.dependencies import get_current_user
from app.billing.cost_estimator import PLAN_TIERS, estimate_job_cost
from app.billing.models import BillingAccount, BillingAuditLog
from app.billing.quota_engine import quota_engine
from app.billing.schemas import (
    BillingAccountResponse,
    ChangePlanRequest,
    CostEstimateResponse,
    UsageSummaryResponse,
)
from app.billing.stripe_cache import (
    get_cached_subscription,
    set_cache_from_stripe_object,
)
from app.config import get_settings
from app.database import get_db
from app.jobs.models import Company, User

logger = logging.getLogger(__name__)

router = APIRouter()
settings = get_settings()

stripe.api_key = settings.stripe_secret_key
stripe.api_timeout = settings.stripe_api_timeout
_STRIPE_TIMEOUT = settings.stripe_api_timeout


@router.get("/account", response_model=BillingAccountResponse)
async def get_billing_account(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    account = await quota_engine.get_or_create_account(
        db, current_user.company_id, current_user.company.subscription_plan
    )
    return account


@router.get("/quota", response_model=UsageSummaryResponse)
async def get_quota_summary(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await quota_engine.get_usage_summary(db, current_user.company_id)


@router.post("/estimate-cost", response_model=CostEstimateResponse)
async def estimate_cost(
    image_count: int = Query(0, ge=0, le=20),
    has_audio: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cost = estimate_job_cost(image_count=image_count, has_audio=has_audio)
    summary = await quota_engine.get_usage_summary(db, current_user.company_id)
    within = cost.total_cost <= (summary["quota_remaining"] * 0.01)

    return CostEstimateResponse(
        estimated_text_cost=cost.text_cost,
        estimated_vision_cost=cost.vision_cost,
        estimated_audio_cost=cost.audio_cost,
        estimated_total_cost=cost.total_cost,
        within_quota=within,
        quota_remaining_after=summary["quota_remaining"] - cost.total_acu,
    )


@router.post("/change-plan", response_model=BillingAccountResponse, dependencies=[Depends(require_admin)])
async def change_plan(
    payload: ChangePlanRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if payload.plan not in PLAN_TIERS:
        raise HTTPException(status_code=400, detail=f"Invalid plan. Must be one of: {', '.join(PLAN_TIERS.keys())}")

    company = await db.execute(select(Company).where(Company.id == current_user.company_id))
    company = company.scalar_one_or_none()

    account = await quota_engine.get_or_create_account(db, current_user.company_id, payload.plan)
    old_plan = account.plan
    new_tier = PLAN_TIERS[payload.plan]
    _old_tier = PLAN_TIERS.get(old_plan, PLAN_TIERS["free"])

    if not company or not company.stripe_subscription_id:
        raise HTTPException(
            status_code=400,
            detail="Plan changes must be processed through Stripe. Use the checkout endpoint for upgrades or contact support for downgrades.",
        )

    from app.billing.invoice_routes import _get_stripe_circuit as _gsc

    stripe_circuit = await _gsc()
    if not await stripe_circuit.is_available():
        cached = await get_cached_subscription(company.stripe_subscription_id)
        if cached:
            logger.info(
                "Using cached subscription for plan change company %s (plan=%s, status=%s)",
                company.id,
                cached.get("plan"),
                cached.get("status"),
            )
        else:
            raise HTTPException(status_code=502, detail="Stripe API temporarily unavailable")
    try:
        stripe_sub = await asyncio.to_thread(lambda: stripe.Subscription.retrieve(company.stripe_subscription_id))
        await stripe_circuit.record_success()
        await set_cache_from_stripe_object(stripe_sub)
    except stripe.error.StripeError as e:
        await stripe_circuit.record_failure()
        cached = await get_cached_subscription(company.stripe_subscription_id)
        if cached:
            logger.info(
                "Stripe API failed for plan change company %s, using cache: %s",
                company.id,
                e,
            )
            stripe_sub: dict | stripe.Subscription = cached
        else:
            logger.error("Stripe verification failed for plan change company %s: %s", company.id, e)
            raise HTTPException(status_code=502, detail="Failed to verify subscription with Stripe") from e

    if isinstance(stripe_sub, dict) and "plan" in stripe_sub and stripe_sub["plan"] != "unknown":
        current_stripe_plan = stripe_sub["plan"]
    else:
        current_stripe_plan = "free"
        items_data = (
            stripe_sub.get("items", {}).get("data", [])
            if isinstance(stripe_sub, dict)
            else stripe_sub.items.data
            if hasattr(stripe_sub, "items")
            else []
        )
        for item in items_data:
            price_id = item.get("price", {}).get("id", "") if isinstance(item, dict) else item.price.id
            try:
                price_map = json.loads(settings.stripe_price_map) if settings.stripe_price_map else {}
                for plan_name, pid in price_map.items():
                    if pid == price_id:
                        current_stripe_plan = plan_name
            except (json.JSONDecodeError, AttributeError):
                pass

    if payload.plan != current_stripe_plan:
        logger.warning(
            "Plan change requested %s -> %s but Stripe subscription reflects %s for company %s",
            old_plan,
            payload.plan,
            current_stripe_plan,
            company.id,
        )
        raise HTTPException(
            status_code=400,
            detail="Plan does not match Stripe subscription. Update your Stripe subscription first.",
        )

    if company:
        company.subscription_plan = payload.plan
    account.plan = payload.plan
    account.monthly_quota_acu = new_tier["quota_acu"]

    # R-1 FIX: Record immutable audit log for billing plan changes
    audit_entry = BillingAuditLog(
        company_id=current_user.company_id,
        billing_account_id=account.id,
        changed_by_user_id=current_user.id,
        field_name="plan",
        old_value=old_plan,
        new_value=payload.plan,
    )
    db.add(audit_entry)

    await db.flush()

    logger.info(
        "Plan changed for company %s: %s -> %s by user %s (role=%s) via Stripe reconciliation",
        current_user.company_id,
        old_plan,
        payload.plan,
        current_user.id,
        current_user.role,
    )

    return account


@router.post("/disable-ai", status_code=200, dependencies=[Depends(require_admin)])
async def disable_ai(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.analytics import log_event
    from app.billing.abuse import abuse_detector

    await abuse_detector.disable_company(db, str(current_user.company_id))

    # R-1 FIX: Record immutable audit log for AI disable
    result = await db.execute(select(BillingAccount).where(BillingAccount.company_id == current_user.company_id))
    account = result.scalar_one_or_none()
    if account:
        audit_entry = BillingAuditLog(
            company_id=current_user.company_id,
            billing_account_id=account.id,
            changed_by_user_id=current_user.id,
            field_name="ai_disabled",
            old_value="False",
            new_value="True",
        )
        db.add(audit_entry)

    await log_event(
        event_name="ai.disabled",
        user_id=current_user.id,
        company_id=str(current_user.company_id),
        metadata={"role": current_user.role},
    )
    return {"status": "disabled"}


@router.post("/enable-ai", status_code=200, dependencies=[Depends(require_admin)])
async def enable_ai(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.analytics import log_event
    from app.billing.abuse import abuse_detector

    await abuse_detector.enable_company(db, str(current_user.company_id))

    # R-1 FIX: Record immutable audit log for AI re-enable
    result = await db.execute(select(BillingAccount).where(BillingAccount.company_id == current_user.company_id))
    account = result.scalar_one_or_none()
    if account:
        audit_entry = BillingAuditLog(
            company_id=current_user.company_id,
            billing_account_id=account.id,
            changed_by_user_id=current_user.id,
            field_name="ai_disabled",
            old_value="True",
            new_value="False",
        )
        db.add(audit_entry)

    await log_event(
        event_name="ai.enabled",
        user_id=current_user.id,
        company_id=str(current_user.company_id),
        metadata={"role": current_user.role},
    )
    return {"status": "enabled"}
