"""Integration tests for non-SSE (application/json) thinking-block sterilization.

Mirrors the SSE thinking-block sterilization in test_dashboard_endpoint.py, but for
non-streaming `stream: false` Messages responses returned with Content-Type:
application/json. See router._sterilize_thinking_in_response and the non-SSE branch
of router._stream.
"""

from __future__ import annotations

import gzip
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from clauderouter.config import Config, ProviderConfig, ServerConfig
from clauderouter.server import create_app
from clauderouter.sessions import SessionInfo


FIXED_SESSION = SessionInfo(pid=4242, cwd="/home/elimel/projectX", label="4242 (~/projectX)")


class _FakeSessionResolver:
    def resolve(self, *args, **kwargs) -> SessionInfo:
        return FIXED_SESSION


class _FakeTrafficLog:
    def __init__(self) -> None:
        self.entries: list = []

    def emit(self, entry) -> None:
        self.entries.append(entry)

    async def run(self) -> None:
        import asyncio
        await asyncio.Event().wait()


def _make_config(tmp_path, providers: list[ProviderConfig]) -> Config:
    server = ServerConfig(
        port=4891,
        traffic_log_path=str(tmp_path / "requests.jsonl"),
        traffic_log_ring_size=10,
        traffic_log_queue_size=10,
    )
    return Config(server=server, providers=providers)


async def _start_proxy(tmp_path, providers: list[ProviderConfig], traffic_log=None):
    app = create_app(_make_config(tmp_path, providers))
    if traffic_log is not None:
        app["traffic_log"] = traffic_log
    app["session_resolver"] = _FakeSessionResolver()
    for p in app["providers"]:
        app["health_registry"].record(p.name, True)

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return app, client


async def _start_upstream(handler) -> TestServer:
    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/messages", handler)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()
    return upstream_server


def _provider(name: str, base_url: str, *, priority: int = 1,
              native_thinking: bool = False) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        priority=priority,
        base_url=base_url,
        auth_style="none",
        models=["claude-sonnet-4-6"],
        native_thinking=native_thinking,
    )


_RESPONSE_WITH_THINKING = {
    "id": "msg_1",
    "type": "message",
    "model": "claude-sonnet-4-6",
    "content": [
        {"type": "thinking", "thinking": "let me think about this carefully"},
        {"type": "text", "text": "Here is the answer."},
    ],
    "usage": {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    },
}


# ── Non-native provider: JSON thinking block is sterilized to text ──────────


@pytest.mark.asyncio
async def test_non_native_json_response_sterilizes_thinking_block(tmp_path) -> None:
    body = json.dumps(_RESPONSE_WITH_THINKING).encode()

    async def upstream_handler(request: web.Request) -> web.Response:
        return web.Response(status=200, content_type="application/json", body=body)

    upstream = await _start_upstream(upstream_handler)
    traffic_log = _FakeTrafficLog()
    try:
        app, client = await _start_proxy(
            tmp_path,
            [_provider("ollama", str(upstream.make_url("")), native_thinking=False)],
            traffic_log=traffic_log,
        )
        try:
            resp = await client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-sonnet-4-6", "stream": False, "messages": []}),
            )
            assert resp.status == 200
            received = json.loads(await resp.read())
        finally:
            await client.close()
    finally:
        await upstream.close()

    # The thinking block is converted to a text block wrapped in <thinking>...</thinking>.
    assert received["content"][0] == {
        "type": "text",
        "text": "<thinking>\nlet me think about this carefully\n</thinking>",
    }
    # The sibling text block is untouched.
    assert received["content"][1] == {"type": "text", "text": "Here is the answer."}
    # No raw "thinking" block type reaches the client.
    assert all(block["type"] != "thinking" for block in received["content"])

    assert len(traffic_log.entries) == 1
    entry = traffic_log.entries[0]
    assert entry.status == 200
    assert entry.usage["input_tokens"] == 100
    assert entry.usage["output_tokens"] == 50


# ── Native provider: JSON thinking block passes through unchanged ───────────


