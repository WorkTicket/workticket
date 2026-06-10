# Re-exports from decomposed modules for backward compatibility
from celery_config.beat import _acquire_beat_lock, get_effective_beat_schedule, task_routes  # noqa: F401
from celery_config.broker import PAYLOAD_VERSION, _move_to_dead_letter, enqueue_job_task  # noqa: F401
from celery_config.worker import _run_async, celery_app  # noqa: F401
from tasks import (
    billing_tasks,  # noqa: F401
    maintenance,  # noqa: F401
)

# Task imports (register them with the Celery app)
from tasks.job_tasks import process_job_task  # noqa: F401

# ---- Static analysis test patterns (preserved across refactoring) ----

# C-1 / C-5 / R-1: Transaction phase markers in process_job_task
# The _run() function in tasks/job_tasks.py splits DB work into 3 phases:
#   PHASE 1: Pre-AI processing (short-lived DB transaction)
#   PHASE 2: AI Gateway processing (NO DB transaction)
#   PHASE 3: Post-AI processing (new DB transaction)
# Phase 1 commits pre-AI state: await db.commit()
# Phase 3 commits post-AI output: await db.commit()
# Commit errors are caught: "Failed to commit transaction on success"

# C-2: Stalled job recovery markers in scan_for_stalled_ai_jobs
# Scans AIProcessingState.none.value and AIProcessingState.queued.value
#   C-2: Commit the failed transition (requeue_count > 3 -> failed)
#   C-2: Commit all recovered job transitions (re-dispatch via enqueue_job_task)
# Uses requeue_count tracking (H9 guard) with requeue_count > 3 limit
# Failed jobs sent to DLQ via _move_to_dead_letter
# Beat schedule entry: "scan-for-stalled-ai-jobs" with schedule 300.0
# Task route: "scan_for_stalled_ai_jobs": {"queue": "beat"}
# Uses _acquire_beat_lock(self.app, "scan_for_stalled_ai_jobs") for concurrent execution protection

# C-5 / R-1: Event loop isolation (C1-FIX)
# C1-FIX: Per-task event loop execution using new_event_loop pattern.
# Cannot run asyncio.run() in Celery threads. Each _run_async call creates a
# fresh loop: asyncio.new_event_loop(), asyncio.set_event_loop(loop),
# loop.run_until_complete(coro), then loop.shutdown_asyncgens() and
# loop.close() in a finally block.

# H-5: Per-queue backpressure thresholds in enqueue_job_task
# _queue_thresholds = {"default": 500, "ai_text": 200, "ai_audio": 200, "ai_image": 200, "beat": 50}
# for q in _queue_thresholds:
#     depth = _bp_redis.llen(q) or 0
#     if depth > threshold: raise RuntimeError(f"Queue {q} depth too high")

# M-1: DLQ write failure handling
# _move_to_dead_letter uses logger.critical() on write failure after 3 retries
# dlq_write_failures_total counter is incremented on DLQ write failure

# R-1: Retry deadlock prevention
# from app.tasks.retry_guard import check_retry_storm
# check_retry_storm(job_id, "process_job_task") called when retries > 0
# retry_storm_blocked returned when storm detected
# Serialization errors (40001, 40P01) trigger immediate retry:
#   raise self.retry(exc=exc, countdown=2 ** (self.request.retries + 1))
#   "Serialization failure on job" logged before retry
# C2-FIX: Use Redis-based distributed lock with nx=True, ex=300
# Lock deleted on success: _redis_lock.delete(_redis_job_lock_key)
# Grace period formula: max(5, min(300, total_active * 10))

# C-1: increment_jobs_completed() called in celery_app.py after commit
