import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from celery import shared_task
from sqlalchemy import select

from app.billing.dead_letter import DeadLetterJob
from app.billing.state_machine import AIProcessingState
from app.database import AsyncSessionLocal
from app.jobs.models import Job
from app.sync_redis_pool import get_sync_redis

logger = logging.getLogger(__name__)

_MAX_DLQ_RETRIES = 5


async def _retry_dead_letter_job(dead_letter_id: str) -> dict[str, Any]:
    """Retry a dead-lettered job by re-queuing it for processing."""
    async with AsyncSessionLocal() as db:
        try:
            result = await db.execute(select(DeadLetterJob).where(DeadLetterJob.id == dead_letter_id))
            dlq_entry = result.scalar_one_or_none()

            if not dlq_entry:
                logger.error("Dead letter job not found: %s", dead_letter_id)
                return {"status": "error", "message": "Dead letter job not found"}

            if dlq_entry.retry_count >= _MAX_DLQ_RETRIES:
                logger.warning("Dead letter job %s has exceeded max retries (%d)", dead_letter_id, _MAX_DLQ_RETRIES)
                dlq_entry.last_state = "max_retries_exceeded"
                await db.commit()
                try:
                    from app.monitoring.prometheus import inc_dlq_retry

                    inc_dlq_retry("max_retries_exceeded")
                except Exception:
                    pass
                return {"status": "error", "message": "Max retries exceeded"}

            result = await db.execute(select(Job).where(Job.id == dlq_entry.job_id))
            job = result.scalar_one_or_none()

            if not job:
                logger.error("Original job not found for DLQ entry: %s", dead_letter_id)
                dlq_entry.last_state = "job_not_found"
                await db.commit()
                return {"status": "error", "message": "Original job not found"}

            # Only retry jobs that are in a failed or stuck state
            if job.ai_processing_state not in (AIProcessingState.failed.value, AIProcessingState.reserved.value):
                logger.warning(
                    "Job %s is in state %s, not retrying",
                    dlq_entry.job_id,
                    job.ai_processing_state,
                )
                dlq_entry.last_state = f"skipped_state_{job.ai_processing_state}"
                await db.commit()
                return {"status": "skipped", "message": f"Job in state {job.ai_processing_state}"}

            # Fetch job details for re-dispatch
            from sqlalchemy.orm import selectinload

            job_result = await db.execute(
                select(Job).where(Job.id == dlq_entry.job_id).options(selectinload(Job.media))
            )
            job = job_result.scalar_one_or_none()
            if not job:
                return {"status": "error", "message": "Job not found with media"}

            image_urls = [m.storage_url for m in job.media if m.type in ("image", "photo")]
            audio_url = next((m.storage_url for m in job.media if m.type == "audio"), None)

            from celery_app import enqueue_job_task

            dispatch_result = enqueue_job_task(
                job_id=str(job.id),
                company_id=str(job.company_id),
                user_id=str(dlq_entry.user_id) if dlq_entry.user_id else None,
                audio_url=audio_url,
                image_urls=image_urls,
                description=job.description or "",
                trace_id=dlq_entry.trace_id or str(uuid.uuid4()),
            )

            dlq_entry.retry_count += 1
            dlq_entry.last_state = "retried"
            dlq_entry.error_message = (
                f"Retry {dlq_entry.retry_count} at "
                f"{datetime.now(UTC).isoformat()}, "
                f"task_id={dispatch_result.get('task_id', 'unknown')}"
            )
            await db.commit()

            logger.info(
                "DLQ retry successful for job %s (attempt %d/%d, task_id=%s)",
                dlq_entry.job_id,
                dlq_entry.retry_count,
                _MAX_DLQ_RETRIES,
                dispatch_result.get("task_id"),
            )
            try:
                from app.monitoring.prometheus import inc_dlq_retry

                inc_dlq_retry("success")
            except Exception:
                pass

            return {
                "status": "success",
                "message": f"Retry {dlq_entry.retry_count} dispatched for job {dlq_entry.job_id}",
                "retry_count": dlq_entry.retry_count,
                "task_id": dispatch_result.get("task_id"),
            }

        except Exception as e:
            await db.rollback()
            logger.error("Failed to retry dead letter job %s: %s", dead_letter_id, e)
            try:
                from app.monitoring.prometheus import inc_dlq_retry

                inc_dlq_retry("error")
            except Exception:
                pass
            return {"status": "error", "message": str(e)}


