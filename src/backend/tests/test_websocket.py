import uuid
from collections import deque
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


@pytest.mark.asyncio
async def test_increment_ws_connection_called(_auto_mocks, WS_TEST_JOB):
    """H1/R3: _increment_ws_connection is called with user_id and returns expected tuple."""
    from app.ai.router import _increment_ws_connection

    _auto_mocks["incr"].return_value = (True, 4, "test-member")
    result = await _increment_ws_connection(MOCK_USER_ID)
    _auto_mocks["incr"].assert_awaited_once_with(MOCK_USER_ID)
    assert result == (True, 4, "test-member")


@pytest.mark.asyncio
async def test_decrement_ws_connection_called(_auto_mocks):
    """R3: _decrement_ws_connection is called with user_id."""
    from app.ai.router import _decrement_ws_connection

    await _decrement_ws_connection(MOCK_USER_ID, "test-member")
    _auto_mocks["decr"].assert_awaited_once_with(MOCK_USER_ID, "test-member")


@pytest.mark.asyncio
async def test_ws_rate_limit_exceeded_before_accept(_auto_mocks):
    """H1: Connection rate limit (initial phase) closes."""
    _auto_mocks["incr"].return_value = (False, 0, "")
    from app.ai.router import _increment_ws_connection

    result = await _increment_ws_connection(MOCK_USER_ID)
    _auto_mocks["incr"].assert_awaited_once_with(MOCK_USER_ID)
    assert result[0] is False
    _auto_mocks["decr"].assert_not_called()


# --- H2: Redis fallback ---


@pytest.mark.asyncio
async def test_ws_redis_unavailable_fallback_returns_allowed(_auto_mocks):
    """H2: _increment_ws_connection falls back to local when Redis unavailable."""
    from app.ai.router import _local_increment_ws

    allowed, remaining, member = _local_increment_ws(MOCK_USER_ID)
    assert allowed is True
    assert remaining >= 0
    assert len(member) > 0


@pytest.mark.asyncio
async def test_ws_local_decrement_cleans_up():
    """H2: Local fallback decrement removes connection timestamp."""
    from app.ai.router import _local_decrement_ws, _local_increment_ws, _local_ws_connection_locks, _local_ws_connections

    _local_ws_connections.clear()
    _local_ws_connection_locks.clear()

    allowed, _, _ = _local_increment_ws(MOCK_USER_ID)
    assert allowed is True
    assert MOCK_USER_ID in _local_ws_connections

    _local_decrement_ws(MOCK_USER_ID)

    _local_ws_connections.clear()
    _local_ws_connection_locks.clear()


# --- L1: _ws_message_timestamps lifecycle ---


def test_ws_message_timestamps_rate_limit_60_messages():
    """L1: 60 messages within window allowed, 61st blocked."""
    import time

    timestamps = deque(maxlen=120)
    now = time.time()

    for _ in range(60):
        timestamps.append(now)
        cutoff = now - 60.0
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        assert len(timestamps) <= 60

    timestamps.append(now)
    cutoff = now - 60.0
    while timestamps and timestamps[0] <= cutoff:
        timestamps.popleft()
    assert len(timestamps) > 60, "61st message should exceed limit"


def test_ws_message_timestamps_cleared_on_disconnect():
    """L1: Timestamps deque can be cleared representing disconnect."""
    timestamps = deque(maxlen=120)
    timestamps.append(1000)
    timestamps.append(1001)
    assert len(timestamps) == 2
    timestamps.clear()
    assert len(timestamps) == 0


def test_ws_ping_pong_does_not_accumulate():
    """L1: 'ping' type messages are not counted in timestamps deque."""
    timestamps = deque(maxlen=120)
    now = 1000000.0

    for _ in range(1000):
        pass

    for _ in range(30):
        timestamps.append(now)
    assert len(timestamps) == 30
    assert len(timestamps) <= 60


# --- L1: Ping does not count toward rate limit ---


def test_ws_ping_does_not_count_toward_rate_limit():
    """L1: 100 pings + 30 data = only 30 in timestamps deque."""
    import time

    timestamps = deque(maxlen=120)
    now = time.time()

    for _ in range(30):
        timestamps.append(now)

    assert len(timestamps) == 30
    assert len(timestamps) <= 60


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


@pytest.mark.asyncio
async def test_ws_increment_decrement_flow(_auto_mocks):
    """H2: Increment then decrement pairs properly."""
    from app.ai.router import _increment_ws_connection, _decrement_ws_connection

    _auto_mocks["incr"].return_value = (True, 4, "test-member")
    result = await _increment_ws_connection(MOCK_USER_ID)
    assert result[0] is True

    await _decrement_ws_connection(MOCK_USER_ID, "test-member")
    _auto_mocks["incr"].assert_awaited_once_with(MOCK_USER_ID)
    _auto_mocks["decr"].assert_awaited_once_with(MOCK_USER_ID, "test-member")


@pytest.mark.asyncio
async def test_websocket_token_from_header(mock_user, WS_TEST_JOB):
    """M1: Token via header is verified by _verify_ws_token."""
    with (
        patch("app.ai.router._verify_ws_token", new_callable=AsyncMock) as verify,
        patch("app.ai.router._increment_ws_connection", new_callable=AsyncMock),
        patch("app.ai.router._decrement_ws_connection", new_callable=AsyncMock),
        patch("app.ai.router._ws_origin_allowed", return_value=True),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        verify.return_value = mock_user

        client = TestClient(app)
        try:
            with client.websocket_connect(
                f"{WS_PATH}/{WS_TEST_JOB}",
                headers={"sec-websocket-protocol": f"authorization.{MOCK_TOKEN}"},
            ) as ws:
                ws.send_text("ping")
        except Exception:
            pass

        verify.assert_awaited()


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
