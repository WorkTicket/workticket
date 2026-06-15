"""P1+P2: Canary deployment controller and auto-rollback engine.

Provides:
  - Canary traffic routing with graduated rollout (5% -> 25% -> 50% -> 100%)
  - Automatic rollback on sustained error rate breach or health degradation
  - Decision logging and Prometheus metrics for audit
  - Integration with /readyz gate for deployment orchestration

Configuration via environment:
  CANARY_ENABLED: Enable canary deployment mode (default: false)
  CANARY_STAGE: Current stage (off|pct5|pct25|pct50|pct100)
  CANARY_ERROR_THRESHOLD_PCT: Error rate % that triggers rollback (default: 5.0)
  CANARY_LATENCY_THRESHOLD_MS: P95 latency threshold for rollback (default: 5000)
  CANARY_STAGE_MIN_DURATION: Minimum seconds per stage before promotion (default: 300)
  DEPLOY_VERSION: Current deployment version string
  PREVIOUS_DEPLOY_VERSION: Previous stable version for rollback target
"""

import logging
import os
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_CANARY_ENABLED = os.environ.get("CANARY_ENABLED", "").lower() in ("true", "1", "yes")
_CANARY_STAGE = os.environ.get("CANARY_STAGE", "off")
_CANARY_ERROR_THRESHOLD = float(os.environ.get("CANARY_ERROR_THRESHOLD_PCT", "5.0"))
_CANARY_LATENCY_THRESHOLD = float(os.environ.get("CANARY_LATENCY_THRESHOLD_MS", "5000"))
_CANARY_STAGE_MIN_DURATION = float(os.environ.get("CANARY_STAGE_MIN_DURATION", "300"))
_DEPLOY_VERSION = os.environ.get("DEPLOY_VERSION", "unknown")
_PREVIOUS_VERSION = os.environ.get("PREVIOUS_DEPLOY_VERSION", "")

_CANARY_STAGES = ["off", "pct5", "pct25", "pct50", "pct100"]
_CANARY_WEIGHTS = {"off": 0, "pct5": 5, "pct25": 25, "pct50": 50, "pct100": 100}

_rollback_triggered = False
_rollback_reason = ""
_rollback_timestamp: float = 0.0
_current_stage_start: float = 0.0
_promotion_decision_count = 0
_rollback_decision_count = 0


@dataclass
class CanaryStatus:
    enabled: bool
    stage: str
    weight_pct: int
    version: str
    previous_version: str
    stage_elapsed_seconds: float
    rollback_triggered: bool
    rollback_reason: str
    promotion_count: int
    rollback_count: int


@dataclass
class RollbackDecision:
    should_rollback: bool
    reason: str = ""
    metrics: dict = field(default_factory=dict)


def _get_stage_weight(stage: str) -> int:
    return _CANARY_WEIGHTS.get(stage, 0)


def _get_next_stage(current: str) -> str | None:
    try:
        idx = _CANARY_STAGES.index(current)
        if idx < len(_CANARY_STAGES) - 1:
            return _CANARY_STAGES[idx + 1]
    except ValueError:
        pass
    return None


def _get_previous_stage(current: str) -> str | None:
    try:
        idx = _CANARY_STAGES.index(current)
        if idx > 0:
            return _CANARY_STAGES[idx - 1]
    except ValueError:
        pass
    return None


def get_canary_status() -> CanaryStatus:
    global _current_stage_start
    elapsed = time.time() - _current_stage_start if _current_stage_start > 0 else 0
    return CanaryStatus(
        enabled=_CANARY_ENABLED,
        stage=_CANARY_STAGE,
        weight_pct=_get_stage_weight(_CANARY_STAGE),
        version=_DEPLOY_VERSION,
        previous_version=_PREVIOUS_VERSION,
        stage_elapsed_seconds=elapsed,
        rollback_triggered=_rollback_triggered,
        rollback_reason=_rollback_reason,
        promotion_count=_promotion_decision_count,
        rollback_count=_rollback_decision_count,
    )


def is_canary_active() -> bool:
    return _CANARY_ENABLED and _CANARY_STAGE != "off" and _CANARY_STAGE != "pct100"


def should_accept_canary_traffic() -> bool:
    """Determine if this instance should handle this request based on canary weight."""
    if not _CANARY_ENABLED or _CANARY_STAGE == "off":
        return False
    if _CANARY_STAGE == "pct100":
        return True
    import random

    weight = _get_stage_weight(_CANARY_STAGE)
    return random.randint(1, 100) <= weight  # nosec B311


