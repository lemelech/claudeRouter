# 04 — Router instrumentation (capture + emit log entries)

**Depends on**: interfaces from 01 (`LogEntry`, `TrafficLog`) and 02
(`SessionInfo`, `SessionResolver`, `UNKNOWN_SESSION`). Does not need 01/02's PRs
merged first — their dataclass/class signatures are frozen in `plan.md`; import
them and proceed. Can run in parallel with 05.
**Files**: `src/clauderouter/router.py`, `tests/test_router_logging.py` (new)

## App state keys (coordinate with 05)

`handle_proxy` reads two objects off `request.app`, created/stored by ticket 05:

- `request.app["traffic_log"]: TrafficLog`
- `request.app["session_resolver"]: SessionResolver`

## Goal

For **every** request handled by `handle_proxy` — success, fallback, or error —
build a `LogEntry` and call `traffic_log.emit(entry)`. This must never change the
bytes sent to the client, never add blocking I/O to the hot path, and never let an
exception in logging propagate out of `handle_proxy`.

## Part A — timing, sizes, session (top of `handle_proxy`)

```python
start = time.monotonic()
request_bytes = len(raw_body)

peername = request.transport.get_extra_info("peername")
sockname = request.transport.get_extra_info("sockname")
if peername and sockname:
    session = request.app["session_resolver"].resolve(
        peername[0], peername[1], sockname[0], sockname[1])
else:
    session = UNKNOWN_SESSION
```

(`SessionResolver.resolve` already catches everything internally per ticket 02 —
no extra try/except needed here.)

## Part B — usage extraction in `_stream`

Change `_stream`'s signature to return `(response_bytes: int, usage: dict)` and add
an `is_sse: bool` parameter (the caller already computes this). **Goal: observe
without changing what/when bytes are written.**

```python
async def _stream(upstream, response, sterilize: bool, is_sse: bool) -> tuple[int, dict]:
    usage: dict = {}
    response_bytes = 0

    if not is_sse:
        # stream:false JSON body — forward each chunk immediately (unchanged
        # timing), and separately accumulate for a post-hoc usage parse.
        body = bytearray()
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
            response_bytes += len(chunk)
            body.extend(chunk)
        _extract_usage_from_json(bytes(body), usage)
        return response_bytes, usage

    if not sterilize:
        # Existing zero-copy passthrough — UNCHANGED writes. Side-channel buffer
        # for event-boundary observation only; trailing partial event is discarded.
        obs_buf = b""
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
            response_bytes += len(chunk)
            obs_buf += chunk
            while b"\n\n" in obs_buf:
                raw_event, obs_buf = obs_buf.split(b"\n\n", 1)
                _update_usage_from_sse_event(raw_event, usage)
        return response_bytes, usage

    # Sterilize path — same structure as today, plus usage observation per event.
    in_thinking = False
    buf = b""
    async for chunk in upstream.content.iter_any():
        buf += chunk
        while b"\n\n" in buf:
            raw_event, buf = buf.split(b"\n\n", 1)
            _update_usage_from_sse_event(raw_event, usage)
            in_thinking, events = _transform_sse_event(raw_event, in_thinking)
            for evt in events:
                await response.write(evt)
                response_bytes += len(evt)
    if buf:
        await response.write(buf)
        response_bytes += len(buf)
    return response_bytes, usage
```

Update the call site in `handle_proxy` accordingly (it currently does
`await _stream(upstream, response, sterilize)` and discards the result).

### New helpers

```python
def _update_usage_from_sse_event(raw: bytes, usage: dict) -> None:
    """Best-effort: pull token counts out of message_start/message_delta events.
    Never raises — malformed/unexpected JSON just leaves `usage` unchanged."""

def _extract_usage_from_json(body: bytes, usage: dict) -> None:
    """Best-effort: pull `usage` out of a non-streaming Messages response body."""
```

- `message_start`: read `data["message"]["usage"]` (dict, may be missing/partial —
  use `.get(key, 0)`), set `usage["input_tokens"]`,
  `usage["cache_read_input_tokens"]`, `usage["cache_creation_input_tokens"]`, and
  initialize `usage["output_tokens"]` from the same object (usually 0 at this
  point).
