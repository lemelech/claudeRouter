# Thinking Block Sterilization

## Why

Anthropic cryptographically signs extended-thinking blocks. Other providers
(Ollama local/remote/cloud) emit thinking blocks with missing or invalid
signatures. Once such a block enters Claude Code's conversation history, every
subsequent request to the real Anthropic API fails with:

```
400 invalid_request_error: Invalid signature in thinking block
```

Because claudeRouter's whole point is switching providers mid-session, a
single turn served by Ollama could otherwise permanently poison the session
for Anthropic. Sterilization prevents that.

## How

Protection is two-layered: prevent corruption going forward, and recover when
history is already corrupted.

### Layer 1 — response sterilization (prevention)

For SSE responses from any provider without `native_thinking = true` in
config, the proxy rewrites thinking-block events into plain text-block events
before they reach Claude Code (`_transform_sse_event` in
`src/clauderouter/router.py`):

- `content_block_start` with `type: thinking` becomes a text block, and a
  synthetic delta injects an opening `<thinking>\n` marker.
- `thinking_delta` events become `text_delta` events carrying the same text.
- `content_block_stop` is preceded by a synthetic `\n</thinking>` delta.

The reasoning content is preserved verbatim for the user; only the block type
changes. Since the blocks stored in history are now ordinary text, Anthropic
never sees an unsigned thinking block on later turns.

Set `native_thinking = true` only on providers whose signatures Anthropic
will accept — currently Anthropic itself. All others default to `false`.

### Layer 2 — auto-retry on 400 (recovery)

If Anthropic still rejects a request with the signature error (history was
corrupted before this feature existed, or `/compact` replays a broken
session), the proxy:

1. Reads the 400 error body from upstream.
2. Checks whether it is specifically the thinking-signature error
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
