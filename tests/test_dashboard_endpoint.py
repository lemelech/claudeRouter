"""Tests for /control/traffic, /dashboard, and traffic-log task lifecycle wiring."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from clauderouter.config import Config, ProviderConfig, ServerConfig
from clauderouter.server import create_app
from clauderouter.sessions import SessionInfo
from clauderouter.traffic_log import LogEntry


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(_poll(), timeout=timeout)


def _make_config(tmp_path) -> Config:
    server = ServerConfig(
        port=4891,
        traffic_log_path=str(tmp_path / "requests.jsonl"),
        traffic_log_ring_size=10,
        traffic_log_queue_size=10,
    )
    providers = [
        ProviderConfig(
            name="anthropic",
            priority=1,
            base_url="https://api.anthropic.com",
            auth_style="none",
            models=["claude-sonnet-4-6"],
            native_thinking=True,
        ),
    ]
    return Config(server=server, providers=providers)


def _make_entry(i: int) -> LogEntry:
    return LogEntry(
        timestamp=f"2026-06-12T14:32:0{i}.123Z",
        session=SessionInfo(pid=1000 + i, cwd=f"/home/elimel/project{i}", label=f"{1000 + i} (~/project{i})"),
        provider="anthropic",
        mode="auto",
        requested_model="claude-sonnet-4-6",
        translated_model="claude-sonnet-4-6",
        tried=[],
        request_bytes=100 + i,
        response_bytes=200 + i,
        response_content_type="application/json",
        status=200,
        error_summary=None,
        usage={
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
        duration_ms=123.4,
    )


# ── GET /control/traffic ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_control_traffic_empty(tmp_path) -> None:
    app = create_app(_make_config(tmp_path))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/control/traffic")
        assert resp.status == 200
        body = await resp.json()
        assert body["entries"] == []
        assert body["effective_provider"] is None
        from clauderouter import router as rt
        assert body["mode"] == rt.get_mode()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_control_traffic_returns_emitted_entries(tmp_path) -> None:
    app = create_app(_make_config(tmp_path))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        app["health_registry"].record("anthropic", True)

        traffic_log = app["traffic_log"]
        entries = [_make_entry(0), _make_entry(1)]
        for entry in entries:
            traffic_log.emit(entry)

        await _wait_until(lambda: len(traffic_log.recent()) == 2)

        resp = await client.get("/control/traffic")
        assert resp.status == 200
        body = await resp.json()

        assert len(body["entries"]) == 2
        # most-recent-first
        assert body["entries"][0]["request_bytes"] == 101
        assert body["entries"][1]["request_bytes"] == 100
        assert body["entries"][0] == entries[1].to_dict()
        assert body["entries"][1] == entries[0].to_dict()

        assert body["effective_provider"] == "anthropic"
    finally:
        await client.close()


# ── GET /dashboard ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dashboard_serves_static_html(tmp_path) -> None:
    app = create_app(_make_config(tmp_path))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/dashboard")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        body = await resp.text()
        assert "/control/traffic" in body
    finally:
        await client.close()


# ── startup/cleanup task lifecycle ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_traffic_log_task_starts_and_cancels_cleanly(tmp_path) -> None:
    app = create_app(_make_config(tmp_path))
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        task = app["traffic_log_task"]
        assert isinstance(task, asyncio.Task)
        assert not task.done()
    finally:
        await client.close()

    assert task.cancelled() or task.done()
