# Thinking Block Sterilization

## Why

Anthropic cryptographically signs extended-thinking blocks. Other providers
(Ollama local/remote/cloud) emit thinking blocks with missing or invalid
signatures. Once such a block enters Claude Code's conversation history, every
subsequent request to the real Anthropic API fails with one of two
`invalid_request_error` 400s, depending on whether the upstream block carried
a (bad) signature or none at all:

```
400 invalid_request_error: Invalid signature in thinking block
400 invalid_request_error: <path>.thinking.signature: Field required
```

Because claudeRouter's whole point is switching providers mid-session, a
single turn served by Ollama could otherwise permanently poison the session
for Anthropic. Sterilization prevents that.

## How

Protection is two-layered: prevent corruption going forward, and recover when
history is already corrupted.

### Layer 1 — response sterilization (prevention)

For any provider without `native_thinking = true` in config, the proxy
rewrites thinking blocks into plain text before they reach Claude Code,
covering both response shapes:

- **SSE streams** — thinking-block events are rewritten into text-block
  events (`_transform_sse_event` in `src/clauderouter/router.py`):
  - `content_block_start` with `type: thinking` becomes a text block, and a
    synthetic delta injects an opening `<thinking>\n` marker.
  - `thinking_delta` events become `text_delta` events carrying the same
    text.
  - `content_block_stop` is preceded by a synthetic `\n</thinking>` delta.

- **Non-streaming `application/json` responses** — the proxy buffers the
  body, walks the top-level `content[]`, and converts any `thinking` block
  to a `<thinking>…</thinking>` text block (`_sterilize_thinking_in_response`,
  via the non-SSE branch of `_stream`). If the body was gzip/deflate
  compressed, it's decompressed for the rewrite and re-compressed to the
  original `Content-Encoding` before forwarding.

In both cases the reasoning content is preserved verbatim for the user; only
the block type changes. Since the blocks stored in history are now ordinary
text, Anthropic never sees an unsigned thinking block on later turns.

For the non-streaming path, a response with no `thinking` block (or any
non-Messages JSON, e.g. error bodies or model listings) is forwarded
byte-identical — no re-serialization or field injection occurs. Any
parse/transform/recompress failure falls back to forwarding the original
bytes untouched. Bodies compressed with `br` or `zstd` are not decompressed
(the proxy's decompressor only supports gzip/deflate) and so pass through
untouched without sterilization — the same limitation that applies on the
SSE side; no currently configured provider uses `br`/`zstd`.

Set `native_thinking = true` only on providers whose signatures Anthropic
will accept — currently Anthropic itself. All others default to `false`, and
native providers get byte-identical passthrough on both response shapes.

### Layer 2 — auto-retry on 400 (recovery)

If Anthropic still rejects a request with either signature-error variant
(history was corrupted before this feature existed, or `/compact` replays a
broken session), the proxy:

1. Reads the 400 error body from upstream.
2. Checks whether it is one of the two thinking-signature errors —
   `Invalid signature in thinking block` or
   `<path>.thinking.signature: Field required`
   (`_is_thinking_signature_error`).
3. If so, rewrites every `thinking` block in the request's `messages` into a
   `<thinking>…</thinking>` text block
   (`_sterilize_thinking_in_messages`) and retries the same provider once
   per request (`signature_sterilized` flag).
4. Any other 400 is forwarded to Claude Code untouched.

Running `/compact` plus this auto-retry is the full recovery path: the
compacted summary replaces history with clean text.

## Gzip handling in the 400 path

The proxy's upstream `ClientSession` is created with `auto_decompress=False`
(`src/clauderouter/server.py`): compressed bodies and their
`Content-Encoding` headers normally pass through to Claude Code byte-for-byte,
and the client decompresses them itself.

The 400-retry check is the one place the proxy must read an upstream body.
Anthropic serves error bodies gzip-compressed, so the proxy decompresses
**a copy** of the bytes for JSON inspection only. The original compressed
bytes are what get forwarded if the error is not a signature error, keeping
the body consistent with the pass-through `Content-Encoding: gzip` header.

This separation matters: an earlier version decompressed the body in place
and forwarded plain JSON under a header still claiming gzip, which made
Claude Code fail with `Decompression error: ZlibError` on every
non-signature 400. If the gzip body is truncated or corrupt, decompression
errors (`BadGzipFile`, `zlib.error`, `EOFError`) are swallowed and the error
is treated as non-matching — the original bytes are still forwarded
unchanged.