@shared_task(bind=True, max_retries=3, default_retry_delay=60, queue="beat")
def retry_dead_letter_job(self, dead_letter_id: str):
    """Celery task to retry a dead-lettered job.

    H6-FIX: Uses _run_async (from celery_app) instead of asyncio.run() to
    prevent event loop corruption on retry.
    """
    try:
        from celery_app import _run_async

        result = _run_async(_retry_dead_letter_job(dead_letter_id))
        if result.get("status") == "error" and "Max retries exceeded" not in result.get("message", ""):
            raise self.retry(exc=Exception(result.get("message", "Unknown error")))
        return result
    except Exception as exc:
        logger.error("Dead letter job retry task failed: %s", exc)
        raise self.retry(exc=exc) from exc


# Shared lock key for all DLQ fallback operations
_DLQ_FALLBACK_LOCK = "dlq:fallback:lock"

_beat_lock_lock = threading.Lock()

# HIGH-1 FIX: Local TTL cache for beat lock state when Redis is unavailable
_LOCAL_BEAT_LOCK_CACHE: dict = {}
_LOCAL_BEAT_LOCK_CACHE_TTL = 120  # seconds


def _acquire_beat_lock(task_name: str, ttl: int = 300) -> bool:
    """Redis-based execution lock for beat tasks.

    HIGH-1 FIX: On Redis failure, uses local TTL-based cache to prevent
    all replicas from executing simultaneously. Falls back to permissive
    only if no recent lock state is known (e.g., fresh startup).
    """
    with _beat_lock_lock:
        try:
            r = get_sync_redis()
            if r is None:
                raise ConnectionError("Redis pool unavailable")
            locked = r.set(f"beat:lock:{task_name}", "1", nx=True, ex=ttl)
            if bool(locked):
                # Stash successful lock in local cache
                _LOCAL_BEAT_LOCK_CACHE[task_name] = time.monotonic()
            return bool(locked)
        except Exception:
            # HIGH-1 FIX: Check local TTL cache before allowing execution
            last_lock_time = _LOCAL_BEAT_LOCK_CACHE.get(task_name)
            if last_lock_time is not None:
                elapsed = time.monotonic() - last_lock_time
                if elapsed < _LOCAL_BEAT_LOCK_CACHE_TTL:
                    # Another replica likely still holds the lock — skip
                    logger.debug("Beat lock %s: using local cache (%.0fs ago), skipping", task_name, elapsed)
                    try:
                        from app.monitoring.prometheus import increment_counter

                        increment_counter(
                            "workticket_beat_lock_skipped_total", {"task": task_name, "reason": "local_cache"}
                        )
                    except Exception:
                        pass
                    return False

            logger.warning(
                "Redis unreachable for beat lock %s and no recent lock state — allowing execution to prevent maintenance stall",
                task_name,
            )
            try:
                from app.monitoring.prometheus import increment_counter

                increment_counter("workticket_beat_lock_skipped_total", {"task": task_name, "reason": "redis_down"})
            except Exception:
                pass
            return True


