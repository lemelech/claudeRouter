"""Provider selection, request forwarding, and fallback logic."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import time
import zlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from .health import HealthRegistry
from .providers import Provider
from .sessions import UNKNOWN_SESSION
from .traffic_log import LogEntry

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# Module-level forced mode; "auto" means chain order + health checks.
_mode: str = "auto"


def get_mode() -> str:
    return _mode


def set_mode(mode: str, valid_names: set[str]) -> None:
    global _mode
    if mode != "auto" and mode not in valid_names:
        raise ValueError(f"Unknown provider: {mode!r}")
    _mode = mode


def _skip_reasons(model: str, providers: list[Provider], registry: HealthRegistry,
                  tried: set[str]) -> dict | None:
    """Why each NOT-selectable provider was passed over, for this model right now.

    Eligible providers are omitted. Returns None if every provider is eligible.
    Mirrors the gate in pick_provider (order: tried → ready → model → healthy).
    """
    reasons: dict[str, str] = {}
    for p in providers:
        if p.name in tried:
            reasons[p.name] = "already tried (failed earlier this request)"
        elif not p.is_ready:
            reasons[p.name] = "not ready (no api key)"
        elif not p.supports_model(model):
            reasons[p.name] = "model not supported"
        elif not registry.is_healthy(p.name):
            reasons[p.name] = "unhealthy (last probe failed)"
    return reasons or None


def pick_provider(model: str, providers: list[Provider],
                  registry: HealthRegistry,
                  tried: set[str] | None = None) -> Provider | None:
    tried = tried or set()
    if _mode != "auto":
        for p in providers:
            if p.name == _mode and p.name not in tried:
                if p.is_ready and p.supports_model(model) and registry.is_healthy(p.name):
                    return p
        return None

    for p in sorted(providers, key=lambda x: x.priority):
        if p.name in tried:
            continue
        if p.is_ready and p.supports_model(model) and registry.is_healthy(p.name):
            return p
    return None


def _encode_sse_event(event_line: str | None, data: dict) -> bytes:
    parts = []
    if event_line:
        parts.append(event_line)
    parts.append(f"data: {json.dumps(data)}")
    return "\n".join(parts).encode() + b"\n\n"


def _parse_sse_data(raw: bytes) -> tuple[str | None, dict | None]:
    """Extract the `event:` line and parsed `data:` JSON from a raw SSE event.

    Returns (event_line, data) where either may be None if absent/unparseable.
    Never raises.
    """
    event_str = raw.decode("utf-8", errors="replace")

    event_line = None
    data_str = None
    for line in event_str.split("\n"):
        if line.startswith("event:"):
            event_line = line
        elif line.startswith("data:"):
            data_str = line[5:].strip()

    if not data_str or data_str == "[DONE]":
        return event_line, None

    try:
        data = json.loads(data_str)
    except json.JSONDecodeError:
        return event_line, None

    return event_line, data


def _transform_sse_event(raw: bytes, in_thinking: bool) -> tuple[bool, list[bytes]]:
    """Rewrite thinking-block SSE events as text-block events.

    Returns (new_in_thinking, list of event byte chunks to emit).
    Thinking blocks from non-Anthropic providers carry invalid signatures;
    converting them to text blocks preserves the content without triggering
    Anthropic's signature validation on future turns.
    """
    event_line, data = _parse_sse_data(raw)
    if data is None:
        return in_thinking, [raw + b"\n\n"]

    event_type = data.get("type")

    if event_type == "content_block_start":
        cb = data.get("content_block", {})
        if cb.get("type") == "thinking":
            data["content_block"] = {"type": "text", "text": ""}
            open_delta = {
                "type": "content_block_delta",
                "index": data.get("index", 0),
                "delta": {"type": "text_delta", "text": "<thinking>\n"},
            }
            return True, [
                _encode_sse_event(event_line, data),
                _encode_sse_event("event: content_block_delta", open_delta),
            ]

    elif event_type == "content_block_delta" and in_thinking:
        delta = data.get("delta", {})
        if delta.get("type") == "thinking_delta":
            data["delta"] = {"type": "text_delta", "text": delta.get("thinking", "")}
            return True, [_encode_sse_event(event_line, data)]

    elif event_type == "content_block_stop" and in_thinking:
        close_delta = {
            "type": "content_block_delta",
            "index": data.get("index", 0),
            "delta": {"type": "text_delta", "text": "\n</thinking>"},
        }
        return False, [
            _encode_sse_event("event: content_block_delta", close_delta),
            raw + b"\n\n",
        ]

    return in_thinking, [raw + b"\n\n"]


def _is_thinking_signature_error(body: dict) -> bool:
    err = body.get("error", {})
    if err.get("type") != "invalid_request_error":
        return False
    msg = err.get("message", "")
    return (
        "Invalid signature in thinking block" in msg
        or ("thinking.signature" in msg and "Field required" in msg)
    )


# e.g. Ollama: "max_tokens (64000) exceeds model's maximum output tokens (32768) for model ..."
_MAX_OUTPUT_TOKENS_RE = re.compile(r"maximum output tokens \((\d+)\)")


def _max_output_tokens_from_error(body: dict) -> int | None:
    """Extract the model's output-token limit from a 400 error body, if present."""
    message = body.get("error", {}).get("message", "")
    m = _MAX_OUTPUT_TOKENS_RE.search(message)
    return int(m.group(1)) if m else None


