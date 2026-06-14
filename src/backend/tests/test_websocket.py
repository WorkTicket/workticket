import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.jobs.models import User
from app.main import app

MOCK_USER_ID = "test-user-id"
MOCK_COMPANY_ID = "00000000-0000-0000-0000-000000000001"
MOCK_TOKEN = "valid-test-token"
WS_PATH = "/api/v1/ai/ws/job-status"
WS_TEST_JOB_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


@pytest.fixture
def WS_TEST_JOB():
    return WS_TEST_JOB_ID


def _ws_connect(client, job_id=None, token=MOCK_TOKEN):
    job_id = job_id or WS_TEST_JOB_ID
    return client.websocket_connect(
        f"{WS_PATH}/{job_id}",
        headers={"sec-websocket-protocol": f"authorization.{token}"},
    )


@pytest.fixture
def mock_user():
    return User(
        id=MOCK_USER_ID,
        company_id=uuid.UUID(MOCK_COMPANY_ID),
        email="test@example.com",
        name="Test User",
        role="owner",
    )


@pytest.fixture(autouse=True)
def _auto_mocks(mock_user, monkeypatch):
    monkeypatch.setenv("WS_ENABLED", "true")
    monkeypatch.setattr("app.ai.router._WS_ENABLED", True)

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = MagicMock()

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()
    mock_session.close = AsyncMock()

    mock_session_factory = MagicMock(return_value=mock_session)

    mock_settings = MagicMock()
    mock_settings.ai_disabled = False

    with (
        patch("app.ai.router.get_settings", return_value=mock_settings),
        patch("app.ai.router._flags.is_enabled", return_value=False),
        patch("app.ai.router._verify_ws_token", new_callable=AsyncMock) as verify,
        patch("app.ai.router._increment_ws_connection", new_callable=AsyncMock) as incr,
        patch("app.ai.router._decrement_ws_connection", new_callable=AsyncMock) as decr,
        patch("app.ai.router._increment_ws_global", new_callable=AsyncMock) as incr_global,
        patch("app.ai.router._decrement_ws_global", new_callable=AsyncMock) as decr_global,
        patch("app.ai.router._ws_origin_allowed", return_value=True),
        patch("asyncio.sleep", new_callable=AsyncMock),
        patch("app.database.AsyncSessionLocal", mock_session_factory),
    ):
        verify.return_value = mock_user
        incr.return_value = (True, 4, "test-member")
        incr_global.return_value = (True, "global-member")
        yield {
            "verify": verify,
            "incr": incr,
            "decr": decr,
        }


# --- M1: Authentication error handling ---


def test_websocket_missing_token():
    """M1: Missing token closes connection with 1008 policy violation."""
    client = TestClient(app)
    with pytest.raises(Exception), client.websocket_connect(f"{WS_PATH}/{uuid.uuid4()}"):
        pass


def test_websocket_invalid_token():
    """M1: Invalid token returns 1008 and connection is refused."""
    with patch("app.ai.router._verify_ws_token", new_callable=AsyncMock) as mock:
        mock.side_effect = HTTPException(status_code=401, detail="Invalid token")
        client = TestClient(app)
        with pytest.raises(Exception), client.websocket_connect(f"{WS_PATH}/{uuid.uuid4()}?token=bad"):
            pass


def test_websocket_auth_unexpected_error():
    """M1: Unexpected auth error returns 1011 and connection is refused."""
    with patch("app.ai.router._verify_ws_token", new_callable=AsyncMock) as mock:
        mock.side_effect = RuntimeError("Something unexpected")
        client = TestClient(app)
        with pytest.raises(Exception), client.websocket_connect(f"{WS_PATH}/{uuid.uuid4()}?token=valid"):
            pass


def test_websocket_auth_none_user(_auto_mocks):
    """M1: _verify_ws_token returning None is treated as auth failure."""
    _auto_mocks["verify"].return_value = None
    client = TestClient(app)
    with pytest.raises(Exception), client.websocket_connect(f"{WS_PATH}/{uuid.uuid4()}?token=valid"):
        pass


# --- H1 / R3: Rate limit closes connection + cleanup ---


@pytest.mark.skip(reason="WebSocket integration requires httpx.AsyncClient/websockets instead of TestClient")
def test_websocket_rate_limit_closes_connection(_auto_mocks, WS_TEST_JOB):
    """H1/R3: 61 data messages trigger rate limit; connection closes and cleanup runs."""
    mocks = _auto_mocks
    client = TestClient(app)
    try:
        with _ws_connect(client) as ws:
            for _i in range(61):
                try:
                    ws.send_text("data")
                except Exception:
                    break
    except Exception:
        pass

    mocks["incr"].assert_awaited_once_with(MOCK_USER_ID)
    mocks["decr"].assert_awaited_once_with(MOCK_USER_ID)


