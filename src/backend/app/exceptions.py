import logging
import uuid
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class WorkTicketError(Exception):
    """Base exception for WorkTicket application."""

    def __init__(
        self,
        message: str,
        error_code: str = "INTERNAL_ERROR",
        status_code: int = 500,
        details: dict[str, Any] | None = None,
    ):
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


class ValidationError(WorkTicketError):
    """Raised when input validation fails."""

    def __init__(
        self,
        message: str = "Validation error",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            message=message,
            error_code="VALIDATION_ERROR",
            status_code=400,
            details=details,
        )


class AuthenticationError(WorkTicketError):
    """Raised when authentication fails."""

    def __init__(
        self,
        message: str = "Authentication failed",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            message=message,
            error_code="AUTHENTICATION_ERROR",
            status_code=401,
            details=details,
        )


class AuthorizationError(WorkTicketError):
    """Raised when user lacks permissions."""

    def __init__(
        self,
        message: str = "Insufficient permissions",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            message=message,
            error_code="AUTHORIZATION_ERROR",
            status_code=403,
            details=details,
        )


class NotFoundError(WorkTicketError):
    """Raised when a resource is not found."""

    def __init__(
        self,
        message: str = "Resource not found",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            message=message,
            error_code="NOT_FOUND",
            status_code=404,
            details=details,
        )


class ConflictError(WorkTicketError):
    """Raised when there is a conflict with current state."""

    def __init__(
        self,
        message: str = "Conflict",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            message=message,
            error_code="CONFLICT",
            status_code=409,
            details=details,
        )


class RateLimitError(WorkTicketError):
    """Raised when rate limit is exceeded."""

    def __init__(
        self,
        message: str = "Rate limit exceeded",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            message=message,
            error_code="RATE_LIMIT_EXCEEDED",
            status_code=429,
            details=details,
        )


class QuotaExceededError(WorkTicketError):
    """Raised when quota is exceeded."""

    def __init__(
        self,
        message: str = "Quota exceeded",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            message=message,
            error_code="QUOTA_EXCEEDED",
            status_code=402,
            details=details,
        )


class ServiceUnavailableError(WorkTicketError):
    """Raised when a service is unavailable."""

    def __init__(
        self,
        message: str = "Service unavailable",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(
            message=message,
            error_code="SERVICE_UNAVAILABLE",
            status_code=503,
            details=details,
        )


async def workticket_exception_handler(request: Request, exc: WorkTicketError) -> JSONResponse:
    """Handle WorkTicketError and return consistent JSON response."""
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

    log_level = logger.error if exc.status_code >= 500 else logger.warning
    log_level(
        f"WorkTicketError: {exc.error_code} - {exc.message}",
        extra={
            "error_code": exc.error_code,
            "message": exc.message,
            "status_code": exc.status_code,
            "details": exc.details,
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
        },
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": {
                "code": exc.error_code,
                "message": exc.message,
                "request_id": request_id,
                "details": exc.details,
            },
        },
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Handle HTTPException and convert to consistent format."""
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

    log_level = logger.error if exc.status_code >= 500 else logger.warning
    log_level(
        f"HTTPException: {exc.status_code} - {exc.detail}",
        extra={
            "status_code": exc.status_code,
            "error_message": exc.detail,
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
        },
    )

    # If the detail is already a dict with our expected format, use it
    if isinstance(exc.detail, dict) and "code" in exc.detail and "message" in exc.detail:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": {
                    "code": exc.detail["code"],
                    "message": exc.detail["message"],
                    "request_id": request_id,
                    "details": exc.detail.get("details", {}),
                },
            },
        )

    # Otherwise, wrap it
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "error": {
                "code": f"HTTP_{exc.status_code}",
                "message": str(exc.detail),
                "request_id": request_id,
                "details": {},
            },
        },
    )


async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected exceptions."""
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

    logger.error(
        f"Unhandled exception: {type(exc).__name__} - {exc!s}",
        extra={
            "exception_type": type(exc).__name__,
            "error_message": str(exc),
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
        },
        exc_info=True,
    )

    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": "An internal server error occurred",
                "request_id": request_id,
                "details": {},
            },
        },
    )


def db_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle DBUnavailableError exceptions from the database circuit breaker."""
    import logging

    _logger = logging.getLogger(__name__)
    _logger.error("DB unavailable: %s, path=%s", exc, request.url.path)
    request_id = getattr(request.state, "correlation_id", "unknown")
    return JSONResponse(
        status_code=503,
        content={
            "success": False,
            "error": {
                "code": "DATABASE_UNAVAILABLE",
                "message": "The database is temporarily unavailable. Please try again later.",
                "request_id": request_id,
                "details": {"retry_after": 30},
            },
        },
        headers={"Retry-After": "30"},
    )


def setup_exception_handlers(app):
    """Setup exception handlers for the FastAPI application."""
    from app.database import DBUnavailableError

    app.add_exception_handler(WorkTicketError, workticket_exception_handler)
    app.add_exception_handler(DBUnavailableError, db_unavailable_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, general_exception_handler)
