"""aiohttp application factory, routes, and lifecycle."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiohttp
from aiohttp import web

from . import router as rt
from .config import Config
from .health import HealthRegistry, probe_loop
from .providers import Provider, from_configs
from .sessions import SessionResolver
from .traffic_log import TrafficLog

log = logging.getLogger(__name__)

_DASHBOARD_PATH = Path(__file__).parent / "static" / "dashboard.html"


async def _handle_control_use(request: web.Request) -> web.Response:
    providers: list[Provider] = request.app["providers"]
    name = request.match_info["name"]
    valid = {p.name for p in providers} | {"auto"}
    try:
        rt.set_mode(name, valid)
    except ValueError as e:
        return web.Response(status=400, content_type="application/json",
                            body=json.dumps({"error": str(e)}))
    log.info("Provider mode set to %r", name)
    return web.Response(content_type="application/json",
                        body=json.dumps({"mode": rt.get_mode()}))


async def _handle_control_status(request: web.Request) -> web.Response:
    providers: list[Provider] = request.app["providers"]
    registry: HealthRegistry = request.app["health_registry"]
    payload = {
        "mode": rt.get_mode(),
        "providers": [
            {
                "name": p.name,
                "priority": p.priority,
                "base_url": p.base_url,
                **registry.status(p.name),
                "last_check": (
                    registry.status(p.name)["last_check"].isoformat()
                    if registry.status(p.name)["last_check"] else None
                ),
            }
            for p in sorted(providers, key=lambda x: x.priority)
        ],
    }
    return web.Response(content_type="application/json",
                        body=json.dumps(payload))


async def _handle_control_traffic(request: web.Request) -> web.Response:
    traffic_log: TrafficLog = request.app["traffic_log"]
    providers: list[Provider] = request.app["providers"]
    registry: HealthRegistry = request.app["health_registry"]

    entries = traffic_log.recent()
    effective_provider = None
    if entries:
        p = rt.pick_provider(entries[0].requested_model, providers, registry, tried=set())
        effective_provider = p.name if p else None

    payload = {
        "mode": rt.get_mode(),
        "effective_provider": effective_provider,
        "entries": [e.to_dict() for e in entries],
    }
    return web.Response(content_type="application/json", body=json.dumps(payload))


async def _handle_health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _handle_dashboard(_request: web.Request) -> web.Response:
    return web.Response(text=_DASHBOARD_PATH.read_text(), content_type="text/html")


async def _on_startup(app: web.Application) -> None:
    cfg: Config = app["config"]
    providers: list[Provider] = app["providers"]
    registry: HealthRegistry = app["health_registry"]

    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=100),
        timeout=aiohttp.ClientTimeout(
            connect=5,
            sock_read=None,   # SSE streams — no read timeout
            total=None,
        ),
        auto_decompress=False,  # pass compressed bytes through as-is; don't mangle Content-Encoding
    )
    app["client_session"] = session

    task = asyncio.create_task(
        probe_loop(providers, session, registry, cfg.server.health_interval_secs)
    )
    app["probe_task"] = task

    traffic_log_task = asyncio.create_task(app["traffic_log"].run())
    app["traffic_log_task"] = traffic_log_task

    log.info("claudeRouter listening on 127.0.0.1:%d", cfg.server.port)


async def _on_cleanup(app: web.Application) -> None:
    app["probe_task"].cancel()
    try:
        await app["probe_task"]
    except asyncio.CancelledError:
        pass

    app["traffic_log_task"].cancel()
    try:
        await app["traffic_log_task"]
    except asyncio.CancelledError:
        pass

    await app["client_session"].close()


def create_app(cfg: Config) -> web.Application:
    providers = from_configs(cfg.providers)
    registry = HealthRegistry()

    traffic_log_path = (
        Path(cfg.server.traffic_log_path).expanduser()
        if cfg.server.traffic_log_path
        else None
    )

    app = web.Application()
    app["config"] = cfg
    app["providers"] = providers
    app["health_registry"] = registry
    app["traffic_log"] = TrafficLog(
        ring_size=cfg.server.traffic_log_ring_size,
        queue_size=cfg.server.traffic_log_queue_size,
        log_path=traffic_log_path,
    )
    app["session_resolver"] = SessionResolver()

    # Anthropic-compatible passthrough
    app.router.add_route("*", "/v1/{tail:.*}", rt.handle_proxy)

    # Control endpoints
    app.router.add_post("/control/use/{name}", _handle_control_use)
    app.router.add_get("/control/status", _handle_control_status)
    app.router.add_get("/control/health", _handle_health)
    app.router.add_get("/control/traffic", _handle_control_traffic)

    # Dashboard
    app.router.add_get("/dashboard", _handle_dashboard)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app