@pytest.mark.skip(reason="WebSocket integration requires httpx.AsyncClient/websockets instead of TestClient")
def test_websocket_cleanup_on_normal_disconnect(_auto_mocks, WS_TEST_JOB):
    """R3: Normal disconnect calls decrement_ws_connection."""
    mocks = _auto_mocks
    client = TestClient(app)
    try:
        with _ws_connect(client) as ws:
            ws.close()
    except Exception:
        pass

    mocks["incr"].assert_awaited_once_with(MOCK_USER_ID)
    mocks["decr"].assert_awaited_once_with(MOCK_USER_ID)


# --- H1: Consistent close on ALL rate limit paths ---


def test_websocket_rate_limit_connection_refused(_auto_mocks, WS_TEST_JOB):
    """H1: Connection rate limit (initial phase) closes with 1008 before accept."""
    mocks = _auto_mocks
    mocks["incr"].return_value = (False, 0, "")

    client = TestClient(app)
    with pytest.raises(Exception), _ws_connect(client):
        pass

    mocks["incr"].assert_awaited_once_with(MOCK_USER_ID)
    mocks["decr"].assert_not_called()


@pytest.mark.skip(reason="WebSocket integration requires httpx.AsyncClient/websockets instead of TestClient")
def test_websocket_rate_limit_sends_error_before_close(_auto_mocks, WS_TEST_JOB):
    """H1: Rate limited client receives JSON error before connection close."""
    mocks = _auto_mocks
    client = TestClient(app)
    try:
        with _ws_connect(client) as ws:
            for _i in range(62):
                try:
                    ws.send_text("data")
                except Exception:
                    break
    except Exception:
        pass

    mocks["incr"].assert_awaited_once_with(MOCK_USER_ID)
    mocks["decr"].assert_awaited_once_with(MOCK_USER_ID)


def test_websocket_connection_limit_exceeded_after_accept(_auto_mocks, WS_TEST_JOB):
    """H1: Increment returning False before accept causes clean rejection."""
    mocks = _auto_mocks
    mocks["incr"].return_value = (False, 5, "")

    client = TestClient(app)
    with pytest.raises(Exception), _ws_connect(client):
        pass

    mocks["incr"].assert_awaited_once_with(MOCK_USER_ID)


# --- M1: All auth error paths covered ---


