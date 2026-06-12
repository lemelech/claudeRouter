# 05 — Server wiring, /control/traffic endpoint, /dashboard route

**Depends on**: interface from 01 (`TrafficLog`). Build against 03's static file
(if not yet landed, create a placeholder `static/dashboard.html` with a `<h1>TODO
dashboard</h1>` so routing/serving can be tested independently, then swap in the
real file once 03 lands — these are independent files, no merge conflict). Can run
in parallel with 04.
**Files**: `src/clauderouter/server.py`, `pyproject.toml` (packaging check),
`README.md`, `tests/test_dashboard_endpoint.py` (new)

## App state keys (coordinate with 04)

`create_app` must populate, before routes are registered:

- `app["traffic_log"] = TrafficLog(ring_size=cfg.server.traffic_log_ring_size, queue_size=cfg.server.traffic_log_queue_size, log_path=<expanduser'd Path or None>)`
- `app["session_resolver"] = SessionResolver()`

## Part A — background writer task

Mirror the existing `probe_task` pattern:

- In `_on_startup`: `app["traffic_log_task"] = asyncio.create_task(app["traffic_log"].run())`
- In `_on_cleanup`: cancel it and await, same as `app["probe_task"]`
  (`try/except asyncio.CancelledError`).

## Part B — `GET /control/traffic`

```python
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
```

Register: `app.router.add_get("/control/traffic", _handle_control_traffic)`.

This matches the JSON shape documented in `plan.md` under "Interface contracts" —
`entries` must already be most-recent-first from `TrafficLog.recent()` (ticket 01).

## Part C — `GET /dashboard`

Serve `src/clauderouter/static/dashboard.html` as `text/html`. Two reasonable
approaches — pick whichever keeps packaging simple:

```python
_DASHBOARD_PATH = Path(__file__).parent / "static" / "dashboard.html"

async def _handle_dashboard(_request: web.Request) -> web.Response:
    return web.Response(text=_DASHBOARD_PATH.read_text(), content_type="text/html")
```

Register: `app.router.add_get("/dashboard", _handle_dashboard)`.

**Packaging check**: confirm `static/dashboard.html` is present and readable when
installed via `uv tool install .` / `pip install -e .` — hatchling includes
package-relative non-`.py` files by default for editable installs, but verify for
a real build. If it's missing from a built wheel, add to `pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel]
artifacts = ["src/clauderouter/static/*"]
```

(or the equivalent `force-include` mapping — check what's actually needed and use
the minimal fix.)

## Part D — config wiring

In `create_app`, read the three new `cfg.server.traffic_log_*` fields (ticket 01)
to construct `TrafficLog`. Expand `traffic_log_path` with
`Path(p).expanduser()` if non-empty, else `None`.

## Part E — README

Add a short "Dashboard" section: `http://localhost:4891/dashboard` shows the
current mode, effective provider in auto mode, provider health, and recent
request traffic (provider/model/size/tokens/duration per request, grouped by
session). One or two sentences, matching the existing README's terse style.

## Tests (`tests/test_dashboard_endpoint.py`)

- `GET /control/traffic` with no traffic yet → `200`, `entries == []`,
  `effective_provider is None`, `mode` matches `rt.get_mode()`.
- After manually `traffic_log.emit(...)`-ing a couple of `LogEntry` fixtures into
  the app's `TrafficLog` and giving the background task a tick to drain the
  queue, `GET /control/traffic` returns those entries (most-recent-first) with the
  documented JSON shape, and `effective_provider` reflects
  `pick_provider(entries[0].requested_model, ...)`.
- `GET /dashboard` → `200`, `Content-Type: text/html`, body contains something
  recognizable from the static file (e.g. a known string/title).
- Startup/cleanup: the traffic-log background task starts and is cancelled
  cleanly (no warnings/errors on app shutdown) — extend whatever pattern existing
  tests use to check `probe_task` lifecycle, if any; otherwise a basic
  start-app/stop-app smoke test is sufficient.
