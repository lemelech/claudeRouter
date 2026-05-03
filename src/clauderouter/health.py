"""Async health probe loop and registry."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

from .providers import Provider

log = logging.getLogger(__name__)

PROBE_TIMEOUT = aiohttp.ClientTimeout(total=3)


class HealthRegistry:
    def __init__(self) -> None:
        self._ok: dict[str, bool] = {}
        self._last_check: dict[str, datetime] = {}
        self._last_error: dict[str, str | None] = {}

    def is_healthy(self, name: str) -> bool:
        return self._ok.get(name, False)

    def status(self, name: str) -> dict:
        return {
            "healthy": self._ok.get(name, False),
            "last_check": self._last_check.get(name, None),
            "last_error": self._last_error.get(name),
        }

    def record(self, name: str, ok: bool, error: str | None = None) -> None:
        self._ok[name] = ok
        self._last_check[name] = datetime.now(timezone.utc)
        self._last_error[name] = error


async def probe(provider: Provider, session: aiohttp.ClientSession,
                registry: HealthRegistry) -> None:
    url = provider.base_url + provider.health_path
    try:
        async with session.get(url, timeout=PROBE_TIMEOUT) as resp:
            ok = resp.status < 500
            registry.record(provider.name, ok,
                            None if ok else f"HTTP {resp.status}")
    except Exception as e:
        registry.record(provider.name, False, str(e))
        log.debug("Health probe failed for %s: %s", provider.name, e)


async def probe_loop(providers: list[Provider], session: aiohttp.ClientSession,
                     registry: HealthRegistry, interval: int) -> None:
    while True:
        await asyncio.gather(*[probe(p, session, registry) for p in providers],
                             return_exceptions=True)
        await asyncio.sleep(interval)
