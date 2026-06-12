"""Tests for provider readiness + deep-probe fields in /control/status and /control/probe."""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from clauderouter.config import Config, ProviderConfig, ServerConfig
from clauderouter.server import create_app


def _config(providers: list[ProviderConfig], *, deep_on_startup: bool = False) -> Config:
    return Config(
        server=ServerConfig(port=4891, traffic_log_path="", deep_probe_on_startup=deep_on_startup),
        providers=providers,
    )


async def _start(app) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _upstream(handler) -> TestServer:
    up = web.Application()
    up.router.add_post("/v1/messages", handler)
    srv = TestServer(up)
    await srv.start_server()
    return srv


@pytest.mark.asyncio
async def test_status_exposes_ready_and_authstyle() -> None:
    providers = [
        ProviderConfig(name="anthropic", priority=1, base_url="https://api.anthropic.com",
                       auth_style="passthrough", models=["claude-sonnet-4-6"], native_thinking=True),
        # bearer with no resolved key -> not ready
        ProviderConfig(name="openrouter", priority=2, base_url="https://openrouter.ai/api",
                       auth_style="bearer", models=["claude-sonnet-4-6"], api_key=None),
    ]
    client = await _start(create_app(_config(providers)))
    try:
        resp = await client.get("/control/status")
        assert resp.status == 200
        data = await resp.json()
        by_name = {p["name"]: p for p in data["providers"]}

        assert by_name["anthropic"]["ready"] is True
        assert by_name["anthropic"]["auth_style"] == "passthrough"
        assert by_name["openrouter"]["ready"] is False  # the masked-by-"healthy" case
        assert by_name["openrouter"]["auth_style"] == "bearer"
        # deep probe hasn't run (disabled on startup) -> unknown
        assert by_name["openrouter"]["deep"]["status"] == "unknown"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_control_probe_runs_and_updates_deep_status() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"id": "msg", "usage": {"input_tokens": 1, "output_tokens": 1}})

    upstream = await _upstream(handler)
    providers = [
        ProviderConfig(name="local", priority=1, base_url=str(upstream.make_url("")),
                       auth_style="none", models=["claude-sonnet-4-6"]),
    ]
    client = await _start(create_app(_config(providers)))
    try:
        # before: unknown
        before = await (await client.get("/control/status")).json()
        assert before["providers"][0]["deep"]["status"] == "unknown"

        resp = await client.post("/control/probe")
        assert resp.status == 200
        data = await resp.json()
        assert data["providers"][0]["deep"]["status"] == "ok"

        # status now reflects it too
        after = await (await client.get("/control/status")).json()
        assert after["providers"][0]["deep"]["status"] == "ok"
    finally:
        await client.close()
        await upstream.close()


@pytest.mark.asyncio
async def test_control_probe_unknown_provider_404() -> None:
    providers = [
        ProviderConfig(name="local", priority=1, base_url="http://127.0.0.1:1",
                       auth_style="none", models=["claude-sonnet-4-6"]),
    ]
    client = await _start(create_app(_config(providers)))
    try:
        resp = await client.post("/control/probe/nope")
        assert resp.status == 404
    finally:
        await client.close()
