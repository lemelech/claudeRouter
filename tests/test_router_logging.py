"""Tests for traffic-log instrumentation in handle_proxy (ticket 04)."""

from __future__ import annotations

import asyncio
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
        await asyncio.Event().wait()


class _RaisingTrafficLog:
    def emit(self, entry) -> None:
        raise Exception("boom")

    async def run(self) -> None:
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


# ── Successful non-streaming (stream:false) response ────────────────────────

@pytest.mark.asyncio
async def test_non_streaming_success_logs_usage_and_bytes(tmp_path) -> None:
    body = json.dumps({
        "id": "msg_1",
        "type": "message",
        "model": "claude-sonnet-4-6",
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 340,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 0,
        },
    }).encode()

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
            assert received == body
        finally:
            await client.close()
    finally:
        await upstream.close()

    assert len(traffic_log.entries) == 1
    entry = traffic_log.entries[0]
    assert entry.status == 200
    assert entry.provider == "anthropic"
    assert entry.response_bytes == len(body)
    assert entry.usage == {
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_read_input_tokens": 800,
        "cache_creation_input_tokens": 0,
    }
    assert entry.session == FIXED_SESSION


# ── Successful SSE response from a native-thinking provider (zero-copy) ─────

_SSE_NATIVE_BODY = (
    b'event: message_start\n'
    b'data: {"type": "message_start", "message": {"id": "msg_1", "usage": '
    b'{"input_tokens": 1200, "output_tokens": 0, "cache_read_input_tokens": 800, '
    b'"cache_creation_input_tokens": 0}}}\n\n'
    b'event: content_block_start\n'
    b'data: {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}\n\n'
    b'event: content_block_stop\n'
    b'data: {"type": "content_block_stop", "index": 0}\n\n'
    b'event: message_delta\n'
    b'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 12}}\n\n'
    b'event: message_stop\n'
    b'data: {"type": "message_stop"}\n\n'
)


@pytest.mark.asyncio
async def test_sse_native_thinking_zero_copy_byte_identical_and_usage(tmp_path) -> None:
    async def upstream_handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(_SSE_NATIVE_BODY)
        await resp.write_eof()
        return resp

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
                data=json.dumps({"model": "claude-sonnet-4-6", "stream": True, "messages": []}),
            )
            assert resp.status == 200
            received = await resp.read()
            assert received == _SSE_NATIVE_BODY
        finally:
            await client.close()
    finally:
        await upstream.close()

    assert len(traffic_log.entries) == 1
    entry = traffic_log.entries[0]
    assert entry.status == 200
    assert entry.response_bytes == len(_SSE_NATIVE_BODY)
    assert entry.usage["input_tokens"] == 1200
    assert entry.usage["output_tokens"] == 12
    assert entry.usage["cache_read_input_tokens"] == 800
    assert entry.usage["cache_creation_input_tokens"] == 0


# ── Successful SSE response from a non-native-thinking provider (sterilize) ─

_SSE_THINKING_BODY = (
    b'event: message_start\n'
    b'data: {"type": "message_start", "message": {"id": "msg_1", "usage": '
    b'{"input_tokens": 500, "output_tokens": 0, "cache_read_input_tokens": 0, '
    b'"cache_creation_input_tokens": 0}}}\n\n'
    b'event: content_block_start\n'
    b'data: {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}\n\n'
    b'event: content_block_delta\n'
    b'data: {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Hmm"}}\n\n'
    b'event: content_block_stop\n'
    b'data: {"type": "content_block_stop", "index": 0}\n\n'
    b'event: message_delta\n'
    b'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}}\n\n'
    b'event: message_stop\n'
    b'data: {"type": "message_stop"}\n\n'
)


@pytest.mark.asyncio
async def test_sse_sterilization_unchanged_and_usage_extracted(tmp_path) -> None:
    async def upstream_handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(_SSE_THINKING_BODY)
        await resp.write_eof()
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
                data=json.dumps({"model": "claude-sonnet-4-6", "stream": True, "messages": []}),
            )
            assert resp.status == 200
            received = await resp.read()
        finally:
            await client.close()
    finally:
        await upstream.close()

    # Sterilization rewrites the thinking content_block_start/delta/stop events
    # into text events wrapping the content in <thinking>...</thinking>.
    assert b'"type": "thinking"' not in received
    assert b"<thinking>" in received
    assert b"</thinking>" in received

    assert len(traffic_log.entries) == 1
    entry = traffic_log.entries[0]
    assert entry.status == 200
    assert entry.usage["input_tokens"] == 500
    assert entry.usage["output_tokens"] == 7


