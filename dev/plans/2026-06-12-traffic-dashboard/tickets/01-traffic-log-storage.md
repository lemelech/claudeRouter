# 01 — Traffic log storage + config

**Depends on**: nothing. Can start immediately.
**Files**: `src/clauderouter/traffic_log.py` (new), `src/clauderouter/config.py`,
`config.example.toml`, `tests/test_traffic_log.py` (new)

## Goal

A small, self-contained module providing the `LogEntry` data shape and a
`TrafficLog` that buffers entries in memory (ring buffer) and persists them to a
JSONL file via a background task — without ever blocking a caller.

## Details

### `LogEntry`

Implement exactly the dataclass in `plan.md` → "Interface contracts" →
`LogEntry`. `to_dict()` must produce the JSON shape shown under
`GET /control/traffic` in `plan.md` (note: `session` becomes a nested
`{"pid", "cwd", "label"}` object — `SessionInfo` itself should also get a
`to_dict()`, or `LogEntry.to_dict()` can call `dataclasses.asdict()` on it).

`SessionInfo` is defined in ticket 02 (`sessions.py`). Import it; if ticket 02
hasn't landed yet, define a local stub matching the frozen contract
(`pid: int | None`, `cwd: str | None`, `label: str`) so this ticket is independently
testable — but prefer importing the real one once both land.

### `TrafficLog`

```python
class TrafficLog:
    def __init__(self, ring_size: int, queue_size: int, log_path: Path | None) -> None: ...
    def emit(self, entry: LogEntry) -> None: ...
    def recent(self, n: int | None = None) -> list[LogEntry]: ...
    async def run(self) -> None: ...
```

- `__init__`: `_ring: deque[LogEntry] = deque(maxlen=ring_size)`,
  `_queue: asyncio.Queue[LogEntry] = asyncio.Queue(maxsize=queue_size)`,
  store `log_path`.
- `emit(entry)`: **non-blocking**. Call `self._queue.put_nowait(entry)`. Catch
  `asyncio.QueueFull` and drop the entry (log at `debug` level, rate-limit if you
  want but not required). Never raises.
- `recent(n=None)`: return a list snapshot of the ring buffer, **most-recent-first**.
  `n=None` returns everything currently buffered.
- `run()`: infinite loop — `entry = await self._queue.get()`, append to `_ring`,
  and if `log_path` is set, append `json.dumps(entry.to_dict()) + "\n"` to the file
  (open in append mode; opening per-line is fine at this volume — don't
  over-engineer with a persistent file handle unless trivial). Wrap the file-write
  in `try/except Exception` — on failure, log a warning **once** (track a flag so
  repeated failures don't spam) and continue; the ring buffer must keep working
  even if the file is unwritable. This task is started/cancelled by ticket 05
  (mirrors `probe_task` in `server.py`).
- If `log_path` is `None` or empty string, skip file writes entirely (ring buffer
  only).

`log_path` should be expanded with `Path(...).expanduser()` and its parent
directory created (`mkdir(parents=True, exist_ok=True)`) — wrap in try/except too,
same "degrade to ring-buffer-only" behavior on failure.

### Config additions (`config.py`)

Add to `ServerConfig`:

```python
traffic_log_path: str = "~/.local/state/claudeRouter/requests.jsonl"
traffic_log_ring_size: int = 500
traffic_log_queue_size: int = 1000
```

Loaded the same way other `ServerConfig` fields are (via the existing
`{k: v for k, v in raw.get("server", {}).items() if k in ServerConfig.__dataclass_fields__}`
filter — no extra code needed in `load()` beyond adding the dataclass fields).

### `config.example.toml`

Add a commented block under `[server]` documenting the three new keys (see
`plan.md` for the exact snippet), including a note that setting
`traffic_log_path = ""` disables file persistence (ring buffer / dashboard still
work).

## Tests (`tests/test_traffic_log.py`)

- `recent()` returns entries most-recent-first and respects `ring_size` (oldest
  evicted once full).
- `emit()` + running `run()` for a bit (e.g. `asyncio.wait_for(..., timeout=...)`
  on a sentinel, or just `await asyncio.sleep(0)` after `emit` + manually pumping
  the queue) results in entries appearing in `recent()`.
- JSONL file gets one valid JSON line per entry; `to_dict()` round-trips through
  `json.dumps`/`json.loads`.
- `log_path=None` → no file is created, `recent()` still works.
- `emit()` never raises and never blocks even if the queue is full (fill the queue
  to `maxsize`, then call `emit` once more — assert no exception, ring stays
  consistent once drained).
- A file-write failure (e.g. point `log_path` at a path under a read-only/
  non-existent unwritable location) doesn't prevent `recent()` from working.
