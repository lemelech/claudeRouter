"""Tests for the deep ('real') health probe."""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from clauderouter.config import ProviderConfig
from clauderouter.health import HealthRegistry, deep_probe
from clauderouter.providers import Provider


async def _upstream(handler) -> TestServer:
    app = web.Application()
    app.router.add_post("/v1/messages", handler)
    srv = TestServer(app)
    await srv.start_server()
    return srv


def _provider(base_url: str, *, auth_style: str = "none", api_key: str | None = None,
              models=("claude-sonnet-4-6",), model_map=None) -> Provider:
    return Provider(ProviderConfig(
        name="p", priority=1, base_url=base_url, auth_style=auth_style,
        models=list(models), model_map=model_map or {}, api_key=api_key,
    ))


def test_deep_status_default_unknown() -> None:
    reg = HealthRegistry()
    assert reg.deep_status("nope")["status"] == "unknown"


@pytest.mark.asyncio
async def test_deep_probe_ok() -> None:
    seen = {}

    async def handler(request: web.Request) -> web.Response:
        seen["body"] = await request.json()
        return web.json_response({"id": "msg", "usage": {"input_tokens": 1, "output_tokens": 1}})

    srv = await _upstream(handler)
    reg = HealthRegistry()
    async with aiohttp.ClientSession(auto_decompress=False) as session:
        try:
            await deep_probe(_provider(str(srv.make_url(""))), session, reg)
        finally:
            await srv.close()

    # Sent a minimal, non-agentic completion.
    assert seen["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert seen["body"]["max_tokens"] == 8
    assert seen["body"]["stream"] is False

    st = reg.deep_status("p")
    assert st["status"] == "ok"
    assert st["error"] is None
    assert st["latency_ms"] is not None
    assert st["checked_at"] is not None


@pytest.mark.asyncio
async def test_deep_probe_translates_model() -> None:
    seen = {}

    async def handler(request: web.Request) -> web.Response:
        seen["body"] = await request.json()
        return web.json_response({"id": "msg"})

    srv = await _upstream(handler)
    reg = HealthRegistry()
    async with aiohttp.ClientSession(auto_decompress=False) as session:
        try:
            await deep_probe(
                _provider(str(srv.make_url("")), model_map={"claude-sonnet-4-6": "z-ai/glm-5"}),
                session, reg)
        finally:
            await srv.close()
    assert seen["body"]["model"] == "z-ai/glm-5"
    assert reg.deep_status("p")["status"] == "ok"


@pytest.mark.asyncio
async def test_deep_probe_fail_non_200() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"error": {"message": "model not found"}}, status=400)

    srv = await _upstream(handler)
    reg = HealthRegistry()
    async with aiohttp.ClientSession(auto_decompress=False) as session:
        try:
            await deep_probe(_provider(str(srv.make_url(""))), session, reg)
        finally:
            await srv.close()
    st = reg.deep_status("p")
    assert st["status"] == "fail"
    assert "HTTP 400" in st["error"]
    assert "model not found" in st["error"]


@pytest.mark.asyncio
async def test_deep_probe_passthrough_is_na_without_calling() -> None:
    # Unroutable base_url: if it tried to connect this would error, not return n/a.
    reg = HealthRegistry()
    async with aiohttp.ClientSession() as session:
        await deep_probe(_provider("http://127.0.0.1:1", auth_style="passthrough"), session, reg)
    st = reg.deep_status("p")
    assert st["status"] == "n/a"
    assert "passthrough" in st["error"]


@pytest.mark.asyncio
async def test_deep_probe_no_model_is_na() -> None:
    reg = HealthRegistry()
    async with aiohttp.ClientSession() as session:
        await deep_probe(_provider("http://127.0.0.1:1", models=()), session, reg)
    assert reg.deep_status("p")["status"] == "n/a"


@pytest.mark.asyncio
async def test_deep_probe_connection_error_is_fail() -> None:
    reg = HealthRegistry()
    async with aiohttp.ClientSession() as session:
        await deep_probe(_provider("http://127.0.0.1:1", models=("claude-sonnet-4-6",)), session, reg)
    assert reg.deep_status("p")["status"] == "fail"
