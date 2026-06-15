"""OpenTelemetry distributed tracing setup.

Provides centralized OTel SDK initialization that instruments FastAPI, Celery,
SQLAlchemy, Redis, and httpx. Trace context is propagated via W3C traceparent
headers and correlated with the existing ExecutionTrace DB model and structured
logging.

Usage:
    from app.telemetry import setup_otel, setup_celery_otel

    # In main.py lifespan (startup), after app and engine exist:
    setup_otel(app=app, engine=engine)

    # In celery_app.py, after Celery app is created:
    setup_celery_otel()

Requirements (pip install):
    opentelemetry-sdk
    opentelemetry-api
    opentelemetry-exporter-otlp
    opentelemetry-instrumentation-fastapi
    opentelemetry-instrumentation-celery
    opentelemetry-instrumentation-sqlalchemy
    opentelemetry-instrumentation-redis
    opentelemetry-instrumentation-httpx
"""

import logging
import os

logger = logging.getLogger(__name__)

_OTEL_INITIALIZED = False
_CELERY_OTEL_INITIALIZED = False


def setup_otel(app=None, engine=None):
    """Initialize OpenTelemetry SDK with all instrumentations.

    Only initializes if OTEL_EXPORTER_OTLP_ENDPOINT is set.
    Safe to call multiple times -- only initializes once.
    """
    global _OTEL_INITIALIZED
    if _OTEL_INITIALIZED:
        return

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        logger.info("OpenTelemetry disabled: set OTEL_EXPORTER_OTLP_ENDPOINT to enable")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import (
            SERVICE_NAME,
            SERVICE_VERSION,
            Resource,
        )
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        service_name = os.getenv("OTEL_SERVICE_NAME", "workticket-backend")
        service_version = os.getenv("APP_VERSION", "1.0.0-beta.1")

        resource = Resource.create(
            {
                SERVICE_NAME: service_name,
                SERVICE_VERSION: service_version,
                "deployment.environment": os.getenv("APP_ENV", "production"),
            }
        )

        tracer_provider = TracerProvider(resource=resource)
        span_processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=10))
        tracer_provider.add_span_processor(span_processor)
        trace.set_tracer_provider(tracer_provider)

        if app is not None:
            FastAPIInstrumentor.instrument_app(app)
            logger.debug("FastAPI instrumented for OpenTelemetry")

        if engine is not None:
            SQLAlchemyInstrumentor().instrument(engine=engine)
            logger.debug("SQLAlchemy engine instrumented for OpenTelemetry")

        RedisInstrumentor().instrument()
        logger.debug("Redis instrumented for OpenTelemetry")

        HTTPXClientInstrumentor().instrument()
        logger.debug("httpx instrumented for OpenTelemetry")

        try:
            RequestsInstrumentor().instrument()
            logger.debug("requests instrumented for OpenTelemetry")
        except Exception:
            logger.debug("Requests instrumentation skipped (non-critical)")
            pass  # nosec B110

        _OTEL_INITIALIZED = True
        logger.info("OpenTelemetry initialized, exporting traces to %s", endpoint)

    except ImportError as e:
        logger.warning(
            "OpenTelemetry packages not installed: %s. "
            "Install with: pip install opentelemetry-{sdk,api,exporter-otlp,"
            "instrumentation-fastapi,instrumentation-celery,"
            "instrumentation-sqlalchemy,instrumentation-redis,"
            "instrumentation-httpx}",
            e,
        )
    except Exception as e:
        logger.error("Failed to initialize OpenTelemetry: %s", e)


def setup_celery_otel():
    """Instrument Celery after the Celery app is created.

    Must be called at module level in celery_app.py, after the Celery() instance
    is created, so the instrumentation can hook into Celery signals.
    """
    global _CELERY_OTEL_INITIALIZED, _OTEL_INITIALIZED
    if _CELERY_OTEL_INITIALIZED:
        return

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        _CELERY_OTEL_INITIALIZED = True
        return

    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor

        CeleryInstrumentor().instrument()
        _CELERY_OTEL_INITIALIZED = True
        logger.debug("Celery instrumented for OpenTelemetry")
    except ImportError as e:
        logger.warning("OpenTelemetry Celery instrumentation not available: %s", e)
    except Exception as e:
        logger.error("Failed to instrument Celery: %s", e)


def get_current_trace_id() -> str | None:
    """Return the current trace ID from OTel span context, if available.

    Returns None if OTel is not initialized or no active span exists.
    """
    if not _OTEL_INITIALIZED:
        return None
    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span and span.get_span_context().is_valid:
            return format(span.get_span_context().trace_id, "032x")
    except Exception:
        logger.debug("Failed to get current trace ID from OTel span context")
        pass  # nosec B110
    return None


def get_current_span_id() -> str | None:
    """Return the current span ID from OTel span context."""
    if not _OTEL_INITIALIZED:
        return None
    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span and span.get_span_context().is_valid:
            return format(span.get_span_context().span_id, "016x")
    except Exception:
        logger.debug("Failed to get current span ID from OTel span context")
        pass  # nosec B110
    return None


def add_span_event(name: str, attributes: dict | None = None):
    """Add a structured event to the current span."""
    if not _OTEL_INITIALIZED:
        return
    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span:
            span.add_event(name, attributes or {})
    except Exception:
        logger.debug("Failed to add span event to OTel span")
        pass  # nosec B110


def set_span_attribute(key: str, value: str | bool | int | float):
    """Set an attribute on the current span."""
    if not _OTEL_INITIALIZED:
        return
    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span:
            span.set_attribute(key, value)
    except Exception:
        logger.debug("Failed to set attribute on OTel span")
        pass  # nosec B110


def record_exception_on_span(exc: Exception, attributes: dict | None = None):
    """Record an exception on the current span."""
    if not _OTEL_INITIALIZED:
        return
    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span:
            span.record_exception(exc, attributes=attributes or {})
    except Exception:
        logger.debug("Failed to record exception on OTel span")
        pass  # nosec B110