- `message_delta`: read `data.get("usage", {})`; if it has `output_tokens`,
  **overwrite** `usage["output_tokens"]` (it's cumulative).
- `_extract_usage_from_json`: `json.loads(body)`, read top-level `usage` the same
  way, populate all four keys.
- Both wrap their body in `try/except Exception: pass` (or
  `except (json.JSONDecodeError, KeyError, TypeError, ValueError)` if you prefer
  to be precise — either is fine, but it must not raise).
- Reuse whatever line-parsing logic `_transform_sse_event` already has for
  extracting the `data:` line + `json.loads` — consider factoring a shared
  `_parse_sse_data(raw: bytes) -> dict | None` helper used by both
  `_transform_sse_event` and `_update_usage_from_sse_event` to avoid duplication
  (optional but recommended).

At the end, if `usage` is `{}` (no events recognized — e.g. very short/aborted
stream), the `LogEntry.usage` field should be `None`, not `{}`.

## Part C — emit a `LogEntry` at every return point

There are three return points in `handle_proxy`. At each, before returning, build
the entry and call `traffic_log.emit(entry)` **inside a
`try/except Exception` that only logs at `debug` level** — a bug in entry
construction must never turn into a 500 for the client.

A small local helper keeps this from being repeated three times:

```python
def _emit(provider_name, translated_model, response_bytes, content_type,
          status, error_summary, usage):
    try:
        request.app["traffic_log"].emit(LogEntry(
            timestamp=_now_iso(),
            session=session,
            provider=provider_name,
            mode=rt.get_mode(),
            requested_model=requested_model,
            translated_model=translated_model,
            tried=list(tried),
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            response_content_type=content_type,
            status=status,
            error_summary=error_summary,
            usage=usage or None,
            duration_ms=(time.monotonic() - start) * 1000,
        ))
    except Exception:
        log.debug("traffic log emit failed", exc_info=True)
```

(`_now_iso()`: `datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")` —
or any equivalent producing the format shown in `plan.md`.)

Call sites:

1. **503 "no provider available"** (provider is `None`): `_emit(None, None,
   len(body_bytes), "application/json", 503, message, None)` — `message` is the
   same string already used to build the error JSON; `body_bytes` is the encoded
   JSON body.

2. **400 error from upstream**: `_emit(provider.name, mutated["model"],
   len(error_bytes), resp_headers.get("Content-Type", "application/json"), 400,
   _truncate(error_body.get("error", {}).get("message", "")), None)`. Add
   `_truncate(s: str, n: int = 200) -> str` (append `"…"` if cut).

3. **Success** (streaming or not): after `_stream` returns
   `(response_bytes, usage)`, and after the `write_eof()` try/except (emit
   regardless of whether `write_eof` raised — that's a client-disconnect issue,
   not a logging concern): `_emit(provider.name, mutated["model"], response_bytes,
   upstream.headers.get("Content-Type", ""), upstream.status, None, usage)`.

## Tests (`tests/test_router_logging.py`)

Use a fake `TrafficLog` (or the real one from ticket 01 with a small ring/queue)
and a fake `SessionResolver` returning a fixed `SessionInfo`, injected via
`app["traffic_log"]` / `app["session_resolver"]` in a test `aiohttp` app (existing
tests likely already build a test app — follow that pattern).

- Successful non-streaming (`stream:false`) response with a `usage` field in the
  body → emitted `LogEntry.usage` matches, `response_bytes` equals the body
  length, bytes received by the test client are byte-identical to the upstream
  body (no buffering artifacts).
- Successful SSE response from a `native_thinking=True` provider (no
  sterilization) containing `message_start` + `message_delta` events → emitted
  `usage.input_tokens`/`output_tokens` populated correctly, and the SSE bytes
  received by the client are **byte-identical** to upstream (zero-copy path
  unchanged).
- Successful SSE response from a non-native-thinking provider with a thinking
  block (sterilization active) → existing sterilization behavior unchanged
  (reuse/extend existing sterilization tests if present), AND usage still
  extracted.
- 400 error response → `LogEntry.status == 400`, `error_summary` populated and
  truncated, `usage is None`.
- 503 "no provider available" → `LogEntry.provider is None`, `status == 503`.
- Fallback: first provider returns 429, second succeeds → `LogEntry.tried ==
  ["<first provider name>"]`.
- **Logging never breaks the response**: monkeypatch `traffic_log.emit` to raise
  `Exception("boom")` → request still completes successfully and the client gets
  the correct response/bytes (the `_emit` try/except swallows it).
- `request_bytes` equals `len(raw_body)` of the original inbound request (not
  affected by sterilization/clamping mutations applied during retries).