def _clamp_max_tokens(body: dict, limit: int) -> dict:
    """Clamp max_tokens (and any thinking budget, which must stay below it)."""
    clamped = {**body, "max_tokens": limit}
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("budget_tokens", 0) >= limit:
        clamped["thinking"] = {**thinking, "budget_tokens": limit - 1}
    return clamped


def _sterilize_thinking_in_messages(body: dict) -> dict:
    """Convert thinking blocks in message history to text blocks.

    Called when Anthropic rejects a request due to invalid thinking signatures
    (e.g. blocks generated by a non-Anthropic provider in a previous turn).
    """
    new_messages = []
    for msg in body.get("messages", []):
        content = msg.get("content", [])
        if not isinstance(content, list):
            new_messages.append(msg)
            continue
        new_content = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                new_content.append({
                    "type": "text",
                    "text": f"<thinking>\n{block.get('thinking', '')}\n</thinking>",
                })
            else:
                new_content.append(block)
        new_messages.append({**msg, "content": new_content})
    return {**body, "messages": new_messages}


def _update_usage_from_sse_event(raw: bytes, usage: dict) -> None:
    """Best-effort: pull token counts out of message_start/message_delta events.

    Never raises — malformed/unexpected JSON just leaves `usage` unchanged.
    """
    try:
        _event_line, data = _parse_sse_data(raw)
        if data is None:
            return
        event_type = data.get("type")
        if event_type == "message_start":
            msg_usage = data.get("message", {}).get("usage", {})
            usage["input_tokens"] = msg_usage.get("input_tokens", 0)
            usage["output_tokens"] = msg_usage.get("output_tokens", 0)
            usage["cache_read_input_tokens"] = msg_usage.get("cache_read_input_tokens", 0)
            usage["cache_creation_input_tokens"] = msg_usage.get("cache_creation_input_tokens", 0)
        elif event_type == "message_delta":
            delta_usage = data.get("usage", {})
            if "output_tokens" in delta_usage:
                usage["output_tokens"] = delta_usage["output_tokens"]
    except Exception:
        pass


def _stream_decompressor(content_encoding: str):
    """Decompressor for the usage-observation side-channel only.

    Forwarded bytes are never altered; this just lets us read token counts out of
    a compressed body. Returns a zlib decompressobj for gzip/deflate, or None for
    identity / unsupported encodings (in which case observed bytes are used as-is).
    """
    enc = (content_encoding or "").lower().strip()
    if enc in ("gzip", "x-gzip"):
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if enc == "deflate":
        return zlib.decompressobj()
    return None


def _decompress_all(body: bytes, content_encoding: str) -> bytes:
    """Best-effort whole-body decompress for observation. Never raises."""
    dec = _stream_decompressor(content_encoding)
    if dec is None:
        return body
    try:
        return dec.decompress(body) + dec.flush()
    except Exception:
        return b""


