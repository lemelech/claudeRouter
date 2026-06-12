"""Async health probe loop and registry."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
import zlib
from datetime import datetime, timezone

import aiohttp

from .providers import Provider

log = logging.getLogger(__name__)

PROBE_TIMEOUT = aiohttp.ClientTimeout(total=3)
# A real probe makes an actual completion call, which can take a few seconds.
DEEP_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=30)


class HealthRegistry:
    def __init__(self) -> None:
        self._ok: dict[str, bool] = {}
        self._last_check: dict[str, datetime] = {}
        self._last_error: dict[str, str | None] = {}
        # Deep ("real") probe results, keyed by provider name.
        self._deep: dict[str, dict] = {}

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

    def deep_status(self, name: str) -> dict:
        """Last deep-probe outcome. status: unknown|ok|fail|n/a."""
        return self._deep.get(name, {
            "status": "unknown", "checked_at": None, "error": None, "latency_ms": None,
        })

    def record_deep(self, name: str, status: str, error: str | None = None,
                    latency_ms: float | None = None) -> None:
        self._deep[name] = {
            "status": status,
            "checked_at": datetime.now(timezone.utc),
            "error": error,
            "latency_ms": latency_ms,
        }


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


def _short_error(raw: bytes, limit: int = 160) -> str:
    """Best-effort short, human-readable error from a (possibly gzip'd) body."""
    body = raw
    if raw[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(raw)
        except (gzip.BadGzipFile, zlib.error, EOFError):
            body = b""
    text = body.decode("utf-8", errors="replace").strip()
    return text if len(text) <= limit else text[:limit] + "…"


async def deep_probe(provider: Provider, session: aiohttp.ClientSession,
                     registry: HealthRegistry) -> None:
    """Real health check: send a minimal completion ("hi") and confirm a 200.

    Exercises auth + model translation + the /v1/messages path — unlike the cheap
    GET reachability probe. Skips passthrough providers (the proxy holds no
    credentials of its own for them). Never raises.
    """
    if provider.auth_style == "passthrough":
        registry.record_deep(provider.name, "n/a",
                             "passthrough auth — proxy holds no credentials to probe with")
        return
    model = provider.probe_model()
    if not model:
        registry.record_deep(provider.name, "n/a", "no model configured")
        return

    body = {
        **provider.extra_body,
        "model": provider.translate_model(model),
        "max_tokens": 8,
        "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    }
    headers = provider.apply_auth({})
    headers["Content-Type"] = "application/json"
    url = provider.base_url + "/v1/messages"

    t0 = time.monotonic()
    try:
        async with session.post(url, data=json.dumps(body).encode(), headers=headers,
                                timeout=DEEP_PROBE_TIMEOUT) as resp:
            raw = await resp.read()
            latency = (time.monotonic() - t0) * 1000
            if resp.status == 200:
                registry.record_deep(provider.name, "ok", None, latency)
            else:
                registry.record_deep(provider.name, "fail",
                                     f"HTTP {resp.status}: {_short_error(raw)}", latency)
    except Exception as e:
        registry.record_deep(provider.name, "fail", str(e),
                             (time.monotonic() - t0) * 1000)
        log.debug("Deep probe failed for %s: %s", provider.name, e)


async def deep_probe_all(providers: list[Provider], session: aiohttp.ClientSession,
                         registry: HealthRegistry) -> None:
    await asyncio.gather(*[deep_probe(p, session, registry) for p in providers],
                         return_exceptions=True)


async def probe_loop(providers: list[Provider], session: aiohttp.ClientSession,
                     registry: HealthRegistry, interval: int) -> None:
    while True:
        await asyncio.gather(*[probe(p, session, registry) for p in providers],
                             return_exceptions=True)
        await asyncio.sleep(interval)
