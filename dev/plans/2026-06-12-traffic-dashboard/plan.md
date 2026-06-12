# Traffic Dashboard & Request Log

**Date**: 2026-06-12
**Goal**: Make provider/model routing transparent — a live web dashboard (served by the
proxy itself at `http://localhost:4891/dashboard`) showing the current mode, the
provider auto-mode would currently pick, and a log of recent requests (session,
provider, model, sizes, tokens, status, duration).

## Background / decisions made during design discussion

- **No new app/process.** The proxy is already an aiohttp server on `localhost:4891`.
  The dashboard is just another route on it — open a browser tab and pin it.
- **"Session" = PID + cwd of the connecting process**, resolved via a one-time
  `/proc` lookup per TCP connection (cached). Claude Code sends no session ID of its
  own; this is the only OS-level signal that groups a main session with its subagent
  fan-out (they share one process/connection-pool). Falls back to `"unknown"` on any
  error (non-Linux, permission issues, race conditions) — never raises.
- **Logging must never affect the primary proxy path.** All inspection (usage
  parsing, session lookup) is non-blocking, exception-isolated, and best-effort.
  Final log emission goes through an `asyncio.Queue` drained by a background task
  (same pattern as the existing `probe_loop`), so file I/O never blocks a response.
- **Storage**: in-memory ring buffer (for the dashboard) + append-only JSONL file
  (for persistence across restarts). No database dependency.
