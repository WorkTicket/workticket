import json
import logging
import os

from celery_config.broker import (
    MAX_SUPPORTED_VERSION,
    MIN_SUPPORTED_VERSION,
    _deterministic_lock_id,
    _get_signing_key,
    _move_to_dead_letter,
    _verify_task_payload,
    get_sync_redis,
)
from celery_config.worker import _run_async, celery_app

logger = logging.getLogger(__name__)


def _is_ai_disabled() -> bool:
    from app.config import FeatureFlags, get_settings

    settings = get_settings()
    if settings.ai_disabled:
        return True
    flags = FeatureFlags()
    return flags.is_enabled(FeatureFlags.AI_DISABLED)


@celery_app.task(bind=True, max_retries=3, queue="ai_text", acks_late=True, reject_on_worker_lost=True)
def process_ai_text(
    self,
    prompt: str,
    company_id: str | None = None,
    user_id: str | None = None,
    model: str = "llama3.1",
    _hmac: str = "",
    payload_version: int = 1,
    **kwargs,
):
    if _is_ai_disabled():
        logger.debug("AI disabled — skipping process_ai_text task")
        return {"status": "skipped", "reason": "ai_disabled"}

    from app.ai.failure_classifier import classify_failure
    from app.ai.gateway import _sanitize_output_text, gateway
    from app.exceptions import ValidationError
    from app.tasks.retry_guard import check_retry_storm

    # DEP-2: Payload version validation
    if payload_version < MIN_SUPPORTED_VERSION or payload_version > MAX_SUPPORTED_VERSION:
        logger.error("Unsupported payload version %d for process_ai_text (company=%s)", payload_version, company_id)
        return {"status": "failed", "error": "unsupported_payload_version", "failure_type": "security"}

    # HMAC verification (HIGH-3) — fail-closed when key missing
    _signing_key = _get_signing_key()
    _is_debug = os.getenv("DEBUG", "").lower() in ("true", "1", "yes")
    if not _signing_key and not _is_debug:
        logger.error("CELERY_TASK_SIGNING_KEY not configured and DEBUG=false — refusing process_ai_text")
        return {"status": "failed", "error": "signing_key_not_configured", "failure_type": "security"}
    if _signing_key:
        _payload = {
            "prompt": prompt,
            "company_id": company_id,
            "user_id": user_id,
            "model": model,
            "payload_version": payload_version,
        }
        if not _verify_task_payload(_payload, _hmac):
            logger.error("HMAC verification FAILED for process_ai_text (company=%s) — sending to DLQ", company_id)
            _move_to_dead_letter(
                job_id="",
                company_id=company_id or "",
                user_id=user_id,
                task_name="process_ai_text",
                error_message="HMAC verification failed",
                failure_category="security",
                last_state="queued",
                retry_count=self.request.retries,
            )
            raise self.reject(requeue=False)

    if self.request.retries > 0 and not check_retry_storm(self.request.id or "unknown", "process_ai_text"):
        logger.error("Retry storm blocked for AI text task (company=%s)", company_id)
        return {"status": "failed", "error": "retry_storm_blocked", "failure_type": "retry_storm"}

    async def _run():
        from app.database import AsyncSessionLocal

        # V2-FIX: Verify company_id has valid access
        if company_id:
            try:
                async with AsyncSessionLocal() as _verify_db:
                    from sqlalchemy import select

                    from app.jobs.models import Company

                    _comp = await _verify_db.execute(select(Company).where(Company.id == company_id).limit(1))
                    if not _comp.scalar_one_or_none():
                        logger.error("Unauthorized text processing attempt: company=%s", company_id)
                        return {"status": "failed", "error": "unauthorized"}
            except Exception as _e:
                logger.debug("AI text company verification failed: %s", _e)

        _lock_key = f"task:lock:ai_text:{company_id}:{_deterministic_lock_id('ai_text', company_id, prompt[:100])}"
        _redis_lock = None
        try:
            _redis_lock = get_sync_redis()
            if _redis_lock is None:
                raise ConnectionError("Redis pool unavailable")
            if not _redis_lock.set(_lock_key, "1", nx=True, ex=300):
                logger.info("Another worker processing same text prompt, skipping duplicate for company=%s", company_id)
                return {"status": "skipped", "reason": "concurrent_processing"}
        except Exception as _e:
            logger.error("Redis unavailable for process_ai_text lock: %s", _e)
            raise

        try:
            async with AsyncSessionLocal() as _check_db:
                import hashlib

                from sqlalchemy import select

                from app.jobs.models import AIOutput

                _prompt_hash = hashlib.sha256(prompt.encode("utf-8") if prompt else b"").hexdigest()[:32]
                existing = await _check_db.execute(
                    select(AIOutput)
                    .where(
                        AIOutput.company_id == company_id,
                        AIOutput.output_type == "text_generation",
                        AIOutput.source_hash == _prompt_hash,
                    )
                    .limit(1)
                )
                if existing.scalar_one_or_none():
                    logger.info(
                        "AI text output already exists for company=%s prompt hash=%s, skipping",
                        company_id,
                        _prompt_hash,
                    )
                    return existing.scalar_one_or_none()

            raw = await gateway.orchestrator.generate_structured_output(
                transcript="",
                vision_analysis="",
                job_metadata={
                    "description": prompt,
                    "trade_type": kwargs.get("trade_type", ""),
                },
            )
            raw.summary = _sanitize_output_text(raw.summary)
            raw.recommended_fix = _sanitize_output_text(raw.recommended_fix)
            raw.problem_type = _sanitize_output_text(raw.problem_type)
            raw.materials = [_sanitize_output_text(str(m)) for m in raw.materials]
            return raw
        finally:
            if _redis_lock:
                try:
                    _redis_lock.delete(_lock_key)
                except Exception as _e:
                    logger.debug("Failed to delete AI text Redis lock: %s", _e)

    try:
        result = _run_async(_run())
        if hasattr(result, "model_dump"):
            return result.model_dump()
        return result
    except Exception as exc:
        error_str = str(exc)
        failure_cat = classify_failure(error_str)
        is_non_recoverable = isinstance(exc, (ValidationError, json.JSONDecodeError)) or "NON_RECOVERABLE" in error_str
        logger.error("AI text task failed for company=%s: %s (category=%s)", company_id, exc, failure_cat.value)
        if is_non_recoverable or self.request.retries >= (self.max_retries or 3):
            _move_to_dead_letter(
                job_id="",
                company_id=company_id or "",
                user_id=user_id,
                task_name="process_ai_text",
                error_message=error_str,
                failure_category=failure_cat.value,
                last_state="failed",
                retry_count=self.request.retries,
            )
            return {"status": "failed", "error": error_str, "failure_type": failure_cat.value}
        raise self.retry(exc=exc) from exc


