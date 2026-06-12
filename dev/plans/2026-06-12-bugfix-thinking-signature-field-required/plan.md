# Bugfix: thinking-signature recovery misses the "Field required" variant

**Date**: 2026-06-12
**Type**: bugfix
**Symptom (user-reported)**: `API Error: 400 messages.3.content.0.thinking.signature: Field required`

## Summary

A thinking block produced by a non-Anthropic provider leaked into Claude Code's
history. On a later turn that hit Anthropic, Anthropic's request validation
rejected the outbound history with:

```
messages.3.content.0.thinking.signature: Field required
```

The proxy already has machinery to transparently recover from leaked thinking
blocks (sterilize history → retry), but it only triggers on the
`Invalid signature in thinking block` message and is blind to this
`thinking.signature: Field required` variant. So the 400 passed straight through
to the user instead of self-healing.

## Root cause

Anthropic emits **two distinct** errors for a leaked thinking block, depending on
whether the offending block carries a signature field at all:

| Error message | Meaning | How a provider triggers it |
|---|---|---|
| `Invalid signature in thinking block` | signature present, cryptographically wrong | non-native provider emits a fake/empty `signature` |
| `<path>.thinking.signature: Field required` | signature field **absent** | non-native provider emits a thinking block with no `signature` key |

`_is_thinking_signature_error` (`src/clauderouter/router.py:145`) only matches the
first:

```python
def _is_thinking_signature_error(body: dict) -> bool:
    err = body.get("error", {})
    return (
        err.get("type") == "invalid_request_error"
        and "Invalid signature in thinking block" in err.get("message", "")
    )
```

So at `router.py:411` the sterilize-and-retry guard never fires for the
"Field required" case, and the error is returned to Claude Code verbatim.

### Secondary gap (how the block got into history)

Response sterilization is gated on SSE only (`router.py:431`):

```python
sterilize = is_sse and not provider.native_thinking
```

A **non-streaming JSON** `/v1/messages` response from a non-native provider is
never run through `_transform_sse_event`, so its thinking blocks pass through
verbatim into Claude Code's history. SSE is the common path, but this JSON path
is unguarded and is a plausible origin for the leaked block.

## Fix

### Primary (required) — broaden the matcher

Make `_is_thinking_signature_error` match both variants, keying on the validation
shape rather than one exact string:

```python
def _is_thinking_signature_error(body: dict) -> bool:
    err = body.get("error", {})
    if err.get("type") != "invalid_request_error":
        return False
    msg = err.get("message", "")
    return (
        "Invalid signature in thinking block" in msg
        or ("thinking.signature" in msg and "Field required" in msg)
    )
```

With this, the existing retry path at `router.py:411` sterilizes the history and
retries transparently for both error variants. No other control-flow change
needed — `_sterilize_thinking_in_messages` already converts every `thinking`
block in `messages` to a `<thinking>…</thinking>` text block.

### Secondary (optional, defense-in-depth) — sterilize non-SSE responses

Close the origin gap so signature-less thinking blocks can't enter history in the
first place when a non-native provider answers with `stream:false`:

- Detect a non-SSE JSON response from a `native_thinking = false` provider.
- Walk `content[]` in the response body and convert `thinking` blocks to text
  (mirror of `_sterilize_thinking_in_messages`, applied to a response instead of
  a request).
- This changes the current zero-copy passthrough for the JSON path, so it must be
  exception-isolated and only applied when `not provider.native_thinking`.

Recommend landing the primary fix first (small, high-value, self-healing) and
treating the secondary as a follow-up.

## Test surface

Add to `tests/test_max_tokens_clamp.py` (or a new `tests/test_thinking_signature.py`):

1. `_is_thinking_signature_error` returns **True** for an
   `invalid_request_error` body whose message is
   `messages.3.content.0.thinking.signature: Field required`.
2. Still **True** for the existing `Invalid signature in thinking block` message
   (regression guard).
3. **False** for an unrelated `invalid_request_error` (e.g. the max-tokens
   message) and for non-400 / non-validation bodies.
4. `_sterilize_thinking_in_messages` converts a `thinking` block at
   `messages[n].content[0]` to a `<thinking>…</thinking>` text block and leaves
   other block types untouched.

(If the secondary fix is implemented: a test that a non-SSE JSON response with a
thinking block is sterilized for a non-native provider, and passed through
untouched for a native one.)

## Recovery for an already-broken session

Documented behavior holds: run `/compact`. It rewrites history into a clean text
summary, dropping the offending block. With the primary fix, fresh occurrences
self-heal without user intervention.

## Files touched

- `src/clauderouter/router.py` — `_is_thinking_signature_error` (primary);
  response-sterilization in the JSON path (secondary, optional).
- `tests/` — new/extended tests per the test surface above.

### Todo list

- [x] Plan reviewed by orchestrator
- [x] Engineer: broaden `_is_thinking_signature_error` to match the "Field required" variant
- [x] Tester: write new tests per the test surface
- [x] Tester: full suite green
- [x] Reviewer: report received
- [x] Review findings triaged / resolved
- [x] Surfaced to user: no end-to-end integration test of the sterilize-and-retry path (minor, optional tester follow-up)
- [x] Docs-writer: documentation updated
- [ ] Commit created
- [ ] Surfaced: secondary non-SSE response sterilization deferred to follow-up (per plan recommendation)

## Out of scope

- No config schema changes.
- No change to SSE sterilization (`_transform_sse_event`) — it already works.
- No change to the auto-retry-once semantics (`signature_sterilized` guard stays).