@shared_task(bind=True, max_retries=3, default_retry_delay=300, queue="beat")
def retry_expired_dead_letter_jobs():
    """Celery beat task to retry dead-lettered jobs with per-DLQ-entry backoff.

    H6-FIX: Uses _run_async (from celery_app) instead of asyncio.run() to
    prevent event loop corruption on retry. Uses _acquire_beat_lock to prevent
    cross-replica duplicate execution.
    """
    if not _acquire_beat_lock("retry_expired_dead_letter_jobs", ttl=300):
        logger.warning("retry_expired_dead_letter_jobs skipped — another execution is in progress")
        return {"status": "skipped", "reason": "concurrent_execution_locked"}

    from celery_app import _run_async

    async def _run():
        async with AsyncSessionLocal() as db:
            try:
                now = datetime.now(UTC)
                cutoff = now - timedelta(minutes=5)
                result = await db.execute(
                    select(DeadLetterJob)
                    .where(
                        DeadLetterJob.created_at < cutoff,
                        DeadLetterJob.retry_count < _MAX_DLQ_RETRIES,
                        DeadLetterJob.last_state != "retried",
                    )
                    .with_for_update(skip_locked=True)
                    .limit(20)
                )
                dlq_jobs = result.scalars().all()
                # MED-2 FIX: Exponential backoff for DLQ retries — each retry level
                # doubles the wait time (5min → 10min → 20min → 40min → 80min).
                # Prevents rapid retry storms when the downstream cause persists.
                dlq_jobs = [j for j in dlq_jobs if j.created_at < now - timedelta(minutes=5 * (2**j.retry_count))]
                if dlq_jobs:
                    logger.info("Found %d dead letter jobs to retry", len(dlq_jobs))
                    for dlq_job in dlq_jobs:
                        retry_dead_letter_job.delay(str(dlq_job.id))
                # Update DLQ count gauge
                try:
                    from app.monitoring.prometheus import set_dlq_entries

                    total_result = await db.execute(select(DeadLetterJob))
                    set_dlq_entries(len(total_result.scalars().all()))
                except Exception:
                    pass
            except Exception as e:
                logger.error("Failed to scan for dead letter jobs: %s", e)
                raise

    _run_async(_run())


def _acquire_dlq_fallback_lock(ttl=300):
    """Shared lock for all DLQ fallback operations."""
    return _acquire_beat_lock(_DLQ_FALLBACK_LOCK, ttl=ttl)


def _scan_dlq_fallback_files(fallback_dir: str) -> list:
    """Scan for PID-named DLQ fallback files and return their paths sorted by mtime."""
    import glob as _glob
    import os as _os

    pattern = _os.path.join(fallback_dir, "workticket_dlq_fallback.*.*.jsonl")
    files = _glob.glob(pattern)
    return sorted(files, key=lambda f: _os.path.getmtime(f))