@pytest.mark.asyncio
async def test_native_provider_json_response_passthrough_unchanged(tmp_path) -> None:
    body = json.dumps(_RESPONSE_WITH_THINKING).encode()

    async def upstream_handler(request: web.Request) -> web.Response:
        return web.Response(status=200, content_type="application/json", body=body)

    upstream = await _start_upstream(upstream_handler)
    traffic_log = _FakeTrafficLog()
    try:
        app, client = await _start_proxy(
            tmp_path,
            [_provider("anthropic", str(upstream.make_url("")), native_thinking=True)],
            traffic_log=traffic_log,
        )
        try:
            resp = await client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-sonnet-4-6", "stream": False, "messages": []}),
            )
            assert resp.status == 200
            received = await resp.read()
        finally:
            await client.close()
    finally:
        await upstream.close()

    # Byte-identical passthrough for native providers.
    assert received == body
    parsed = json.loads(received)
    assert parsed["content"][0]["type"] == "thinking"

    assert len(traffic_log.entries) == 1
    entry = traffic_log.entries[0]
    assert entry.status == 200
    assert entry.response_bytes == len(body)


# ── Non-native provider with no thinking blocks: content unchanged ──────────


@pytest.mark.asyncio
async def test_non_native_json_response_without_thinking_unchanged(tmp_path) -> None:
    response_body = {
        "id": "msg_2",
        "type": "message",
        "content": [{"type": "text", "text": "plain answer"}],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }
    body = json.dumps(response_body).encode()

    async def upstream_handler(request: web.Request) -> web.Response:
        return web.Response(status=200, content_type="application/json", body=body)

    upstream = await _start_upstream(upstream_handler)
    traffic_log = _FakeTrafficLog()
    try:
        app, client = await _start_proxy(
            tmp_path,
            [_provider("ollama", str(upstream.make_url("")), native_thinking=False)],
            traffic_log=traffic_log,
        )
        try:
            resp = await client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-sonnet-4-6", "stream": False, "messages": []}),
            )
            assert resp.status == 200
            received = json.loads(await resp.read())
        finally:
            await client.close()
    finally:
        await upstream.close()

    assert received["content"] == [{"type": "text", "text": "plain answer"}]


# ── Non-native provider, gzip'd JSON response: sterilized + re-compressed ───


@pytest.mark.asyncio
async def test_non_native_gzip_json_response_sterilized_and_recompressed(tmp_path) -> None:
    body = json.dumps(_RESPONSE_WITH_THINKING).encode()
    gzipped = gzip.compress(body)

    async def upstream_handler(request: web.Request) -> web.Response:
        resp = web.Response(status=200, body=gzipped)
        resp.headers["Content-Type"] = "application/json"
        resp.headers["Content-Encoding"] = "gzip"
        return resp

    upstream = await _start_upstream(upstream_handler)
    traffic_log = _FakeTrafficLog()
    try:
        app, client = await _start_proxy(
            tmp_path,
            [_provider("ollama", str(upstream.make_url("")), native_thinking=False)],
            traffic_log=traffic_log,
        )
        try:
            resp = await client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-sonnet-4-6", "stream": False, "messages": []}),
            )
            assert resp.status == 200
            # aiohttp's test client auto-decompresses based on Content-Encoding.
            received = json.loads(await resp.read())
        finally:
            await client.close()
    finally:
        await upstream.close()

    assert received["content"][0] == {
        "type": "text",
        "text": "<thinking>\nlet me think about this carefully\n</thinking>",
    }
    assert received["content"][1] == {"type": "text", "text": "Here is the answer."}

    assert len(traffic_log.entries) == 1
    assert traffic_log.entries[0].usage["input_tokens"] == 100


# ── Non-native provider, non-Messages JSON: passed through unchanged ────────


@pytest.mark.asyncio
async def test_non_native_json_error_response_unchanged(tmp_path) -> None:
    # Non-Messages JSON (error body) from a non-native provider should pass through
    # byte-identical — no content: [] injected, no re-serialization.
    #
    # Status 404 (not 400) so this doesn't hit handle_proxy's special 400-error
    # branch — it goes through the normal _stream non-SSE path, which is what
    # _sterilize_thinking_in_response's identity short-circuit guards.
    body = json.dumps({"error": {"type": "invalid_request_error", "message": "model not found"}}).encode()

    async def upstream_handler(request: web.Request) -> web.Response:
        return web.Response(status=404, content_type="application/json", body=body)

    upstream = await _start_upstream(upstream_handler)
    traffic_log = _FakeTrafficLog()
    try:
        app, client = await _start_proxy(
            tmp_path,
            [_provider("ollama", str(upstream.make_url("")), native_thinking=False)],
            traffic_log=traffic_log,
        )
        try:
            resp = await client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-sonnet-4-6", "stream": False, "messages": []}),
            )
            assert resp.status == 404
            received = await resp.read()
        finally:
            await client.close()
    finally:
        await upstream.close()

    # Byte-identical passthrough for non-Messages JSON.
    assert received == body
