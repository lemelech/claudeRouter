# 02 — Session resolver (PID + cwd via /proc)

**Depends on**: nothing. Can start immediately.
**Files**: `src/clauderouter/sessions.py` (new), `tests/test_sessions.py` (new)

## Goal

Given the local proxy server's address and a connected client's peer address,
determine the **PID and cwd of the OS process that opened the connection** — this
is the "session" identifier shown in the dashboard. Must degrade to `"unknown"`
on any failure (non-Linux, permissions, race conditions) without raising.

## Details

### Data shape

```python
@dataclass(frozen=True)
class SessionInfo:
    pid: int | None
    cwd: str | None
    label: str   # "12345 (~/projectA)" or "unknown"

UNKNOWN_SESSION = SessionInfo(pid=None, cwd=None, label="unknown")
```

`label`: if `cwd` starts with `$HOME`, replace that prefix with `~` for
readability (e.g. `/home/elimel/projectA` → `~/projectA`). Format:
`f"{pid} ({display_cwd})"`. If `cwd` is `None` but `pid` is known, label is just
`str(pid)`.

### `SessionResolver`

```python
class SessionResolver:
    def __init__(self, cache_size: int = 256, cache_ttl_secs: float = 60.0) -> None: ...
    def resolve(self, peer_ip: str, peer_port: int,
                 local_ip: str, local_port: int) -> SessionInfo: ...
```

Caller passes (from `aiohttp`'s `request.transport`):
- `peer_ip, peer_port = request.transport.get_extra_info("peername")` — the
  client's address as seen by the proxy.
- `local_ip, local_port = request.transport.get_extra_info("sockname")` — the
  proxy's own listening address for this connection.

### Algorithm

1. **Cache lookup**: key = `(peer_ip, peer_port)`. If a cached entry exists and is
   younger than `cache_ttl_secs`, return it. Cache is a simple dict; evict oldest
   entries once it exceeds `cache_size` (FIFO is fine — `collections.OrderedDict`
   or just track insertion order).

2. **Only handle IPv4** (`127.0.0.1`). If `peer_ip` or `local_ip` contains `:`
   (IPv6, e.g. `::1`), return `UNKNOWN_SESSION` immediately (cache this result too,
   so we don't retry every request). This is a known v1 limitation — the proxy is
   documented to bind `127.0.0.1`.

3. **Find the inode**: read `/proc/net/tcp`. Each data line (skip the header) has
   whitespace-separated fields; field 1 is `local_address` and field 2 is
   `rem_address`, each formatted `HEXIP:HEXPORT`. **From the connecting process's
   point of view, *its* local address is `peer_ip:peer_port` and *its* remote
   address is `local_ip:local_port`** (i.e. swapped relative to the proxy's view).
   So look for the row where:
   - `local_address` decodes to `(peer_ip, peer_port)`
   - `rem_address` decodes to `(local_ip, local_port)`

   **Decoding**: `HEXPORT` is a normal big-endian 16-bit hex number. `HEXIP` for
   IPv4 is the 4 address bytes written **in reverse (little-endian) order** as hex,
   e.g. `127.0.0.1` → bytes `7F 00 00 01` → encoded as `0100007F`. Write a small
   helper `_encode_ipv4_port(ip: str, port: int) -> str` that produces the
   `HEXIP:HEXPORT` string for comparison (uppercase, matching `/proc/net/tcp`'s
   formatting — compare case-insensitively to be safe).

   Take field index 9, `inode`, from the matching line. If no line matches, return
   `UNKNOWN_SESSION` (cached).

4. **Find the PID owning that inode**: iterate `Path("/proc").glob("[0-9]*")`. For
   each `pid_dir`, iterate `(pid_dir / "fd").iterdir()` (wrap in
   `try/except (PermissionError, FileNotFoundError, OSError)` — skip on error,
   processes you don't own or that exit mid-scan are expected and not failures).
   For each fd, `os.readlink(fd_path)`; if it equals `f"socket:[{inode}]"`, that
   `pid_dir.name` is the PID. Stop on first match.

   If no PID found, return `UNKNOWN_SESSION` (cached).

5. **Read cwd**: `os.readlink(f"/proc/{pid}/cwd")`, wrapped in try/except → `None`
   on failure.

6. Build `SessionInfo(pid=int(pid), cwd=cwd, label=...)`, cache it (keyed by
   `(peer_ip, peer_port)`, timestamped for TTL), and return it.

Wrap **the entire `resolve()` body** in a top-level `try/except Exception` as a
final safety net — any unexpected error returns `UNKNOWN_SESSION` (not cached,
so a transient error can be retried on the next request).

## Tests (`tests/test_sessions.py`)

Build a fake `/proc` tree under `tmp_path` and monkeypatch the module's
`PROC_ROOT` (add a module-level `PROC_ROOT = Path("/proc")` constant specifically
so tests can override it — this is the seam that makes the module testable).

- Construct `tmp_path/net/tcp` with a header line + one data line whose
  `local_address`/`rem_address`/`inode` correspond to a known
  `(peer_ip, peer_port)` / `(local_ip, local_port)` / `inode=12345`.
- Construct `tmp_path/<pid>/fd/0` as a symlink to `socket:[12345]`, and
  `tmp_path/<pid>/cwd` as a symlink to some directory.
- `resolve(peer_ip, peer_port, local_ip, local_port)` → `SessionInfo(pid=<pid>,
  cwd=<that dir>, label=...)`.
- Missing `/proc/net/tcp` (e.g. `PROC_ROOT` points somewhere empty) →
  `UNKNOWN_SESSION`, no exception.
- IPv6 peer (`peer_ip="::1"`) → `UNKNOWN_SESSION` without touching the filesystem.
- Cache: second call with the same `(peer_ip, peer_port)` within `cache_ttl_secs`
  returns the same object without re-reading the fake `/proc` tree (e.g. delete
  the fake tree after the first call and assert the second call still succeeds
  from cache).
- Verify the `~`-substitution in `label` when `cwd` is under `$HOME`.

## Note for ticket 04 (consumer)

`router.py` will call `resolver.resolve(*peername, *sockname)` once per request
(cache makes repeated calls on the same connection cheap) and pass the resulting
`SessionInfo` into `LogEntry.session`.