def _extract_usage_from_json(body: bytes, usage: dict) -> None:
    """Best-effort: pull `usage` out of a non-streaming Messages response body.

    Never raises — malformed/unexpected JSON just leaves `usage` unchanged.
    """
    try:
        data = json.loads(body)
        msg_usage = data.get("usage", {})
        usage["input_tokens"] = msg_usage.get("input_tokens", 0)
        usage["output_tokens"] = msg_usage.get("output_tokens", 0)
        usage["cache_read_input_tokens"] = msg_usage.get("cache_read_input_tokens", 0)
        usage["cache_creation_input_tokens"] = msg_usage.get("cache_creation_input_tokens", 0)
    except Exception:
        pass


async def _stream(upstream: aiohttp.ClientResponse, response: web.StreamResponse,
                  sterilize: bool, is_sse: bool, content_encoding: str = "") -> tuple[int, dict]:
    usage: dict = {}
    response_bytes = 0

    if not is_sse:
        body = bytearray()
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
            response_bytes += len(chunk)
            body.extend(chunk)
        _extract_usage_from_json(_decompress_all(bytes(body), content_encoding), usage)
        return response_bytes, usage

    if not sterilize:
        # Zero-copy passthrough: forward each chunk UNCHANGED. Observe usage on a
        # decompressed copy (responses are commonly gzip'd; auto_decompress is off).
        dec = _stream_decompressor(content_encoding)
        obs_buf = b""
        obs_ok = True
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
            response_bytes += len(chunk)
            if not obs_ok:
                continue
            try:
                obs_buf += dec.decompress(chunk) if dec is not None else chunk
            except Exception:
                obs_ok, obs_buf = False, b""  # stop observing; never touch forwarding
                continue
            while b"\n\n" in obs_buf:
                raw_event, obs_buf = obs_buf.split(b"\n\n", 1)
                _update_usage_from_sse_event(raw_event, usage)
        return response_bytes, usage

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