@celery_app.task(bind=True, max_retries=3, queue="ai_audio", acks_late=True, reject_on_worker_lost=True)
def process_ai_audio(self, audio_url: str, company_id: str | None = None, user_id: str | None = None, _hmac: str = ""):
    if _is_ai_disabled():
        logger.debug("AI disabled — skipping process_ai_audio task")
        return {"status": "skipped", "reason": "ai_disabled"}

    from app.ai.failure_classifier import classify_failure
    from app.ai.gateway import _sanitize_output_text, gateway
    from app.exceptions import ValidationError
    from app.tasks.retry_guard import check_retry_storm

    # HMAC verification (HIGH-3) — fail-closed when key missing
    _signing_key = _get_signing_key()
    _is_debug = os.getenv("DEBUG", "").lower() in ("true", "1", "yes")
    if not _signing_key and not _is_debug:
        logger.error("CELERY_TASK_SIGNING_KEY not configured and DEBUG=false — refusing process_ai_audio")
        return {"status": "failed", "error": "signing_key_not_configured", "failure_type": "security"}
    if _signing_key:
        _payload = {"audio_url": audio_url, "company_id": company_id, "user_id": user_id}
        if not _verify_task_payload(_payload, _hmac):
            logger.error("HMAC verification FAILED for process_ai_audio (company=%s) — sending to DLQ", company_id)
            _move_to_dead_letter(
                job_id="",
                company_id=company_id or "",
                user_id=user_id,
                task_name="process_ai_audio",
                error_message="HMAC verification failed",
                failure_category="security",
                last_state="queued",
                retry_count=self.request.retries,
            )
            raise self.reject(requeue=False)

    if self.request.retries > 0 and not check_retry_storm(self.request.id or "unknown", "process_ai_audio"):
        logger.error("Retry storm blocked for AI audio task (company=%s)", company_id)
        return {"status": "failed", "error": "retry_storm_blocked", "failure_type": "retry_storm"}

    async def _run():
        from sqlalchemy import select

        from app.database import AsyncSessionLocal
        from app.jobs.models import AIOutput

        # V2-FIX: Verify company_id has valid access
        if company_id and audio_url:
            try:
                async with AsyncSessionLocal() as _verify_db:
                    from app.jobs.models import JobMedia

                    _media = await _verify_db.execute(
                        select(JobMedia)
                        .where(
                            JobMedia.storage_url == audio_url,
                            JobMedia.company_id == company_id,
                        )
                        .limit(1)
                    )
                    if not _media.scalar_one_or_none():
                        logger.error("Unauthorized audio access attempt: company=%s url=%s", company_id, audio_url)
                        return {"status": "failed", "error": "unauthorized"}
            except Exception as _e:
                logger.debug("AI audio company verification failed: %s", _e)

        _lock_key = f"task:lock:ai_audio:{company_id}:{audio_url}"
        _redis_lock = None
        try:
            _redis_lock = get_sync_redis()
            if _redis_lock is None:
                raise ConnectionError("Redis pool unavailable")
            if not _redis_lock.set(_lock_key, "1", nx=True, ex=300):
                logger.info("Another worker processing same audio, skipping duplicate for company=%s", company_id)
                return {"status": "skipped", "reason": "concurrent_processing"}
        except Exception as _e:
            logger.error("Redis unavailable for process_ai_audio lock: %s", _e)
            raise

        try:
            async with AsyncSessionLocal() as _check_db:
                from sqlalchemy import select

                if audio_url:
                    existing = await _check_db.execute(
                        select(AIOutput)
                        .where(
                            AIOutput.company_id == company_id,
                            AIOutput.output_type == "audio_transcript",
                            AIOutput.source_url == audio_url,
                        )
                        .limit(1)
                    )
                else:
                    existing = await _check_db.execute(
                        select(AIOutput)
                        .where(
                            AIOutput.company_id == company_id,
                            AIOutput.output_type == "audio_transcript",
                        )
                        .limit(1)
                    )
                if existing.scalar_one_or_none():
                    logger.info(
                        "Audio transcript already exists for company=%s audio=%s, skipping", company_id, audio_url
                    )
                    return {"status": "skipped", "reason": "already_processed"}

            transcript = await gateway.orchestrator.transcribe_audio(audio_url)
            return {"transcript": _sanitize_output_text(transcript)}
        finally:
            if _redis_lock:
                try:
                    _redis_lock.delete(_lock_key)
                except Exception as _e:
                    logger.debug("Failed to delete AI audio Redis lock: %s", _e)

    try:
        result = _run_async(_run())
        return result
    except Exception as exc:
        error_str = str(exc)
        failure_cat = classify_failure(error_str)
        is_non_recoverable = isinstance(exc, (ValidationError, json.JSONDecodeError)) or "NON_RECOVERABLE" in error_str
        logger.error("AI audio task failed for company=%s: %s (category=%s)", company_id, exc, failure_cat.value)
        if is_non_recoverable or self.request.retries >= (self.max_retries or 3):
            _move_to_dead_letter(
                job_id="",
                company_id=company_id or "",
                user_id=user_id,
                task_name="process_ai_audio",
                error_message=error_str,
                failure_category=failure_cat.value,
                last_state="failed",
                retry_count=self.request.retries,
            )
            return {"status": "failed", "error": error_str, "failure_type": failure_cat.value}
        raise self.retry(exc=exc) from exc