# ── 400 error response ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_400_error_logs_status_and_truncated_summary(tmp_path) -> None:
    error_body = json.dumps({
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "x" * 300},
    }).encode()

    async def upstream_handler(request: web.Request) -> web.Response:
        return web.Response(status=400, content_type="application/json", body=error_body)

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
            assert resp.status == 400
        finally:
            await client.close()
    finally:
        await upstream.close()

    assert len(traffic_log.entries) == 1
    entry = traffic_log.entries[0]
    assert entry.status == 400
    assert entry.usage is None
    assert entry.error_summary is not None
    assert entry.error_summary.endswith("…")
    assert len(entry.error_summary) == 201  # 200 chars + "…"


# ── 503 "no provider available" ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_503_no_provider_available(tmp_path) -> None:
    traffic_log = _FakeTrafficLog()
    app, client = await _start_proxy(
        tmp_path,
        [_provider("anthropic", "https://api.anthropic.com")],
        traffic_log=traffic_log,
    )
    # Mark the only provider unhealthy so pick_provider returns None.
    app["health_registry"].record("anthropic", False, "down")
    try:
        resp = await client.post(
            "/v1/messages",
            data=json.dumps({"model": "claude-sonnet-4-6", "stream": False, "messages": []}),
        )
        assert resp.status == 503
    finally:
        await client.close()

    assert len(traffic_log.entries) == 1
    entry = traffic_log.entries[0]
    assert entry.provider is None
    assert entry.status == 503
    assert entry.translated_model is None
    assert entry.usage is None


# ── Fallback: first provider 429s, second succeeds ──────────────────────────

@pytest.mark.asyncio
async def test_fallback_records_tried_providers(tmp_path) -> None:
    body = json.dumps({
        "id": "msg_1",
        "type": "message",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }).encode()

    async def rate_limited_handler(request: web.Request) -> web.Response:
        return web.Response(status=429, content_type="application/json",
                             body=json.dumps({"error": {"type": "rate_limit_error"}}))

    async def success_handler(request: web.Request) -> web.Response:
        return web.Response(status=200, content_type="application/json", body=body)

    upstream_a = await _start_upstream(rate_limited_handler)
    upstream_b = await _start_upstream(success_handler)
    traffic_log = _FakeTrafficLog()
    try:
        app, client = await _start_proxy(
            tmp_path,
            [
                _provider("first", str(upstream_a.make_url("")), priority=1),
                _provider("second", str(upstream_b.make_url("")), priority=2),
            ],
            traffic_log=traffic_log,
        )
        try:
            resp = await client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-sonnet-4-6", "stream": False, "messages": []}),
            )
            assert resp.status == 200
        finally:
            await client.close()
    finally:
        await upstream_a.close()
        await upstream_b.close()

    assert len(traffic_log.entries) == 1
    entry = traffic_log.entries[0]
    assert entry.provider == "second"
    assert entry.tried == ["first"]


# ── Logging never breaks the response ────────────────────────────────────────

@pytest.mark.asyncio
async def test_emit_exception_does_not_break_response(tmp_path) -> None:
    body = json.dumps({"id": "msg_1", "type": "message", "usage": {}}).encode()

    async def upstream_handler(request: web.Request) -> web.Response:
        return web.Response(status=200, content_type="application/json", body=body)

    upstream = await _start_upstream(upstream_handler)
    try:
        app, client = await _start_proxy(
            tmp_path,
            [_provider("anthropic", str(upstream.make_url("")), native_thinking=True)],
            traffic_log=_RaisingTrafficLog(),
        )
        try:
            resp = await client.post(
                "/v1/messages",
                data=json.dumps({"model": "claude-sonnet-4-6", "stream": False, "messages": []}),
            )
            assert resp.status == 200
            received = await resp.read()
            assert received == body
        finally:
            await client.close()
    finally:
        await upstream.close()


# ── request_bytes reflects the original inbound request ──────────────────────

@pytest.mark.asyncio
async def test_request_bytes_matches_original_raw_body(tmp_path) -> None:
    body = json.dumps({"id": "msg_1", "type": "message", "usage": {}}).encode()

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
            raw_body = json.dumps({
                "model": "claude-sonnet-4-6", "stream": False, "messages": [],
                "max_tokens": 1024,
            })
            resp = await client.post("/v1/messages", data=raw_body)
            assert resp.status == 200
        finally:
            await client.close()
    finally:
        await upstream.close()

    assert len(traffic_log.entries) == 1
    entry = traffic_log.entries[0]
    assert entry.request_bytes == len(raw_body.encode())