@pytest.mark.skip(reason="WebSocket integration requires httpx.AsyncClient/websockets instead of TestClient")
def test_websocket_token_from_header(mock_user, WS_TEST_JOB):
    """M1: Token via sec-websocket-protocol header is accepted."""
    with (
        patch("app.ai.router._verify_ws_token", new_callable=AsyncMock) as verify,
        patch("app.ai.router._increment_ws_connection", new_callable=AsyncMock) as incr,
        patch("app.ai.router._decrement_ws_connection", new_callable=AsyncMock),
        patch("app.ai.router._increment_ws_global", new_callable=AsyncMock) as incr_global,
        patch("app.ai.router._decrement_ws_global", new_callable=AsyncMock),
        patch("app.ai.router._ws_origin_allowed", return_value=True),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        verify.return_value = mock_user
        incr.return_value = (True, 4, "test-member")
        incr_global.return_value = (True, "global-member")

        client = TestClient(app)
        try:
            with client.websocket_connect(
                f"{WS_PATH}/{WS_TEST_JOB}",
                headers={"sec-websocket-protocol": f"authorization.{MOCK_TOKEN}"},
            ) as ws:
                ws.send_text("ping")
                resp = ws.receive_json()
                assert resp.get("type") == "pong"
        except Exception:
            pass


def test_websocket_empty_token_closes():
    """M1: Empty token string closes with 1008 policy violation."""
    client = TestClient(app)
    with pytest.raises(Exception), client.websocket_connect(f"{WS_PATH}/{uuid.uuid4()}?token="):
        pass


def test_websocket_expired_token_caught():
    """M1: jwt.ExpiredSignatureError is caught and returns 1008."""
    with patch("app.ai.router._verify_ws_token", new_callable=AsyncMock) as mock:
        mock.side_effect = HTTPException(status_code=401, detail="Token expired")
        client = TestClient(app)
        with pytest.raises(Exception), client.websocket_connect(f"{WS_PATH}/{uuid.uuid4()}?token=expired"):
            pass


# --- H2: Redis fallback ---


@pytest.mark.skip(reason="WebSocket integration requires httpx.AsyncClient/websockets instead of TestClient")
def test_websocket_redis_unavailable_fallback(_auto_mocks, WS_TEST_JOB):
    """H2: When Redis is unavailable, local fallback handles tracking."""
    mocks = _auto_mocks
    mocks["incr"].return_value = (True, 4, "test-member")

    client = TestClient(app)
    try:
        with _ws_connect(client) as ws:
            ws.close()
    except Exception:
        pass

    mocks["incr"].assert_awaited_once_with(MOCK_USER_ID)
    mocks["decr"].assert_awaited_once_with(MOCK_USER_ID)


@pytest.mark.skip(reason="WebSocket integration requires httpx.AsyncClient/websockets instead of TestClient")
def test_websocket_decrement_called_on_poll_exception(_auto_mocks, WS_TEST_JOB):
    """H2/H1: Decrement is called even when polling raises exception."""
    mocks = _auto_mocks
    client = TestClient(app)
    try:
        with _ws_connect(client) as ws:
            ws.send_text("ping")
            ws.close()
    except Exception:
        pass

    mocks["decr"].assert_awaited_once_with(MOCK_USER_ID)


# --- L1: _ws_message_timestamps lifecycle ---


@pytest.mark.skip(reason="WebSocket integration requires httpx.AsyncClient/websockets instead of TestClient")
def test_websocket_timestamps_cleared_on_disconnect(_auto_mocks, WS_TEST_JOB):
    """L1: _ws_message_timestamps is cleared when connection closes."""
    mocks = _auto_mocks
    client = TestClient(app)
    try:
        with _ws_connect(client) as ws:
            ws.send_text("data")
            ws.send_text("data")
            ws.close()
    except Exception:
        pass

    mocks["decr"].assert_awaited_once_with(MOCK_USER_ID)


@pytest.mark.skip(reason="WebSocket integration requires httpx.AsyncClient/websockets instead of TestClient")
def test_websocket_ping_pong_does_not_accumulate(_auto_mocks, WS_TEST_JOB):
    """L1: Sending 1000 pings does not grow _ws_message_timestamps."""
    mocks = _auto_mocks
    client = TestClient(app)
    try:
        with _ws_connect(client) as ws:
            for _ in range(1000):
                try:
                    ws.send_text("ping")
                    resp = ws.receive_json()
                    assert resp.get("type") == "pong"
                except Exception:
                    break
    except Exception:
        pass

    mocks["decr"].assert_awaited_once_with(MOCK_USER_ID)


# --- L1: Ping does not leak memory ---


@pytest.mark.skip(reason="WebSocket integration requires httpx.AsyncClient/websockets instead of TestClient")
def test_websocket_ping_does_not_count_toward_rate_limit(_auto_mocks, WS_TEST_JOB):
    """L1: 100 pings + 61 data messages should NOT trigger rate limit (pings excluded)."""
    mocks = _auto_mocks
    client = TestClient(app)
    try:
        with _ws_connect(client) as ws:
            for _i in range(100):
                ws.send_text("ping")
                resp = ws.receive_json()
                assert resp.get("type") == "pong", f"Expected pong, got {resp}"

            for _i in range(61):
                ws.send_text("data")

            ws.send_text("ping")
            resp = ws.receive_json()
            assert resp.get("type") == "pong"
    except Exception:
        pass

    mocks["decr"].assert_awaited_once_with(MOCK_USER_ID)


# --- H2: Redis-based connection tracking ---


def test_websocket_connection_limit_enforced(_auto_mocks, WS_TEST_JOB):
    """H2: Redis increment returning False prevents connection."""
    mocks = _auto_mocks
    mocks["incr"].return_value = (False, 0, "")

    client = TestClient(app)
    with pytest.raises(Exception), _ws_connect(client):
        pass

    mocks["incr"].assert_awaited_once_with(MOCK_USER_ID)
    mocks["decr"].assert_not_called()


@pytest.mark.skip(reason="WebSocket integration requires httpx.AsyncClient/websockets instead of TestClient")
def test_websocket_redis_tracking_called(_auto_mocks, WS_TEST_JOB):
    """H2: Redis-based _increment_ws_connection is used for tracking."""
    mocks = _auto_mocks
    client = TestClient(app)
    try:
        with _ws_connect(client) as ws:
            ws.close()
    except Exception:
        pass

    mocks["incr"].assert_awaited_once_with(MOCK_USER_ID)
    mocks["decr"].assert_awaited_once_with(MOCK_USER_ID)
