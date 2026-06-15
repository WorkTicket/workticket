import json
import logging
import time

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_circuit_open = False
_circuit_failures = 0
_circuit_last_failure = 0.0
_circuit_cooldown = 30.0
_CIRCUIT_THRESHOLD = 3
_CIRCUIT_COOLDOWN = 30.0
_CIRCUIT_MAX_COOLDOWN = 600.0
_CIRCUIT_REDIS_KEY = "email:circuit_breaker"
_DELIVERY_LOG_KEY_PREFIX = "email:delivery:"
_DLQ_KEY = "email:dlq"

# L-3 FIX: Email latency histogram for observability
_email_latencies: list[float] = []
_email_failures_total: int = 0
_email_success_total: int = 0


async def _get_redis():
    try:
        from app.ai.rate_limiter import _get_redis

        return await _get_redis()
    except Exception:
        return None


async def _load_circuit_breaker():
    global _circuit_open, _circuit_failures, _circuit_last_failure, _circuit_cooldown
    r = await _get_redis()
    if not r:
        return
    try:
        state = await r.get(_CIRCUIT_REDIS_KEY)
        if state:
            data = json.loads(state)
            _circuit_open = data.get("open", False)
            _circuit_failures = data.get("failures", 0)
            _circuit_last_failure = data.get("last_failure", 0.0)
            _circuit_cooldown = data.get("cooldown", _CIRCUIT_COOLDOWN)
    except Exception:
        logger.debug("Email non-critical operation failed, continuing without persistence")
        pass  # nosec B110


async def _save_circuit_breaker():
    r = await _get_redis()
    if not r:
        return
    try:
        state = json.dumps(
            {
                "open": _circuit_open,
                "failures": _circuit_failures,
                "last_failure": _circuit_last_failure,
                "cooldown": _circuit_cooldown,
            }
        )
        ttl = int(_circuit_cooldown * 4)
        await r.setex(_CIRCUIT_REDIS_KEY, ttl, state)
    except Exception:
        logger.debug("Email non-critical operation failed, continuing without persistence")
        pass  # nosec B110


async def _log_delivery(to: str, subject: str, status: str, company_id: str | None = None):
    r = await _get_redis()
    if not r:
        return
    try:
        entry = json.dumps(
            {
                "to": to,
                "subject": subject,
                "status": status,
                "timestamp": time.time(),
                "company_id": company_id,
            }
        )
        key = f"{_DELIVERY_LOG_KEY_PREFIX}{company_id or 'global'}"
        await r.zadd(key, {entry: time.time()})
        await r.expire(key, 86400)
    except Exception:
        logger.debug("Email non-critical operation failed, continuing without persistence")
        pass  # nosec B110


async def _enqueue_dlq(to: str, subject: str, body: str, company_id: str | None = None):
    r = await _get_redis()
    if not r:
        return
    try:
        entry = json.dumps(
            {
                "to": to,
                "subject": subject,
                "body": body,
                "company_id": company_id,
                "enqueued_at": time.time(),
                "retry_count": 0,
            }
        )
        await r.lpush(_DLQ_KEY, entry)
        await r.ltrim(_DLQ_KEY, 0, 999)
        await r.expire(_DLQ_KEY, 604800)
        logger.info("Enqueued email to %s on DLQ (circuit was open)", to)
    except Exception:
        logger.debug("Email non-critical operation failed, continuing without persistence")
        pass  # nosec B110


def _report_email_metrics(circuit_open: int, latency_ms: float, is_failure: int):
    global _email_latencies, _email_failures_total, _email_success_total
    if is_failure:
        _email_failures_total += 1
        _email_latencies.append(latency_ms)
        if len(_email_latencies) > 1000:
            _email_latencies = _email_latencies[-1000:]
    else:
        _email_success_total += 1
        _email_latencies.append(latency_ms)
        if len(_email_latencies) > 1000:
            _email_latencies = _email_latencies[-1000:]
    try:
        from app.monitoring.prometheus import increment_email_failure, observe_email_latency, set_email_circuit_state

        set_email_circuit_state(circuit_open)
        observe_email_latency(latency_ms)
        if is_failure:
            increment_email_failure()
    except Exception:
        logger.debug("Email non-critical operation failed, continuing without persistence")
        pass  # nosec B110