def _truncate(s: str, n: int = 200) -> str:
    return s if len(s) <= n else s[:n] + "…"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def handle_proxy(request: web.Request) -> web.StreamResponse:
    providers: list[Provider] = request.app["providers"]
    registry: HealthRegistry = request.app["health_registry"]
    session: aiohttp.ClientSession = request.app["client_session"]

    start = time.monotonic()

    raw_body = await request.read()
    request_bytes = len(raw_body)
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        body = {}

    peername = request.transport.get_extra_info("peername")
    sockname = request.transport.get_extra_info("sockname")
    if peername and sockname:
        session_info = request.app["session_resolver"].resolve(
            peername[0], peername[1], sockname[0], sockname[1])
    else:
        session_info = UNKNOWN_SESSION

    requested_model: str = body.get("model", "")
    tried: set[str] = set()
    signature_sterilized = False  # only sterilize once per request
    max_tokens_clamped = False    # only clamp max_tokens once per request

    def _emit(provider_name: str | None, translated_model: str | None,
              response_bytes: int, content_type: str, status: int,
              error_summary: str | None, usage: dict | None) -> None:
        try:
            request.app["traffic_log"].emit(LogEntry(
                timestamp=_now_iso(),
                session=session_info,
                provider=provider_name,
                mode=get_mode(),
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
                skipped=_skip_reasons(requested_model, providers, registry, tried),
            ))
        except Exception:
            log.debug("traffic log emit failed", exc_info=True)

    while True:
        provider = pick_provider(requested_model, providers, registry, tried)
        if provider is None:
            tried_str = ", ".join(tried) if tried else "none"
            message = (
                f"No healthy provider available for model {requested_model!r} "
                f"(mode={_mode!r}, tried={tried_str})."
            )
            body_bytes = json.dumps({
                "error": {
                    "type": "provider_unavailable",
                    "message": message,
                }
            }).encode()
            _emit(None, None, len(body_bytes), "application/json", 503, message, None)
            return web.Response(
                status=503,
                content_type="application/json",
                body=body_bytes,
            )

        upstream_url = provider.base_url + request.path_qs
        mutated = {**provider.extra_body, **body}
        mutated["model"] = provider.translate_model(requested_model)
        upstream_body = json.dumps(mutated).encode()

        headers = provider.apply_auth(dict(request.headers))
        headers.pop("Host", None)
        headers.pop("host", None)
        # Usage is read by decompressing the response in a side-channel. Pin native
        # providers to gzip so the stdlib can always decode it (avoids needing
        # brotli/zstd deps). Non-native providers are left untouched — their SSE is
        # sterilized, which assumes an uncompressed stream.
        if provider.native_thinking:
            for k in [h for h in headers if h.lower() == "accept-encoding"]:
                headers.pop(k)
            headers["Accept-Encoding"] = "gzip"
        headers["Content-Length"] = str(len(upstream_body))

        log.info("→ %s %s via %s (model: %s → %s)",
                 request.method, request.path, provider.name,
                 requested_model, mutated["model"])

        try:
            upstream = await session.request(
                request.method,
                upstream_url,
                headers=headers,
                data=upstream_body,
                allow_redirects=False,
            )
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
            log.warning("Provider %s connection error: %s — trying next", provider.name, e)
            registry.record(provider.name, False, str(e))
            tried.add(provider.name)
            continue

        if upstream.status in (429, 529):
            log.warning("Provider %s returned %d — trying next", provider.name, upstream.status)
            registry.record(provider.name, False, f"{upstream.status}")
            tried.add(provider.name)
            upstream.release()
            continue

        if upstream.status == 400:
            error_bytes = await upstream.read()
            resp_headers = {k: v for k, v in upstream.headers.items()
                            if k.lower() not in ("transfer-encoding", "content-length")}
            upstream.release()
            # Decompress a copy for inspection only; error_bytes stays as-is so the
            # forwarded body matches the pass-through Content-Encoding header.
            inspect_bytes = error_bytes
            if error_bytes.startswith(b"\x1f\x8b"):
                try:
                    inspect_bytes = gzip.decompress(error_bytes)
                except (gzip.BadGzipFile, zlib.error, EOFError):
                    inspect_bytes = b""
            try:
                error_body = json.loads(inspect_bytes)
            except (json.JSONDecodeError, UnicodeDecodeError):
                error_body = {}
            if (provider.native_thinking and not signature_sterilized
                    and _is_thinking_signature_error(error_body)):
                log.warning("Thinking signature error — sterilizing history and retrying")
                body = _sterilize_thinking_in_messages(body)
                signature_sterilized = True
                continue  # retry same provider (not added to tried)
            limit = _max_output_tokens_from_error(error_body)
            if (limit is not None and not max_tokens_clamped
                    and body.get("max_tokens", 0) > limit):
                log.warning("Provider %s caps output at %d tokens — clamping and retrying",
                            provider.name, limit)
                body = _clamp_max_tokens(body, limit)
                max_tokens_clamped = True
                continue  # retry same provider (not added to tried)
            _emit(provider.name, mutated["model"], len(error_bytes),
                  resp_headers.get("Content-Type", "application/json"), 400,
                  _truncate(error_body.get("error", {}).get("message", "")), None)
            return web.Response(status=400, headers=resp_headers, body=error_bytes)

        is_sse = "text/event-stream" in upstream.headers.get("Content-Type", "")
        sterilize = is_sse and not provider.native_thinking

        response = web.StreamResponse(
            status=upstream.status,
            headers={k: v for k, v in upstream.headers.items()
                     if k.lower() not in ("transfer-encoding", "content-length")},
        )
        await response.prepare(request)

        response_bytes = 0
        usage: dict = {}
        content_encoding = upstream.headers.get("Content-Encoding", "")
        try:
            response_bytes, usage = await _stream(upstream, response, sterilize, is_sse,
                                                  content_encoding)
        except (aiohttp.ClientError, ConnectionResetError) as e:
            log.debug("Stream closed from %s: %s", provider.name, e)
        finally:
            upstream.release()

        try:
            await response.write_eof()
        except (aiohttp.ClientError, ConnectionResetError):
            pass

        _emit(provider.name, mutated["model"], response_bytes,
              upstream.headers.get("Content-Type", ""), upstream.status, None, usage)
        return response