def evaluate_rollback(error_rate_pct: float, p95_latency_ms: float) -> RollbackDecision:
    """P2 FIX: Evaluate whether auto-rollback should be triggered.

    Checks:
      1. Sustained error rate above threshold
      2. P95 latency spike above threshold
      3. Consecutive health check failures (checked externally)

    Returns RollbackDecision with should_rollback flag and reason.
    """
    global _rollback_triggered, _rollback_reason, _rollback_timestamp
    global _rollback_decision_count

    reasons = []
    if error_rate_pct > _CANARY_ERROR_THRESHOLD:
        reasons.append(f"error_rate={error_rate_pct:.1f}% > threshold={_CANARY_ERROR_THRESHOLD}%")
    if p95_latency_ms > _CANARY_LATENCY_THRESHOLD:
        reasons.append(f"p95_latency={p95_latency_ms:.0f}ms > threshold={_CANARY_LATENCY_THRESHOLD}ms")

    if reasons:
        _rollback_decision_count += 1
        if not _rollback_triggered:
            _rollback_triggered = True
            _rollback_reason = "; ".join(reasons)
            _rollback_timestamp = time.time()
            logger.critical(
                "AUTO-ROLLBACK TRIGGERED: %s. Rolling back %s -> %s",
                _rollback_reason,
                _DEPLOY_VERSION,
                _PREVIOUS_VERSION or "previous",
            )
            try:
                from app.monitoring.prometheus import increment_counter

                increment_counter(
                    "workticket_canary_rollback_triggered_total",
                    {
                        "version": _DEPLOY_VERSION,
                        "reason": _rollback_reason[:100],
                    },
                )
            except Exception:
                logger.debug("Failed to increment canary rollback metric")
                pass  # nosec B110
        return RollbackDecision(
            should_rollback=True,
            reason=_rollback_reason,
            metrics={
                "error_rate_pct": error_rate_pct,
                "p95_latency_ms": p95_latency_ms,
                "current_stage": _CANARY_STAGE,
            },
        )

    return RollbackDecision(
        should_rollback=False,
        reason="",
        metrics={
            "error_rate_pct": error_rate_pct,
            "p95_latency_ms": p95_latency_ms,
        },
    )


def promote_canary_stage() -> dict:
    """Promote to next canary stage if minimum duration has elapsed and health is green.

    Returns dict with {promoted, new_stage, reason}.
    """
    global _current_stage_start, _promotion_decision_count

    if not _CANARY_ENABLED:
        return {"promoted": False, "new_stage": _CANARY_STAGE, "reason": "canary_disabled"}

    if _rollback_triggered:
        return {"promoted": False, "new_stage": _CANARY_STAGE, "reason": "rollback_active"}

    next_stage = _get_next_stage(_CANARY_STAGE)
    if next_stage is None:
        return {"promoted": False, "new_stage": _CANARY_STAGE, "reason": "already_at_pct100"}

    elapsed = time.time() - _current_stage_start if _current_stage_start > 0 else _CANARY_STAGE_MIN_DURATION + 1
    if elapsed < _CANARY_STAGE_MIN_DURATION:
        return {
            "promoted": False,
            "new_stage": _CANARY_STAGE,
            "reason": f"stage_elapsed={elapsed:.0f}s < min={_CANARY_STAGE_MIN_DURATION}s",
        }

    _promotion_decision_count += 1
    _current_stage_start = time.time()
    logger.info(
        "CANARY PROMOTION: %s -> %s (elapsed=%.0fs, decision=%d)",
        _CANARY_STAGE,
        next_stage,
        elapsed,
        _promotion_decision_count,
    )

    try:
        from app.monitoring.prometheus import increment_counter

        increment_counter(
            "workticket_canary_promotion_total",
            {
                "from_stage": _CANARY_STAGE,
                "to_stage": next_stage,
            },
        )
    except Exception:
        logger.debug("Failed to increment canary promotion metric")
        pass  # nosec B110

    return {"promoted": True, "new_stage": next_stage, "reason": f"elapsed={elapsed:.0f}s"}


def init_canary():
    """Initialize canary deployment state at module load.
    Records the start time of the current stage so promotion timing works.
    """
    global _current_stage_start
    if _CANARY_ENABLED and _CANARY_STAGE != "off":
        _current_stage_start = time.time()
        logger.info(
            "Canary deployment active: stage=%s weight=%d%% version=%s",
            _CANARY_STAGE,
            _get_stage_weight(_CANARY_STAGE),
            _DEPLOY_VERSION,
        )


init_canary()
