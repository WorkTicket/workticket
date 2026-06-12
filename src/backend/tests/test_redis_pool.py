from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_pool():
    from app.sync_redis_pool import _sync_redis_pool

    _sync_redis_pool.close()


@pytest.mark.asyncio
async def test_get_sync_redis_returns_client():
    from app.sync_redis_pool import _sync_redis_pool, get_sync_redis

    mock_pool = MagicMock()
    mock_pool.ping.return_value = True

    with (
        patch.object(_sync_redis_pool, "_connect", return_value=True),
        patch.object(_sync_redis_pool, "_pool", mock_pool),
    ):
        client = get_sync_redis()
        assert client is not None
        assert client == mock_pool


@pytest.mark.asyncio
async def test_get_sync_redis_returns_none_when_unavailable():
    from app.sync_redis_pool import _sync_redis_pool, get_sync_redis

    with patch.object(_sync_redis_pool, "_connect", return_value=False):
        client = get_sync_redis()
        assert client is None


@pytest.mark.asyncio
async def test_pool_reuses_connection():
    from app.sync_redis_pool import _sync_redis_pool, get_sync_redis

    mock_pool = MagicMock()
    mock_pool.ping.return_value = True

    with (
        patch.object(_sync_redis_pool, "_connect", return_value=True),
        patch.object(_sync_redis_pool, "_pool", mock_pool),
    ):
        client1 = get_sync_redis()
        client2 = get_sync_redis()
        assert client1 is client2
        assert client1 == mock_pool


@pytest.mark.asyncio
async def test_pool_survives_multiple_calls():
    from app.sync_redis_pool import _sync_redis_pool

    mock_pool = MagicMock()
    mock_pool.ping.return_value = True

    with (
        patch.object(_sync_redis_pool, "_connect", return_value=True),
        patch.object(_sync_redis_pool, "_pool", mock_pool),
    ):
        for _ in range(20):
            client = _sync_redis_pool.get_client()
            assert client is not None

        _sync_redis_pool._available = True
        stats = _sync_redis_pool.get_pool_stats()
        assert stats.get("available") is True


@pytest.mark.asyncio
async def test_pool_reconnects_after_failure():
    from app.sync_redis_pool import _sync_redis_pool

    mock_pool = MagicMock()
    mock_pool.ping.side_effect = [Exception("down"), True]

    call_count = 0

    def connect_side_effect():
        nonlocal call_count
        call_count += 1
        _sync_redis_pool._available = call_count > 1
        _sync_redis_pool._pool = mock_pool if call_count > 1 else None
        return call_count > 1

    with patch.object(_sync_redis_pool, "_connect", side_effect=connect_side_effect):
        _sync_redis_pool._available = False
        _sync_redis_pool._pool = None
        result1 = _sync_redis_pool.get_client()
        assert result1 is None, "First call should fail"

        result2 = _sync_redis_pool.get_client()
        assert result2 is not None, "Second call should recover"


@pytest.mark.asyncio
async def test_pool_stats_report_metrics():
    from app.sync_redis_pool import _sync_redis_pool

    mock_pool = MagicMock()
    mock_pool.ping.return_value = True
    mock_conn_pool = MagicMock()
    mock_conn_pool._in_use_connections = 3
    mock_conn_pool._created_connections = 10
    mock_pool.connection_pool = mock_conn_pool

    with (
        patch.object(_sync_redis_pool, "_connect", return_value=True),
        patch.object(_sync_redis_pool, "_pool", mock_pool),
    ):
        _sync_redis_pool.get_client()
        _sync_redis_pool._available = True
        stats = _sync_redis_pool.get_pool_stats()
        assert stats.get("available") is True
        assert stats.get("in_use") == 3
        assert stats.get("total") == 10


@pytest.mark.asyncio
async def test_pool_close_clears_connection():
    from app.sync_redis_pool import _sync_redis_pool, get_sync_redis

    mock_pool = MagicMock()
    mock_pool.ping.return_value = True

    with (
        patch.object(_sync_redis_pool, "_connect", return_value=True),
        patch.object(_sync_redis_pool, "_pool", mock_pool),
    ):
        client = get_sync_redis()
        assert client is not None

        _sync_redis_pool.close()
        assert _sync_redis_pool._pool is None
        assert _sync_redis_pool._available is False
        mock_pool.close.assert_called_once()
