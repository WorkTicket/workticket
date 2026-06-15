import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


_anomaly_window_minutes = 15
_anomaly_threshold_multiplier = 2.0


class AnomalyDetector:
    def __init__(self):
        self._local_baseline = {}
        self._last_baseline_update = None
        self._min_baseline_samples = 10

    async def _get_redis(self):
        try:
            from app.ai.rate_limiter import _get_redis

            return await _get_redis()
        except Exception:
            return None

    async def _get_baseline(self, key: str) -> dict | None:
        r = await self._get_redis()
        if r:
            try:
                data = await r.hgetall(f"baseline:{key}")
                if data:
                    return {"avg_ms": float(data.get(b"avg_ms", 0)), "samples": int(data.get(b"samples", 0))}
            except Exception:
                logger.debug("Redis baseline operation failed, falling back to local cache")
        pass  # nosec B110
        return self._local_baseline.get(key)

    async def _set_baseline(self, key: str, baseline: dict):
        r = await self._get_redis()
        if r:
            try:
                await r.hset(f"baseline:{key}", mapping=baseline)
                await r.expire(f"baseline:{key}", 1800)
            except Exception:
                logger.debug("Redis baseline operation failed, falling back to local cache")
        pass  # nosec B110
        self._local_baseline[key] = baseline

    async def check_latency_anomaly(
        self,
        step_name: str,
        current_avg_ms: float,
        company_id: str | None = None,
    ) -> dict | None:
        key = f"{step_name}:{company_id or 'global'}"
        baseline = await self._get_baseline(key)
        if baseline is None:
            await self._set_baseline(key, {"avg_ms": current_avg_ms, "samples": 1})
            return None
        if baseline["samples"] < self._min_baseline_samples:
            baseline["avg_ms"] = (baseline["avg_ms"] * baseline["samples"] + current_avg_ms) / (baseline["samples"] + 1)
            baseline["samples"] += 1
            await self._set_baseline(key, baseline)
            return None
        if current_avg_ms > baseline["avg_ms"] * _anomaly_threshold_multiplier and current_avg_ms > 1000:
            alert = {
                "step": step_name,
                "metric": "latency_ms",
                "current": round(current_avg_ms, 2),
                "baseline": round(baseline["avg_ms"], 2),
                "ratio": round(current_avg_ms / baseline["avg_ms"], 2),
                "threshold": _anomaly_threshold_multiplier,
                "company_id": company_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "severity": "warning" if current_avg_ms < baseline["avg_ms"] * 3 else "critical",
            }
            logger.warning("Anomaly detected: %s", alert)
            return alert
        baseline["avg_ms"] = (baseline["avg_ms"] * 0.9) + (current_avg_ms * 0.1)
        await self._set_baseline(key, baseline)
        return None

    async def check_failure_rate_anomaly(
        self,
        current_rate: float,
        company_id: str | None = None,
    ) -> dict | None:
        key = f"failure_rate:{company_id or 'global'}"
        baseline = await self._get_baseline(key)
        if baseline is None:
            await self._set_baseline(key, {"rate": current_rate, "samples": 1})
            return None
        if baseline["samples"] < self._min_baseline_samples:
            baseline["rate"] = (baseline["rate"] * baseline["samples"] + current_rate) / (baseline["samples"] + 1)
            baseline["samples"] += 1
            await self._set_baseline(key, baseline)
            return None
        if current_rate > baseline["rate"] * _anomaly_threshold_multiplier and current_rate > 0.05:
            alert = {
                "metric": "failure_rate",
                "current": round(current_rate, 4),
                "baseline": round(baseline["rate"], 4),
                "ratio": round(current_rate / baseline["rate"], 2) if baseline["rate"] > 0 else 0,
                "company_id": company_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "severity": "warning" if current_rate < baseline["rate"] * 3 else "critical",
            }
            logger.warning("Anomaly detected: %s", alert)
            return alert
        baseline["rate"] = (baseline["rate"] * 0.9) + (current_rate * 0.1)
        await self._set_baseline(key, baseline)
        return None

    async def check_output_quality_drift(
        self,
        current_avg_confidence: float,
        fallback_rate: float,
        partial_failure_rate: float,
        company_id: str | None = None,
    ) -> list:
        alerts = []
        for metric_name, current_val, threshold in [
            ("avg_confidence", current_avg_confidence, 0.3),
            ("fallback_rate", fallback_rate, 0.1),
            ("partial_failure_rate", partial_failure_rate, 0.1),
        ]:
            key = f"quality:{metric_name}:{company_id or 'global'}"
            baseline = await self._get_baseline(key)
            if baseline is None:
                await self._set_baseline(key, {"value": current_val, "samples": 1})
                continue
            if baseline["samples"] < self._min_baseline_samples:
                baseline["value"] = (baseline["value"] * baseline["samples"] + current_val) / (baseline["samples"] + 1)
                baseline["samples"] += 1
                await self._set_baseline(key, baseline)
                continue
            deviation = abs(current_val - baseline["value"])
            if metric_name == "avg_confidence":
                if current_val < baseline["value"] - threshold:
                    alerts.append(
                        {
                            "metric": metric_name,
                            "current": round(current_val, 3),
                            "baseline": round(baseline["value"], 3),
                            "drop": round(deviation, 3),
                            "severity": "warning" if deviation < threshold * 2 else "critical",
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                    )
                baseline["value"] = (baseline["value"] * 0.9) + (current_val * 0.1)
            else:
                if current_val > baseline["value"] + threshold:
                    alerts.append(
                        {
                            "metric": metric_name,
                            "current": round(current_val, 4),
                            "baseline": round(baseline["value"], 4),
                            "increase": round(deviation, 4),
                            "severity": "warning" if deviation < threshold * 2 else "critical",
                            "timestamp": datetime.now(UTC).isoformat(),
                        }
                    )
                baseline["value"] = (baseline["value"] * 0.9) + (current_val * 0.1)
            await self._set_baseline(key, baseline)
        return alerts


anomaly_detector = AnomalyDetector()
