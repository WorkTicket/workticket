import json
import logging
import time
from datetime import UTC
from typing import Any, Literal

from app.config import get_settings
from app.notifications.encryption import decrypt_push_token

logger = logging.getLogger(__name__)
settings = get_settings()


_delivery_log: list[dict[str, Any]] = []
_MAX_DELIVERY_LOG = 100

# Push notification circuit breaker (exponential cooldown like email/SMS)
_push_circuit_open = False
_push_circuit_failures = 0
_push_circuit_last_failure = 0.0
_PUSH_CIRCUIT_THRESHOLD = 3
_PUSH_CIRCUIT_COOLDOWN_BASE = 30.0
_PUSH_CIRCUIT_COOLDOWN_MAX = 600.0
_push_circuit_cooldown = _PUSH_CIRCUIT_COOLDOWN_BASE
_PUSH_CIRCUIT_REDIS_KEY = "push:circuit_breaker"

# Push DLQ
_PUSH_DLQ_KEY = "push:dlq"
_PUSH_DLQ_MAX = 1000
_PUSH_DLQ_TTL = 7 * 24 * 3600  # 7 days


async def _get_redis():
    try:
        from app.ai.rate_limiter import _get_redis

        return await _get_redis()
    except Exception:
        return None


async def _load_circuit_breaker():
    """Load circuit breaker state from Redis (persisted across restarts)."""
    global _push_circuit_open, _push_circuit_failures, _push_circuit_last_failure, _push_circuit_cooldown
    r = await _get_redis()
    if not r:
        return
    try:
        state = await r.get(_PUSH_CIRCUIT_REDIS_KEY)
        if state:
            import json

            data = json.loads(state)
            _push_circuit_open = data.get("open", False)
            _push_circuit_failures = data.get("failures", 0)
            _push_circuit_last_failure = data.get("last_failure", 0.0)
            _push_circuit_cooldown = data.get("cooldown", _PUSH_CIRCUIT_COOLDOWN_BASE)
    except Exception:
        logger.debug("Push notification non-critical operation failed, continuing without persistence")
        pass  # nosec B110


async def _save_circuit_breaker():
    """Persist circuit breaker state to Redis."""
    r = await _get_redis()
    if not r:
        return
    try:
        import json

        state = json.dumps(
            {
                "open": _push_circuit_open,
                "failures": _push_circuit_failures,
                "last_failure": _push_circuit_last_failure,
                "cooldown": _push_circuit_cooldown,
            }
        )
        ttl = int(_push_circuit_cooldown * 4)
        await r.setex(_PUSH_CIRCUIT_REDIS_KEY, ttl, state)
    except Exception:
        logger.debug("Push notification non-critical operation failed, continuing without persistence")
        pass  # nosec B110


async def _enqueue_push_dlq(entry: dict[str, Any]) -> None:
    """Enqueue a failed push notification to the dead letter queue."""
    r = await _get_redis()
    if not r:
        return
    try:
        import json as _json

        payload = _json.dumps(entry, default=str)
        await r.lpush(_PUSH_DLQ_KEY, payload)
        await r.ltrim(_PUSH_DLQ_KEY, 0, _PUSH_DLQ_MAX - 1)
        await r.expire(_PUSH_DLQ_KEY, _PUSH_DLQ_TTL)
    except Exception:
        logger.debug("Push notification non-critical operation failed, continuing without persistence")
        pass  # nosec B110