- **Token usage** (input/output/cache tokens) is the most meaningful "usage" metric
  for a multi-provider router — extracted from SSE `message_start`/`message_delta`
  events for streaming responses, or from the single JSON body for `stream:false`.
  This requires extending the *one* code path that currently does true zero-copy
  passthrough (native-thinking providers' SSE) to also split on event boundaries —
  bytes forwarded are unchanged, just observed in passing.
- **Dashboard rendering**: inline SVG/canvas + vanilla JS, no CDN dependencies
  (consistent with the project's offline-friendly design). Client-side aggregation
  from a flat list of recent entries — the backend stays dumb.

## Architecture

```
                 ┌─────────────────────────────────────────────┐
 Claude Code ───▶│ handle_proxy (router.py)                     │
                 │  - resolve session (sessions.py, cached)     │
                 │  - forward request, stream response          │
                 │  - observe usage while streaming (no buffer  │
                 │    delay added)                              │
                 │  - traffic_log.emit(entry)  [non-blocking]   │
                 └───────────────┬───────────────────────────────┘
                                  │ asyncio.Queue
                                  ▼
                 ┌─────────────────────────────────────────────┐
                 │ TrafficLog.run() background task             │
                 │  - append to ring buffer (deque)             │
                 │  - append line to requests.jsonl             │
                 └───────────────┬───────────────────────────────┘
                                  │
        GET /control/traffic  ◀──┘   (mode, effective provider, recent entries)
        GET /dashboard  ──▶ static/dashboard.html (polls the above + /control/status)
```

## Interface contracts

These are frozen so tickets can be built in parallel against them.

### `SessionInfo` (src/clauderouter/sessions.py)

```python
@dataclass(frozen=True)
class SessionInfo:
    pid: int | None
    cwd: str | None
    label: str   # e.g. "12345 (~/projectA)" or "unknown"

UNKNOWN_SESSION = SessionInfo(pid=None, cwd=None, label="unknown")

class SessionResolver:
    def __init__(self, cache_size: int = 256, cache_ttl_secs: float = 60.0): ...
    def resolve(self, peer_ip: str, peer_port: int,
                 local_ip: str, local_port: int) -> SessionInfo: ...
```

### `LogEntry` / `TrafficLog` (src/clauderouter/traffic_log.py)

```python
@dataclass(frozen=True)
class LogEntry:
    timestamp: str                  # ISO8601 UTC with milliseconds, e.g. "2026-06-12T14:32:01.123Z"
    session: SessionInfo
    provider: str | None            # None only for 503 "no provider available"
    mode: str                        # "auto" or a forced provider name
    requested_model: str
    translated_model: str | None    # None when provider is None
    tried: list[str]                # providers attempted before this result
    request_bytes: int
    response_bytes: int
    response_content_type: str
    status: int
    error_summary: str | None       # truncated to ~200 chars, only for 4xx/503
    usage: dict | None               # {"input_tokens", "output_tokens",
                                      #  "cache_read_input_tokens", "cache_creation_input_tokens"}
    duration_ms: float

    def to_dict(self) -> dict: ...   # JSON-serializable; session -> nested {"pid","cwd","label"}

class TrafficLog:
    def __init__(self, ring_size: int, queue_size: int, log_path: Path | None): ...
    def emit(self, entry: LogEntry) -> None: ...       # non-blocking, drops on QueueFull
    def recent(self, n: int | None = None) -> list[LogEntry]: ...  # most-recent-first
    async def run(self) -> None: ...                    # background task (drain queue)
```

### `GET /control/traffic` (new endpoint, server.py)

```json
{
  "mode": "auto",
  "effective_provider": "anthropic",
  "entries": [
    {
      "timestamp": "2026-06-12T14:32:01.123Z",
      "session": {"pid": 12345, "cwd": "/home/elimel/projectA", "label": "12345 (~/projectA)"},
      "provider": "anthropic",
      "mode": "auto",
      "requested_model": "claude-sonnet-4-6",
      "translated_model": "claude-sonnet-4-6",
      "tried": [],
      "request_bytes": 4213,
      "response_bytes": 18234,
      "response_content_type": "text/event-stream",
      "status": 200,
      "error_summary": null,
      "usage": {"input_tokens": 1200, "output_tokens": 340,
                 "cache_read_input_tokens": 800, "cache_creation_input_tokens": 0},
      "duration_ms": 4521.3
    }
  ]
}
```

`entries` ordered most-recent-first. `effective_provider` = result of
`pick_provider(requested_model, providers, registry, tried=set())` for the most
recent entry's `requested_model`, or `null` if `entries` is empty.

### App state keys (server.py ↔ router.py contract)

`create_app` (ticket 05) populates these on `app` before routes run; `handle_proxy`
(ticket 04) reads them:

```python
app["traffic_log"]: TrafficLog          # from traffic_log.py (ticket 01)
app["session_resolver"]: SessionResolver  # from sessions.py (ticket 02)
```

### Config additions (config.py / config.example.toml)

```toml
[server]
traffic_log_path = "~/.local/state/claudeRouter/requests.jsonl"  # "" disables file persistence
traffic_log_ring_size = 500
traffic_log_queue_size = 1000
```

## Tickets & parallelization

```
Phase 1 (fully parallel — independent files, build against contracts above)
  01-traffic-log-storage.md   src/clauderouter/traffic_log.py, config.py, config.example.toml
  02-session-resolver.md      src/clauderouter/sessions.py
  03-dashboard-ui.md           src/clauderouter/static/dashboard.html

Phase 2 (parallel with each other; each depends only on Phase 1 interfaces, not on
         Phase 1 PRs landing first — the dataclasses/signatures above are stable)
  04-router-instrumentation.md   src/clauderouter/router.py     (needs 01 + 02 interfaces)
  05-server-wiring-endpoint.md   src/clauderouter/server.py,    (needs 01 interface,
                                  README.md                       feeds 03's contract)
```

Each ticket includes its own tests. No separate test/docs phase.

## Out of scope (future work)

- Persisting/rotating the JSONL log (size-based rotation).
- Server-side aggregation/analytics beyond the raw recent-entries list.
- IPv6 loopback session resolution (proxy is documented to bind `127.0.0.1`; IPv6
  peers resolve to `"unknown"`).
- Grouping the dashboard table by session (v1 shows session as a column; sorting/
  grouping in the UI is a stretch goal, not a blocker).
