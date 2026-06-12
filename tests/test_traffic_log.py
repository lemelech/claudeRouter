"""Tests for the in-memory ring buffer + JSONL persistence in traffic_log.py."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from clauderouter.sessions import SessionInfo, UNKNOWN_SESSION
from clauderouter.traffic_log import LogEntry, TrafficLog


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(_poll(), timeout=timeout)


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


# ── recent() ordering and ring eviction ───────────────────────────────────────

def test_recent_returns_most_recent_first_and_respects_ring_size() -> None:
    tl = TrafficLog(ring_size=3, queue_size=10, log_path=None)
    for i in range(5):
        tl._ring.append(_make_entry(i))

    recent = tl.recent()
    assert len(recent) == 3
    # Oldest two (0, 1) evicted; remaining most-recent-first: 4, 3, 2
    assert [e.request_bytes for e in recent] == [104, 103, 102]


def test_recent_with_n_limits_results() -> None:
    tl = TrafficLog(ring_size=10, queue_size=10, log_path=None)
    for i in range(5):
        tl._ring.append(_make_entry(i))

    recent = tl.recent(n=2)
    assert len(recent) == 2
    assert [e.request_bytes for e in recent] == [104, 103]


# ── emit() + run() ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_emit_and_run_populates_recent() -> None:
    tl = TrafficLog(ring_size=10, queue_size=10, log_path=None)
    task = asyncio.create_task(tl.run())
    try:
        entry = _make_entry(0)
        tl.emit(entry)
        await _wait_until(lambda: len(tl._ring) == 1)

        recent = tl.recent()
        assert len(recent) == 1
        assert recent[0] == entry
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ── JSONL persistence ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_writes_jsonl_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "requests.jsonl"
    tl = TrafficLog(ring_size=10, queue_size=10, log_path=log_path)
    task = asyncio.create_task(tl.run())
    try:
        entries = [_make_entry(i) for i in range(3)]
        for entry in entries:
            tl.emit(entry)
        await _wait_until(lambda: len(tl._ring) == 3)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    lines = log_path.read_text().splitlines()
    assert len(lines) == 3
    for line, entry in zip(lines, entries):
        parsed = json.loads(line)
        assert parsed == entry.to_dict()


def test_to_dict_round_trips_through_json() -> None:
    entry = _make_entry(0)
    d = entry.to_dict()
    round_tripped = json.loads(json.dumps(d))
    assert round_tripped == d
    assert round_tripped["session"] == {
        "pid": 1000,
        "cwd": "/home/elimel/project0",
        "label": "1000 (~/project0)",
    }


def test_log_path_none_creates_no_file_and_recent_works(tmp_path: Path) -> None:
    tl = TrafficLog(ring_size=10, queue_size=10, log_path=None)
    tl._ring.append(_make_entry(0))

    assert tl.recent() == [_make_entry(0)]
    assert list(tmp_path.iterdir()) == []


# ── emit() never raises / never blocks when full ──────────────────────────────

def test_emit_does_not_raise_when_queue_full() -> None:
    tl = TrafficLog(ring_size=10, queue_size=2, log_path=None)
    tl.emit(_make_entry(0))
    tl.emit(_make_entry(1))
    # Queue is now full; this third emit must not raise.
    tl.emit(_make_entry(2))

    assert tl._queue.full()


@pytest.mark.asyncio
async def test_emit_dropped_entry_does_not_break_ring_once_drained() -> None:
    tl = TrafficLog(ring_size=10, queue_size=2, log_path=None)
    tl.emit(_make_entry(0))
    tl.emit(_make_entry(1))
    tl.emit(_make_entry(2))  # dropped, queue full

    task = asyncio.create_task(tl.run())
    try:
        await _wait_until(lambda: len(tl._ring) == 2)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    recent = tl.recent()
    assert len(recent) == 2
    assert {e.request_bytes for e in recent} == {100, 101}


# ── unwritable log path degrades to ring-buffer-only ──────────────────────────

@pytest.mark.asyncio
async def test_unwritable_log_path_does_not_break_recent(tmp_path: Path) -> None:
    # Point log_path at a location whose parent can't be created
    # (a file where a directory is expected).
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    bad_path = blocker / "requests.jsonl"

    tl = TrafficLog(ring_size=10, queue_size=10, log_path=bad_path)
    task = asyncio.create_task(tl.run())
    try:
        entry = _make_entry(0)
        tl.emit(entry)
        await _wait_until(lambda: len(tl._ring) == 1)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    recent = tl.recent()
    assert len(recent) == 1
    assert recent[0] == entry


def test_unknown_session_constant_shape() -> None:
    assert UNKNOWN_SESSION.pid is None
    assert UNKNOWN_SESSION.cwd is None
    assert UNKNOWN_SESSION.label == "unknown"