async def send_email(
    to: str,
    subject: str,
    body: str,
    company_id: str | None = None,
) -> bool:
    global _circuit_open, _circuit_failures, _circuit_last_failure, _circuit_cooldown
    _start_time = time.monotonic()

    if _circuit_failures == 0 and not _circuit_open:
        await _load_circuit_breaker()
    if _circuit_open:
        if time.time() - _circuit_last_failure < _circuit_cooldown:
            logger.warning("Email circuit breaker OPEN — queuing to DLQ for %s", to)
            await _enqueue_dlq(to, subject, body, company_id)
            _report_email_metrics(1, 0, 0)
            return False
        _circuit_open = False
        _circuit_failures = 0
        _circuit_cooldown = _CIRCUIT_COOLDOWN
        await _save_circuit_breaker()

    try:
        payload = {
            "from": "notifications@workticket.app",
            "to": to,
            "subject": subject,
            "text": body,
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            _elapsed_ms = (time.monotonic() - _start_time) * 1000
            if resp.status_code == 200:
                logger.info("Email sent to %s (subject=%s, %dms)", to, subject, int(_elapsed_ms))
                await _log_delivery(to, subject, "sent", company_id)
                _report_email_metrics(0, _elapsed_ms, 0)
                return True
            logger.error("Resend API error %d for %s: %s", resp.status_code, to, resp.text[:200])
            await _log_delivery(to, subject, f"error_{resp.status_code}", company_id)
            _report_email_metrics(0, _elapsed_ms, 1)
            await _handle_failure(to, subject, body, company_id)
            return False
    except httpx.TimeoutException:
        _elapsed_ms = (time.monotonic() - _start_time) * 1000
        logger.error("Resend API timed out sending to %s", to)
        await _log_delivery(to, subject, "timeout", company_id)
        await _handle_failure(to, subject, body, company_id)
        _report_email_metrics(0, _elapsed_ms, 1)
        return False
    except Exception as e:
        _elapsed_ms = (time.monotonic() - _start_time) * 1000
        logger.error("Failed to send email to %s: %s", to, e)
        await _log_delivery(to, subject, f"exception:{str(e)[:50]}", company_id)
        await _handle_failure(to, subject, body, company_id)
        _report_email_metrics(0, _elapsed_ms, 1)
        return False


async def _handle_failure(to: str, subject: str, body: str, company_id: str | None = None):
    global _circuit_open, _circuit_failures, _circuit_last_failure, _circuit_cooldown
    _circuit_failures += 1
    if _circuit_failures >= _CIRCUIT_THRESHOLD and not _circuit_open:
        _circuit_open = True
        _circuit_last_failure = time.time()
        _circuit_cooldown = min(_circuit_cooldown * 2, _CIRCUIT_MAX_COOLDOWN)
        logger.warning(
            "Email circuit breaker OPEN after %d failures (cooldown=%.0fs)",
            _circuit_failures,
            _circuit_cooldown,
        )
        await _enqueue_dlq(to, subject, body, company_id)
    await _save_circuit_breaker()


def get_email_metrics() -> dict:
    """Get email service metrics for observability endpoints."""
    global _email_latencies, _email_failures_total, _email_success_total
    total = _email_failures_total + _email_success_total
    failure_rate = _email_failures_total / max(total, 1)
    sorted_latencies = sorted(_email_latencies) if _email_latencies else [0]
    p95_idx = min(int(len(sorted_latencies) * 0.95), len(sorted_latencies) - 1)
    return {
        "total_sent": total,
        "failures": _email_failures_total,
        "successes": _email_success_total,
        "failure_rate": round(failure_rate, 4),
        "circuit_open": _circuit_open,
        "p95_latency_ms": round(sorted_latencies[p95_idx], 1) if sorted_latencies else 0,
    }
