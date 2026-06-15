import asyncio
import ipaddress
import logging
import os
import time

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_STRIPE_IPS_V4_URL = "https://stripe.com/files/ips/stripe_public_ips_v4.json"
_STRIPE_IPS_V6_URL = "https://stripe.com/files/ips/stripe_public_ips_v6.json"

_DEFAULT_STRIPE_IPS = [
    "13.248.128.0/24",
    "13.248.129.0/24",
    "13.248.130.0/24",
    "13.248.131.0/24",
    "13.248.132.0/24",
    "13.248.133.0/24",
    "13.248.134.0/24",
    "13.248.135.0/24",
    "18.155.128.0/24",
    "18.155.129.0/24",
    "18.155.130.0/24",
    "18.155.131.0/24",
    "18.155.132.0/24",
    "18.155.133.0/24",
    "18.155.134.0/24",
    "18.155.135.0/24",
    "54.187.174.0/24",
    "54.187.175.0/24",
    "54.187.176.0/24",
    "54.187.177.0/24",
    "54.187.178.0/24",
    "54.187.179.0/24",
]

_cached_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
_last_fetch: float = 0
_fetch_lock = asyncio.Lock()
_CACHE_TTL = 3600
_prev_ip_count = 0
_prev_ip_count_lock = asyncio.Lock()

# Load default IPs at import time to prevent cold-start fail-open
_load_defaults_done = False


def _load_default_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(c) for c in _DEFAULT_STRIPE_IPS]


def _ensure_defaults_loaded():
    global _cached_networks, _last_fetch, _load_defaults_done
    if _load_defaults_done:
        return
    _cached_networks = _load_default_networks()
    _last_fetch = time.time()
    _load_defaults_done = True
    logger.info("Loaded %d default Stripe IP networks as initial cache", len(_cached_networks))


_ensure_defaults_loaded()


async def fetch_stripe_ips() -> list[str]:
    cidrs = []
    for url in (_STRIPE_IPS_V4_URL, _STRIPE_IPS_V6_URL):
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                cidrs.extend(data.get("ips", data.get("networks", [])))
        except Exception as e:
            logger.warning("Failed to fetch Stripe IPs from %s: %s", url, e)
    if not cidrs:
        logger.warning("No Stripe IPs fetched, using defaults")
        cidrs = list(_DEFAULT_STRIPE_IPS)
    return cidrs


async def refresh_stripe_ips(force: bool = False) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    global _cached_networks, _last_fetch, _prev_ip_count
    now = time.time()
    if not force and _cached_networks and (now - _last_fetch < _CACHE_TTL):
        return _cached_networks
    async with _fetch_lock:
        if not force and _cached_networks and (now - _last_fetch < _CACHE_TTL):
            return _cached_networks
        cidrs = await fetch_stripe_ips()
        networks = []
        for cidr in cidrs:
            try:
                networks.append(ipaddress.ip_network(cidr))
            except ValueError:
                logger.warning("Invalid Stripe IP CIDR: %s", cidr)
        if networks:
            _cached_networks = networks
            _last_fetch = now
            logger.info("Refreshed Stripe IP ranges: %d networks", len(networks))
        async with _prev_ip_count_lock:
            prev = _prev_ip_count
            if prev and prev != len(networks):
                logger.critical(
                    "Stripe IP ranges CHANGED: was %d, now %d — webhook IP validation updated", prev, len(networks)
                )
            _prev_ip_count = len(networks)
        return _cached_networks


async def validate_stripe_ip(client_ip: str) -> bool:
    # Allow bypass in non-production only with explicit env var
    if os.getenv("STRIPE_WEBHOOK_IP_CHECK_DISABLED") == "1":
        settings = get_settings()
        if settings.debug:
            logger.warning(
                "Stripe IP check disabled via env var (STRIPE_WEBHOOK_IP_CHECK_DISABLED=1) — INSECURE: allowing all IPs"
            )
            return True
        else:
            logger.critical("STRIPE_WEBHOOK_IP_CHECK_DISABLED=1 ignored in production mode — IP check enforced")
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False

    # Check if we have cached networks (defaults always loaded at import time)
    networks = await refresh_stripe_ips()
    if networks and any(ip in net for net in networks):
        return True

    if networks:
        return False

    # Fail-open with warning: cache should never be empty with hardcoded defaults,
    # but if it is (e.g. startup race), log critical warning instead of rejecting.
    logger.critical(
        "Stripe IP validation failed for %s — no cached networks available, allowing (fail-open with warning)",
        client_ip,
    )
    if not _cached_networks:
        try:
            from app.monitoring.prometheus import increment_counter

            increment_counter("workticket_stripe_ip_cache_empty_warning", {})
        except Exception:
            logger.debug("Failed to increment Stripe IP cache empty warning metric")
            pass  # nosec B110
    return True


def get_cached_network_count() -> int:
    return len(_cached_networks)


def get_last_fetch_age() -> float:
    return time.time() - _last_fetch if _last_fetch else float("inf")