def _merge_dlq_fallback_files(fallback_dir: str) -> dict:
    """Merge all PID-named DLQ fallback files into a single main file, deduplicating by job_id+task_name."""
    import json as _json
    import os as _os

    main_path = _os.path.join(fallback_dir, "workticket_dlq_fallback.merged.jsonl")
    seen_entries = set()
    total_read = 0
    total_written = 0

    pid_files = _scan_dlq_fallback_files(fallback_dir)
    if not pid_files:
        return {"merged": 0, "files_processed": 0, "main_file": main_path}

    for pid_file in pid_files:
        try:
            with open(pid_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = _json.loads(line)
                        dedup_key = (
                            entry.get("job_id", ""),
                            entry.get("task_name", ""),
                            entry.get("failure_category", ""),
                        )
                        if dedup_key not in seen_entries:
                            seen_entries.add(dedup_key)
                            with open(main_path, "a") as main_f:
                                main_f.write(_json.dumps(entry) + "\n")
                            total_written += 1
                        total_read += 1
                    except _json.JSONDecodeError:
                        pass
            _os.remove(pid_file)
        except Exception as e:
            logger.warning("Failed to process DLQ fallback file %s: %s", pid_file, e)

    return {
        "merged": total_written,
        "files_processed": len(pid_files),
        "total_read": total_read,
        "main_file": main_path,
    }


@shared_task(bind=True, max_retries=3, default_retry_delay=60, queue="beat")
def merge_dlq_fallback_files(self):
    """Collector beat task: merge PID-named DLQ fallback files into main file.

    H6-FIX: Uses Redis distributed lock to prevent cross-replica duplicate execution.
    """
    if not _acquire_dlq_fallback_lock(ttl=120):
        logger.warning("merge_dlq_fallback_files skipped — another execution is in progress")
        return {"status": "skipped", "reason": "concurrent_execution_locked"}

    import os as _os

    _dlq_fallback_dir = _os.getenv("DLQ_FALLBACK_DIR", "/tmp/workticket/dlq_fallback")
    if not _os.path.exists(_dlq_fallback_dir):
        return {"status": "no_fallback_dir"}

    result = _merge_dlq_fallback_files(_dlq_fallback_dir)
    return {"status": "completed", **result}


@shared_task(bind=True, max_retries=3, default_retry_delay=60, queue="beat")
def replay_dlq_fallback(self):
    """Replay dead letter entries from merged DLQ fallback file into the DLQ database table.

    Runs every 5 minutes to pick up any entries that were written to the
    JSONL fallback file (when the DLQ database write failed) and migrate them
    into the main dead letter queue for standard retry processing.

    H6-FIX: Uses Redis distributed lock to prevent cross-replica duplicate execution.
    """
    if not _acquire_dlq_fallback_lock(ttl=300):
        logger.warning("replay_dlq_fallback skipped — another execution is in progress")
        return {"status": "skipped", "reason": "concurrent_execution_locked"}

    import json as _json
    import os as _os

    _dlq_fallback_dir = _os.getenv("DLQ_FALLBACK_DIR", "/tmp/workticket/dlq_fallback")
    _dlq_fallback_path = _os.path.join(_dlq_fallback_dir, "workticket_dlq_fallback.merged.jsonl")

    if not _os.path.exists(_dlq_fallback_path):
        return {"status": "no_fallback_file"}

    try:
        from app.monitoring.prometheus import set_dlq_fallback_file_size

        _file_size = _os.path.getsize(_dlq_fallback_path)
        set_dlq_fallback_file_size(_file_size)
    except Exception:
        pass

    replayed = 0
    errors = 0
    remaining_lines = []

    async def _run():
        nonlocal replayed, errors, remaining_lines
        async with AsyncSessionLocal() as db:
            try:
                with open(_dlq_fallback_path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = _json.loads(line)
                        except _json.JSONDecodeError:
                            # Partial line from concurrent merge — keep as pending
                            remaining_lines.append(line)
                            continue

                        # Check if already in DLQ database
                        from app.billing.dead_letter import DeadLetterJob

                        existing = await db.execute(
                            select(DeadLetterJob)
                            .where(
                                DeadLetterJob.job_id == entry.get("job_id", ""),
                            )
                            .limit(1)
                        )
                        if existing.scalar_one_or_none():
                            continue  # Already in DLQ, skip

                        dlq_entry = DeadLetterJob(
                            job_id=entry.get("job_id", ""),
                            company_id=entry.get("company_id", ""),
                            user_id=entry.get("user_id"),
                            task_name=entry.get("task_name", ""),
                            error_message=entry.get("error_message", "")[:500],
                            failure_category=entry.get("failure_category", ""),
                            last_state=entry.get("last_state", "failed"),
                            retry_count=entry.get("retry_count", 0),
                            trace_id=entry.get("trace_id"),
                        )
                        db.add(dlq_entry)
                        replayed += 1

                if replayed:
                    await db.commit()
                    logger.info("Replayed %d entries from DLQ fallback file", replayed)
                else:
                    remaining_lines = _get_non_empty_lines(_dlq_fallback_path)

            except Exception as e:
                logger.error("DLQ fallback replay failed: %s", e)
                raise

    def _get_non_empty_lines(path: str) -> list:
        try:
            with open(path) as f:
                return [line for line in f.read().splitlines() if line.strip()]
        except Exception:
            return []

    try:
        from celery_app import _run_async

        _run_async(_run())
    except Exception as e:
        logger.error("DLQ fallback replay task failed: %s", e)
        raise self.retry(exc=e) from e

    # If all entries replayed successfully, remove the fallback file
    if replayed > 0 and errors == 0 and not remaining_lines:
        try:
            _os.remove(_dlq_fallback_path)
            logger.info("DLQ fallback file removed after successful replay")
        except Exception as e:
            logger.warning("Failed to remove DLQ fallback file: %s", e)

    return {
        "status": "completed",
        "replayed": replayed,
        "errors": errors,
        "remaining": len(remaining_lines),
    }


def register_tasks(celery_app):
    """Register dead letter queue tasks with the Celery app.

    All tasks in this module use @shared_task decorator and are
    auto-registered when the module is imported by celery_app.py.
    This function exists as an explicit registration hook for
    programmatic task loading.
    """
    import app.billing.tasks as _tasks

    _ = _tasks  # ensure the module is loaded (triggers @shared_task decorators)
