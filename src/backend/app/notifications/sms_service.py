import base64
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
_CIRCUIT_REDIS_KEY = "sms:circuit_breaker"
_DELIVERY_LOG_KEY_PREFIX = "sms:delivery:"
_DLQ_KEY = "sms:dlq"

_sms_latencies: list[float] = []
_sms_failures_total: int = 0
_sms_success_total: int = 0


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
        logger.debug("SMS non-critical operation failed, continuing without persistence")
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
        logger.debug("SMS non-critical operation failed, continuing without persistence")
        pass  # nosec B110


async def _log_delivery(to: str, status: str, company_id: str | None = None):
    r = await _get_redis()
    if not r:
        return
    try:
        entry = json.dumps(
            {
                "to": to,
                "status": status,
                "timestamp": time.time(),
                "company_id": company_id,
            }
        )
        key = f"{_DELIVERY_LOG_KEY_PREFIX}{company_id or 'global'}"
        await r.zadd(key, {entry: time.time()})
        await r.expire(key, 86400)
    except Exception:
        logger.debug("SMS non-critical operation failed, continuing without persistence")
        pass  # nosec B110


async def _enqueue_dlq(to: str, body: str, company_id: str | None = None):
    r = await _get_redis()
    if not r:
        return
    try:
        entry = json.dumps(
            {
                "to": to,
                "body": body,
                "company_id": company_id,
                "enqueued_at": time.time(),
                "retry_count": 0,
            }
        )
        await r.lpush(_DLQ_KEY, entry)
        await r.ltrim(_DLQ_KEY, 0, 999)
        await r.expire(_DLQ_KEY, 604800)
        logger.info("Enqueued SMS to %s on DLQ (circuit was open)", to)
    except Exception:
        logger.debug("SMS non-critical operation failed, continuing without persistence")
        pass  # nosec B110


def _basic_auth() -> str:
    token = f"{settings.twilio_account_sid}:{settings.twilio_auth_token}"
    return base64.b64encode(token.encode()).decode()


def _report_sms_metrics(circuit_open: int, latency_ms: float, is_failure: int):
    global _sms_latencies, _sms_failures_total, _sms_success_total
    if is_failure:
        _sms_failures_total += 1
        _sms_latencies.append(latency_ms)
        if len(_sms_latencies) > 1000:
            _sms_latencies = _sms_latencies[-1000:]
    else:
        _sms_success_total += 1
        _sms_latencies.append(latency_ms)
        if len(_sms_latencies) > 1000:
            _sms_latencies = _sms_latencies[-1000:]
    try:
        from app.monitoring.prometheus import increment_sms_failure, observe_sms_latency, set_sms_circuit_state

        set_sms_circuit_state(circuit_open)
        observe_sms_latency(latency_ms)
        if is_failure:
            increment_sms_failure()
    except Exception:
        logger.debug("SMS non-critical operation failed, continuing without persistence")
        pass  # nosec B110


async def send_sms(
    to: str,
    body: str,
    company_id: str | None = None,
) -> bool:
    global _circuit_open, _circuit_failures, _circuit_last_failure, _circuit_cooldown
    _start_time = time.monotonic()

    if _circuit_failures == 0 and not _circuit_open:
        await _load_circuit_breaker()
    if _circuit_open:
        if time.time() - _circuit_last_failure < _circuit_cooldown:
            logger.warning("SMS circuit breaker OPEN — queuing to DLQ for %s", to)
            await _enqueue_dlq(to, body, company_id)
            _report_sms_metrics(1, 0, 0)
            return False
        _circuit_open = False
        _circuit_failures = 0
        _circuit_cooldown = _CIRCUIT_COOLDOWN
        await _save_circuit_breaker()

    try:
        payload = {
            "To": to,
            "From": settings.twilio_from_number,
            "Body": body,
        }
        url = f"https://api.twilio.com/2010-04-01/Accounts/{settings.twilio_account_sid}/Messages.json"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Basic {_basic_auth()}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data=payload,
            )
            _elapsed_ms = (time.monotonic() - _start_time) * 1000
            if resp.status_code == 201:
                logger.info("SMS sent to %s (%dms)", to, int(_elapsed_ms))
                await _log_delivery(to, "sent", company_id)
                _report_sms_metrics(0, _elapsed_ms, 0)
                return True
            logger.error("Twilio API error %d for %s: %s", resp.status_code, to, resp.text[:200])
            await _log_delivery(to, f"error_{resp.status_code}", company_id)
            _report_sms_metrics(0, _elapsed_ms, 1)
            await _handle_failure(to, body, company_id)
            return False
    except httpx.TimeoutException:
        _elapsed_ms = (time.monotonic() - _start_time) * 1000
        logger.error("Twilio API timed out sending to %s", to)
        await _log_delivery(to, "timeout", company_id)
        _report_sms_metrics(0, _elapsed_ms, 1)
        await _handle_failure(to, body, company_id)
        return False
    except Exception as e:
        _elapsed_ms = (time.monotonic() - _start_time) * 1000
        logger.error("Failed to send SMS to %s: %s", to, e)
        await _log_delivery(to, f"exception:{str(e)[:50]}", company_id)
        _report_sms_metrics(0, _elapsed_ms, 1)
        await _handle_failure(to, body, company_id)
        return False


async def _handle_failure(to: str, body: str, company_id: str | None = None):
    global _circuit_open, _circuit_failures, _circuit_last_failure, _circuit_cooldown
    _circuit_failures += 1
    if _circuit_failures >= _CIRCUIT_THRESHOLD and not _circuit_open:
        _circuit_open = True
        _circuit_last_failure = time.time()
        _circuit_cooldown = min(_circuit_cooldown * 2, _CIRCUIT_MAX_COOLDOWN)
        logger.warning(
            "SMS circuit breaker OPEN after %d failures (cooldown=%.0fs)",
            _circuit_failures,
            _circuit_cooldown,
        )
        await _enqueue_dlq(to, body, company_id)
    await _save_circuit_breaker()


def get_sms_metrics() -> dict:
    """Get SMS service metrics for observability endpoints."""
    global _sms_latencies, _sms_failures_total, _sms_success_total
    total = _sms_failures_total + _sms_success_total
    failure_rate = _sms_failures_total / max(total, 1)
    sorted_latencies = sorted(_sms_latencies) if _sms_latencies else [0]
    p95_idx = min(int(len(sorted_latencies) * 0.95), len(sorted_latencies) - 1)
    return {
        "total_sent": total,
        "failures": _sms_failures_total,
        "successes": _sms_success_total,
        "failure_rate": round(failure_rate, 4),
        "circuit_open": _circuit_open,
        "p95_latency_ms": round(sorted_latencies[p95_idx], 1) if sorted_latencies else 0,
    }
