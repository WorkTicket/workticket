import logging

logger = logging.getLogger(__name__)


def parse_sentinel_url(url: str) -> tuple[str | None, str | None, str | None]:
    """Parse a Redis Sentinel service URL into (master_name, hosts_str, password).

    Supports two formats:
      sentinel://:password@host1:26379,host2:26379,host3:26379/mastername
      redis+sentinel://:password@host1:26379,host2:26379,host3:26379/mastername

    Falls back to treating the URL as a plain redis:// URL when sentinel
    scheme is not detected (returns None for all fields).
    """
    if not url:
        return None, None, None

    scheme_detected = url.startswith(("sentinel://", "redis+sentinel://"))
    if not scheme_detected:
        return None, None, None

    rest = url.split("://", 1)[1]
    auth_part, rest = rest.split("@", 1) if "@" in rest else ("", rest)
    hosts_part, _, master_name = rest.partition("/")
    password = auth_part.lstrip(":") if auth_part else None

    return master_name or None, hosts_part or None, password


def create_redis_from_url(
    url: str,
    socket_connect_timeout: float = 0.5,
    socket_timeout: float = 3.0,
    decode_responses: bool = True,
    **kwargs,
):
    """Create a Redis client from a URL, supporting Sentinel URLs.

    When the URL uses 'sentinel://' or 'redis+sentinel://' scheme,
    connects via Redis Sentinel for high-availability failover.
    Otherwise, falls back to standard redis.from_url().
    """
    master_name, hosts_str, password = parse_sentinel_url(url)

    if master_name and hosts_str:
        return _create_sentinel_client(
            master_name, hosts_str, password, socket_connect_timeout, socket_timeout, decode_responses, **kwargs
        )

    import redis.asyncio as aioredis

    return aioredis.from_url(
        url,
        decode_responses=decode_responses,
        socket_connect_timeout=socket_connect_timeout,
        socket_timeout=socket_timeout,
        **kwargs,
    )


def _create_sentinel_client(
    master_name: str,
    hosts_str: str,
    password: str | None,
    socket_connect_timeout: float,
    socket_timeout: float,
    decode_responses: bool,
    **kwargs,
):
    """Create a Redis client connected through Sentinel for HA failover."""
    try:
        import redis.asyncio as aioredis
        from redis.asyncio.sentinel import Sentinel as AsyncSentinel

        sentinel_hosts = []
        for entry in hosts_str.split(","):
            entry = entry.strip()
            if ":" in entry:
                host, port = entry.rsplit(":", 1)
                sentinel_hosts.append((host, int(port)))
            else:
                sentinel_hosts.append((entry, 26379))

        sentinel = AsyncSentinel(
            sentinel_hosts,
            socket_connect_timeout=socket_connect_timeout,
            socket_timeout=socket_timeout,
            password=password,
        )

        client = sentinel.master_for(
            master_name,
            decode_responses=decode_responses,
            password=password,
            **kwargs,
        )
        logger.info(
            "Connected to Redis Sentinel master=%s via %d sentinel nodes",
            master_name,
            len(sentinel_hosts),
        )
        return client
    except ImportError:
        logger.warning("redis.asyncio.sentinel not available — falling back to direct Redis connection")
        import redis.asyncio as aioredis

        fallback_url = (
            f"redis://:{password}@{hosts_str.split(',')[0]}/0" if password else f"redis://{hosts_str.split(',')[0]}/0"
        )
        return aioredis.from_url(
            fallback_url,
            decode_responses=decode_responses,
            socket_connect_timeout=socket_connect_timeout,
            socket_timeout=socket_timeout,
        )


def create_sync_redis_from_url(
    url: str,
    socket_connect_timeout: float = 0.5,
    decode_responses: bool = True,
    **kwargs,
):
    """Create a synchronous Redis client from a URL, supporting Sentinel URLs."""
    master_name, hosts_str, password = parse_sentinel_url(url)

    if master_name and hosts_str:
        return _create_sync_sentinel_client(
            master_name, hosts_str, password, socket_connect_timeout, decode_responses, **kwargs
        )

    import redis as _sync_redis

    return _sync_redis.from_url(
        url,
        decode_responses=decode_responses,
        socket_connect_timeout=socket_connect_timeout,
        **kwargs,
    )


def _create_sync_sentinel_client(
    master_name: str,
    hosts_str: str,
    password: str | None,
    socket_connect_timeout: float,
    decode_responses: bool,
    **kwargs,
):
    """Create a sync Redis client connected through Sentinel for HA failover."""
    try:
        import redis.sentinel as sentinel_module

        sentinel_hosts = []
        for entry in hosts_str.split(","):
            entry = entry.strip()
            if ":" in entry:
                host, port = entry.rsplit(":", 1)
                sentinel_hosts.append((host, int(port)))
            else:
                sentinel_hosts.append((entry, 26379))

        sentinel = sentinel_module.Sentinel(
            sentinel_hosts,
            socket_connect_timeout=socket_connect_timeout,
            password=password,
        )

        client = sentinel.master_for(
            master_name,
            decode_responses=decode_responses,
            password=password,
            **kwargs,
        )
        return client
    except ImportError:
        logger.warning("redis.sentinel not available — falling back to direct Redis connection")
        import redis as _sync_redis

        last_err = None
        host_entries = hosts_str.split(",")
        for entry in host_entries:
            entry = entry.strip()
            try:
                fallback_url = f"redis://:{password}@{entry}/0" if password else f"redis://{entry}/0"
                client = _sync_redis.from_url(
                    fallback_url,
                    decode_responses=decode_responses,
                    socket_connect_timeout=socket_connect_timeout,
                )
                client.ping()
                logger.info("Connected to Redis fallback host %s", entry)
                return client
            except Exception as e:
                last_err = e
                logger.warning("Failed to connect to Redis fallback host %s: %s", entry, e)
                continue
        raise last_err or ConnectionError("No Redis host reachable from sentinel fallback list") from None
