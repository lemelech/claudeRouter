"""Provider selection, request forwarding, and fallback logic."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from .health import HealthRegistry
from .providers import Provider

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# Module-level forced mode; "auto" means chain order + health checks.
_mode: str = "auto"


def get_mode() -> str:
    return _mode


def set_mode(mode: str, valid_names: set[str]) -> None:
    global _mode
    if mode != "auto" and mode not in valid_names:
        raise ValueError(f"Unknown provider: {mode!r}")
    _mode = mode


def pick_provider(model: str, providers: list[Provider],
                  registry: HealthRegistry,
                  tried: set[str] | None = None) -> Provider | None:
    tried = tried or set()
    if _mode != "auto":
        for p in providers:
            if p.name == _mode and p.name not in tried:
                if p.supports_model(model) and registry.is_healthy(p.name):
                    return p
        return None

    for p in sorted(providers, key=lambda x: x.priority):
        if p.name in tried:
            continue
        if p.supports_model(model) and registry.is_healthy(p.name):
            return p
    return None


async def handle_proxy(request: web.Request) -> web.StreamResponse:
    providers: list[Provider] = request.app["providers"]
    registry: HealthRegistry = request.app["health_registry"]
    session: aiohttp.ClientSession = request.app["client_session"]

    raw_body = await request.read()
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        body = {}

    requested_model: str = body.get("model", "")
    tried: set[str] = set()

    while True:
        provider = pick_provider(requested_model, providers, registry, tried)
        if provider is None:
            tried_str = ", ".join(tried) if tried else "none"
            return web.Response(
                status=503,
                content_type="application/json",
                body=json.dumps({
                    "error": {
                        "type": "provider_unavailable",
                        "message": (
                            f"No healthy provider available for model {requested_model!r} "
                            f"(mode={_mode!r}, tried={tried_str})."
                        ),
                    }
                }),
            )

        upstream_url = provider.base_url + request.path_qs
        mutated = dict(body)
        mutated["model"] = provider.translate_model(requested_model)
        upstream_body = json.dumps(mutated).encode()

        headers = provider.apply_auth(dict(request.headers))
        headers.pop("Host", None)
        headers.pop("host", None)
        headers["Content-Length"] = str(len(upstream_body))

        log.info("→ %s %s via %s (model: %s → %s)",
                 request.method, request.path, provider.name,
                 requested_model, mutated["model"])

        try:
            upstream = await session.request(
                request.method,
                upstream_url,
                headers=headers,
                data=upstream_body,
                allow_redirects=False,
            )
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
            log.warning("Provider %s connection error: %s — trying next", provider.name, e)
            registry.record(provider.name, False, str(e))
            tried.add(provider.name)
            continue

        # Got a response — check for retryable status before streaming
        if upstream.status == 429:
            log.warning("Provider %s returned 429 — trying next", provider.name)
            registry.record(provider.name, False, "429 rate-limited")
            tried.add(provider.name)
            upstream.release()
            continue

        # Start streaming
        response = web.StreamResponse(
            status=upstream.status,
            headers={k: v for k, v in upstream.headers.items()
                     if k.lower() not in ("transfer-encoding", "content-length")},
        )
        await response.prepare(request)

        try:
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
        except (aiohttp.ClientError, ConnectionResetError) as e:
            log.error("Stream error from %s: %s", provider.name, e)
        finally:
            upstream.release()

        await response.write_eof()
        return response


# asyncio not imported at top — need it for TimeoutError catch
import asyncio  # noqa: E402
