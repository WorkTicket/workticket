import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_GLOBAL_RETRY_BUDGET = int(os.getenv("GLOBAL_RETRY_BUDGET", "100"))
_GLOBAL_RETRY_WINDOW = int(os.getenv("GLOBAL_RETRY_WINDOW", "60"))
_TOKEN_SCALE = 10000

_budget_lock = threading.Lock()
_retry_tokens: int = _GLOBAL_RETRY_BUDGET * _TOKEN_SCALE
_last_refill: float = time.monotonic()


def _refill():
    """Refill tokens at budget/window per second using integer arithmetic."""
    global _retry_tokens, _last_refill
    now = time.monotonic()
    elapsed = now - _last_refill
    if elapsed <= 0:
        return
    refill = int(elapsed * (_GLOBAL_RETRY_BUDGET * _TOKEN_SCALE) / _GLOBAL_RETRY_WINDOW)
    _retry_tokens = min(_GLOBAL_RETRY_BUDGET * _TOKEN_SCALE, _retry_tokens + refill)
    _last_refill = now


def consume_retry_budget(task_name: str, cost: float = 1.0) -> bool:
    """Consume from global retry budget. Returns True if allowed, False if budget exhausted.

    Priorities (higher = more important):
      billing: cost=0.1 (can retry 1000/min)
      job_task: cost=1.0 (can retry 100/min)
      ai_task: cost=2.0 (can retry 50/min)
      beat: cost=0.5 (can retry 200/min)
    """
    global _retry_tokens
    with _budget_lock:
        _refill()
        cost_scaled = int(cost * _TOKEN_SCALE)
        if _retry_tokens >= cost_scaled:
            _retry_tokens -= cost_scaled
            return True
        logger.error(
            "Global retry budget exhausted (%.1f tokens remaining, need %.1f) for %s",
            _retry_tokens / _TOKEN_SCALE,
            cost,
            task_name,
        )
        try:
            from app.monitoring.prometheus import increment_counter

            increment_counter(
                "workticket_requests_shed_total",
                {"priority": task_name.split(".")[0] if "." in task_name else task_name},
            )
        except Exception:
            logger.debug("Failed to increment retry budget shed metric")
            pass  # nosec B110
        return False


def get_budget_status() -> dict:
    with _budget_lock:
        _refill()
        return {
            "tokens_remaining": _retry_tokens / _TOKEN_SCALE,
            "capacity": _GLOBAL_RETRY_BUDGET,
            "window_seconds": _GLOBAL_RETRY_WINDOW,
        }