@celery_app.task(bind=True, max_retries=3, queue="ai_image", acks_late=True, reject_on_worker_lost=True)
def process_ai_image(
    self, image_urls: list, company_id: str | None = None, user_id: str | None = None, prompt: str = "", _hmac: str = ""
):
    if _is_ai_disabled():
        logger.debug("AI disabled — skipping process_ai_image task")
        return {"status": "skipped", "reason": "ai_disabled"}

    from app.ai.failure_classifier import classify_failure
    from app.ai.gateway import _sanitize_output_text, gateway
    from app.exceptions import ValidationError
    from app.tasks.retry_guard import check_retry_storm

    # HMAC verification (HIGH-3) — fail-closed when key missing
    _signing_key = _get_signing_key()
    _is_debug = os.getenv("DEBUG", "").lower() in ("true", "1", "yes")
    if not _signing_key and not _is_debug:
        logger.error("CELERY_TASK_SIGNING_KEY not configured and DEBUG=false — refusing process_ai_image")
        return {"status": "failed", "error": "signing_key_not_configured", "failure_type": "security"}
    if _signing_key:
        _payload = {"image_urls": image_urls, "company_id": company_id, "user_id": user_id, "prompt": prompt}
        if not _verify_task_payload(_payload, _hmac):
            logger.error("HMAC verification FAILED for process_ai_image (company=%s) — sending to DLQ", company_id)
            _move_to_dead_letter(
                job_id="",
                company_id=company_id or "",
                user_id=user_id,
                task_name="process_ai_image",
                error_message="HMAC verification failed",
                failure_category="security",
                last_state="queued",
                retry_count=self.request.retries,
            )
            raise self.reject(requeue=False)

    if self.request.retries > 0 and not check_retry_storm(self.request.id or "unknown", "process_ai_image"):
        logger.error("Retry storm blocked for AI image task (company=%s)", company_id)
        return {"status": "failed", "error": "retry_storm_blocked", "failure_type": "retry_storm"}

    async def _run():
        from sqlalchemy import select

        from app.database import AsyncSessionLocal

        # V2-FIX: Verify company_id has valid access
        if company_id and image_urls:
            try:
                async with AsyncSessionLocal() as _verify_db:
                    from app.jobs.models import JobMedia

                    _media = await _verify_db.execute(
                        select(JobMedia)
                        .where(
                            JobMedia.storage_url.in_(image_urls),
                            JobMedia.company_id == company_id,
                        )
                        .limit(1)
                    )
                    if not _media.scalar_one_or_none():
                        logger.error("Unauthorized image access attempt: company=%s urls=%s", company_id, image_urls)
                        return {"status": "failed", "error": "unauthorized"}
            except Exception as _e:
                logger.debug("AI image company verification failed: %s", _e)

        _urls_str = ",".join(sorted(image_urls)) if image_urls else ""
        _lock_key = f"task:lock:ai_image:{company_id}:{_deterministic_lock_id('ai_image', company_id, _urls_str)}"
        _redis_lock = None
        try:
            _redis_lock = get_sync_redis()
            if _redis_lock is None:
                raise ConnectionError("Redis pool unavailable")
            if not _redis_lock.set(_lock_key, "1", nx=True, ex=300):
                logger.info("Another worker processing same images, skipping duplicate for company=%s", company_id)
                return {"status": "skipped", "reason": "concurrent_processing"}
        except Exception as _e:
            logger.error("Redis unavailable for process_ai_image lock: %s", _e)
            raise

        try:
            async with AsyncSessionLocal() as _check_db:
                import hashlib

                from sqlalchemy import select

                from app.jobs.models import AIOutput

                _urls_str = ",".join(sorted(image_urls)) if image_urls else ""
                _urls_hash = hashlib.sha256(_urls_str.encode("utf-8")).hexdigest()[:32]
                existing = await _check_db.execute(
                    select(AIOutput)
                    .where(
                        AIOutput.company_id == company_id,
                        AIOutput.output_type == "image_analysis",
                        AIOutput.source_hash == _urls_hash,
                    )
                    .limit(1)
                )
                if existing.scalar_one_or_none():
                    logger.info(
                        "AI image output already exists for company=%s urls hash=%s, skipping", company_id, _urls_hash
                    )
                    return {"analysis": existing.scalar_one_or_none().json_result}

            analysis = await gateway.orchestrator.analyze_images(image_urls)
            return _sanitize_output_text(analysis)
        finally:
            if _redis_lock:
                try:
                    _redis_lock.delete(_lock_key)
                except Exception as _e:
                    logger.debug("Failed to delete AI image Redis lock: %s", _e)

    try:
        result = _run_async(_run())
        return {"analysis": result}
    except Exception as exc:
        error_str = str(exc)
        failure_cat = classify_failure(error_str)
        is_non_recoverable = isinstance(exc, (ValidationError, json.JSONDecodeError)) or "NON_RECOVERABLE" in error_str
        logger.error("AI image task failed for company=%s: %s (category=%s)", company_id, exc, failure_cat.value)
        if is_non_recoverable or self.request.retries >= (self.max_retries or 3):
            _move_to_dead_letter(
                job_id="",
                company_id=company_id or "",
                user_id=user_id,
                task_name="process_ai_image",
                error_message=error_str,
                failure_category=failure_cat.value,
                last_state="failed",
                retry_count=self.request.retries,
            )
            return {"status": "failed", "error": error_str, "failure_type": failure_cat.value}
        raise self.retry(exc=exc) from exc
