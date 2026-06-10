import json
import logging
import os
import re
import sys
import time
import traceback
from collections import OrderedDict

_PII_PATTERNS = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL_REDACTED]"),
    (re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"), "[PHONE_REDACTED]"),
    (re.compile(r"\b[A-Za-z0-9+/]{20,}={0,2}\b"), "[TOKEN_REDACTED]"),
    (
        re.compile(r'(?i)((?:jwt|bearer|token|secret|key|password)\s*[:=]\s*)["\']?[A-Za-z0-9_\-./+=]{8,}'),
        r"\1[REDACTED]",
    ),
    (re.compile(r"\buser_[a-zA-Z0-9]{20,}\b"), "[CLERK_ID_REDACTED]"),
    (re.compile(r"\bcompany_[a-zA-Z0-9]{20,}\b"), "[COMPANY_ID_REDACTED]"),
]


def _redact_pii(text: str) -> str:
    if not text:
        return text
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


_STRUCTURED_ENABLED = os.getenv("LOG_FORMAT", "").lower() == "json" or os.getenv("STRUCTURED_LOGGING", "0").lower() in (
    "true",
    "1",
    "yes",
)


def _get_otel_trace_id() -> str | None:
    """Pull trace_id from OTel context if available."""
    try:
        from app.telemetry import get_current_trace_id

        return get_current_trace_id()
    except Exception:
        return None


def _get_middleware_trace_id() -> dict:
    """Pull trace context from the tracing middleware contextvar."""
    try:
        from app.middleware.tracing import get_logging_context

        return get_logging_context()
    except Exception:
        return {}


class PIIRedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        try:
            formatted = record.getMessage()
        except TypeError:
            formatted = str(record.msg)
        record.msg = _redact_pii(formatted)
        record.args = None
        return super().format(record)


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter with consistent key ordering."""

    def format(self, record: logging.LogRecord) -> str:
        if not _STRUCTURED_ENABLED:
            return super().format(record)

        log_entry = OrderedDict()
        log_entry["timestamp"] = self.formatTime(record, self.datefmt) or time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)
        )
        log_entry["level"] = record.levelname
        log_entry["logger"] = record.name
        log_entry["message"] = _redact_pii(record.getMessage())

        # Populate trace_id from the richest available source:
        # 1. Explicitly set on the log record (extra fields)
        # 2. OTel active span context
        # 3. Tracing middleware contextvar
        if not any(getattr(record, k, None) for k in ("trace_id",)):
            otel_id = _get_otel_trace_id()
            if otel_id:
                record.trace_id = otel_id
            else:
                mw_ctx = _get_middleware_trace_id()
                if mw_ctx.get("trace_id"):
                    record.trace_id = mw_ctx["trace_id"]

        for key in ("trace_id", "span_id", "company_id", "job_id", "user_id", "service"):
            val = getattr(record, key, None) or record.__dict__.get(key)
            if val:
                log_entry[key] = str(val)

        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "stack": "".join(traceback.format_exception(*record.exc_info)) if record.exc_info[2] else None,
            }

        return json.dumps(log_entry, default=str)


def setup_logging(service_name: str = "workticket-backend") -> None:
    """Configure structured JSON logging for production, plain text for dev."""
    handler = logging.StreamHandler(sys.stdout)
    if _STRUCTURED_ENABLED:
        formatter = JSONFormatter()
    else:
        formatter = PIIRedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Remove existing handlers and add our own
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.addHandler(handler)

    # LOW-1 FIX: Silence verbose billing DEBUG loggers in production.
    # Billing reconciliation and quota checks are high-volume and
    # produce noisy DEBUG logs that obscure actionable signals.
    for _billing_logger in ("app.billing", "billing", "app.billing.state_machine", "app.billing.reconciliation"):
        logging.getLogger(_billing_logger).setLevel(logging.WARNING)


def get_logger(name: str, trace_id: str | None = None, company_id: str | None = None, job_id: str | None = None) -> logging.Logger:
    """Get a logger with structured context pre-populated."""
    logger = logging.getLogger(name)
    logger = logging.LoggerAdapter(
        logger,
        {
            "trace_id": trace_id,
            "company_id": company_id,
            "job_id": job_id,
        },
    )
    return logger
