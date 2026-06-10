import json
import logging

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import ASGIApp

from app.ai.gateway import _sanitize_output_dict
from app.config import get_settings

logger = logging.getLogger(__name__)


class _SanitizedStreamingResponse(StreamingResponse):
    """Wraps a StreamingResponse to sanitize each chunk before sending."""

    def __init__(self, response: StreamingResponse):
        headers = dict(response.headers)
        headers.pop("content-length", None)
        super().__init__(
            content=self._sanitized_stream(response.body_iterator),
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
            background=response.background,
        )

    async def _sanitized_stream(self, body_iterator):
        buffer = b""
        async for chunk in body_iterator:
            buffer += chunk
            try:
                decoded = buffer.decode("utf-8")
                if isinstance(decoded, str) and decoded.strip():
                    data = json.loads(decoded)
                    sanitized = _sanitize_output_dict(data)
                    yield (json.dumps(sanitized, default=str) + "\n").encode("utf-8")
                else:
                    yield chunk
                buffer = b""
            except (json.JSONDecodeError, UnicodeDecodeError):
                yield chunk
                buffer = b""


class AIResponseSanitizationMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)
        self._api_prefix = get_settings().api_v1_prefix

    def _should_sanitize(self, path: str) -> bool:
        prefix = self._api_prefix
        ai_paths = [
            f"{prefix}/ai/output/",
            f"{prefix}/ai/process-job/",
            f"{prefix}/estimates/",
            f"{prefix}/quotes/",
            f"{prefix}/ai/anomaly-check",
            f"{prefix}/ai/metrics",
        ]
        return any(path.startswith(p) for p in ai_paths)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not self._should_sanitize(path):
            return await call_next(request)

        response = await call_next(request)

        if isinstance(response, StreamingResponse):
            return _SanitizedStreamingResponse(response)

        body = await _get_response_body(response)
        if body is None:
            logger.warning(
                "AI sanitization middleware could not parse response body for %s — response type=%s, status=%d",
                path,
                type(response).__name__,
                response.status_code,
            )
            return response

        sanitized = _sanitize_output_dict(body)
        new_resp = JSONResponse(
            content=sanitized,
            status_code=response.status_code,
            headers=dict(response.headers),
        )
        return new_resp


async def _get_response_body(response: Response) -> object:
    if hasattr(response, "body"):
        body_bytes = response.body
    else:
        chunks = [chunk async for chunk in response.body_iterator]  # type: ignore[attr-defined]
        body_bytes = b"".join(chunks)
    if not body_bytes:
        return None
    try:
        return json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