async def send_push_notification(
    token: str, title: str, body: str, data: dict[str, Any] | None = None, company_id: str | None = None
) -> bool | Literal["remove_token"]:
    global _push_circuit_open, _push_circuit_failures, _push_circuit_last_failure, _push_circuit_cooldown
    # Load persisted state on first call after restart
    if _push_circuit_failures == 0 and not _push_circuit_open:
        await _load_circuit_breaker()
    if _push_circuit_open:
        if time.time() - _push_circuit_last_failure < _push_circuit_cooldown:
            logger.warning("Push circuit breaker OPEN — enqueueing to DLQ")
            await _enqueue_push_dlq(
                {
                    "token": token[:20],
                    "title": title,
                    "body": body[:200],
                    "data": data,
                    "company_id": company_id,
                    "failed_at": time.time(),
                    "reason": "circuit_open",
                }
            )
            return False
        # Cooldown expired, try again in half-open state
        _push_circuit_open = False
        _push_circuit_failures = 0
        await _save_circuit_breaker()

    try:
        import time as tm

        import httpx

        decrypted = decrypt_push_token(token)
        payload = {
            "to": decrypted,
            "title": title,
            "body": body,
            "data": data or {},
            "sound": "default",
        }
        sent_at = tm.time()
        push_id: str | None = None
        delivery_status = "unknown"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://exp.host/--/api/v2/push/send",
                headers={"Content-Type": "application/json"},
                content=json.dumps(payload),
            )
            if resp.status_code == 429:
                logger.warning("Expo rate limited for token %s", token[:8])
                delivery_status = "rate_limited"
                await _log_delivery(token, title, delivery_status, push_id, sent_at, company_id)
                return False
            result = resp.json()
            if result.get("data") and isinstance(result["data"], list):
                for ticket in result["data"]:
                    status = ticket.get("status")
                    push_id = ticket.get("id")
                    if status == "error":
                        message = ticket.get("message", "")
                        logger.error("Expo push error for token %s: %s", token[:8], message)
                        delivery_status = f"error:{message[:50]}"
                        if "DeviceNotRegistered" in message:
                            await _log_delivery(token, title, delivery_status, push_id, sent_at, company_id)
                            return "remove_token"
                    elif status == "ok":
                        logger.info("Push sent to %s: id=%s", token[:8], push_id)
                        delivery_status = "sent"
            await _log_delivery(token, title, delivery_status, push_id, sent_at, company_id)
            return resp.status_code == 200
    except Exception as e:
        logger.warning("Failed to send push notification: %s", e)
        _push_circuit_failures += 1
        if _push_circuit_failures >= _PUSH_CIRCUIT_THRESHOLD:
            _push_circuit_open = True
            _push_circuit_last_failure = time.time()
            _push_circuit_cooldown = min(_push_circuit_cooldown * 2, _PUSH_CIRCUIT_COOLDOWN_MAX)
            logger.warning(
                "Push circuit breaker OPEN after %d failures (cooldown=%.0fs)",
                _push_circuit_failures,
                _push_circuit_cooldown,
            )
        await _save_circuit_breaker()
        await _log_delivery(token, title, f"exception:{str(e)[:50]}", None, tm.time(), company_id)
        await _enqueue_push_dlq(
            {
                "token": token[:20],
                "title": title,
                "body": body[:200],
                "data": data,
                "company_id": company_id,
                "failed_at": time.time(),
                "reason": f"exception:{str(e)[:100]}",
            }
        )
        return False


async def _log_delivery(
    token: str, title: str, status: str, push_id: str | None, sent_at: float, company_id: str | None = None
) -> None:
    global _delivery_log
    from datetime import datetime

    entry = {
        "token_prefix": token[:8],
        "title": title,
        "status": status,
        "push_id": push_id,
        "sent_at": datetime.fromtimestamp(sent_at, tz=UTC).isoformat(),
        "company_id": company_id,
    }
    # Try Redis-backed log first
    r = await _get_redis()
    if r:
        try:
            key = f"push:delivery:{company_id or 'global'}"
            await r.zadd(key, {json.dumps(entry): sent_at})
            await r.expire(key, 86400)
            return
        except Exception:
            logger.debug("Push notification non-critical operation failed, continuing without persistence")
        pass  # nosec B110
    # In-memory fallback
    _delivery_log.append(entry)
    if len(_delivery_log) > _MAX_DELIVERY_LOG:
        _delivery_log = _delivery_log[-(_MAX_DELIVERY_LOG // 2) :]


async def get_delivery_log(limit: int = 50, company_id: str | None = None) -> list[dict[str, Any]]:
    r = await _get_redis()
    if r:
        try:
            key = f"push:delivery:{company_id or 'global'}"
            entries = await r.zrevrange(key, 0, limit - 1)
            return [json.loads(e) for e in entries]
        except Exception:
            logger.debug("Push notification non-critical operation failed, continuing without persistence")
        pass  # nosec B110
    log = (
        _delivery_log[-limit:]
        if company_id is None
        else [e for e in _delivery_log if e.get("company_id") == company_id][-limit:]
    )
    return list(log)


async def notify_job_complete(user_id: str, job_id: str, push_tokens: list[str], company_id: str | None = None) -> None:
    for token in push_tokens:
        await send_push_notification(
            token=token,
            title="AI Analysis Complete",
            body=f"Job {job_id[:8]} has been analyzed. View the results now.",
            data={"job_id": job_id, "type": "job_complete"},
            company_id=company_id,
        )


async def notify_quote_ready(user_id: str, job_id: str, push_tokens: list[str], company_id: str | None = None) -> None:
    for token in push_tokens:
        await send_push_notification(
            token=token,
            title="Quote Ready",
            body=f"A quote is ready for job {job_id[:8]}. Review and approve.",
            data={"job_id": job_id, "type": "quote_ready"},
            company_id=company_id,
        )


async def notify_quote_approved(job_id: str, push_tokens: list[tuple[int, str]], company_id: str | None = None) -> None:
    for token_id, token in push_tokens:
        result = await send_push_notification(
            token=token,
            title="Quote Approved",
            body=f"Quote for job {job_id[:8]} has been approved and sent.",
            data={"job_id": job_id, "type": "quote_approved"},
            company_id=company_id,
        )
        if result == "remove_token":
            from sqlalchemy import delete

            from app.database import AsyncSessionLocal
            from app.notifications.models import PushToken

            async with AsyncSessionLocal() as db:
                await db.execute(delete(PushToken).where(PushToken.id == token_id))
                await db.commit()
                logger.info("Removed stale push token %s", token_id)
