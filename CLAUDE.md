# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`claudeRouter` is a local HTTP proxy that sits between Claude Code and AI providers. Claude Code points to it via `ANTHROPIC_BASE_URL=http://localhost:4891`. The proxy handles provider switching, auto-fallback, and header/model-field rewriting transparently.

## Architecture

### Core Design

The proxy exposes an Anthropic-compatible HTTP API on `localhost:4891`. It rewrites outbound requests per-provider:
- `Authorization` / `x-api-key` headers (each provider has different auth)
- The `model` field in the request body (e.g. `claude-sonnet-4-6` → `qwen3.5` for Ollama)
- The target base URL

Streaming (SSE) passes through transparently, except for thinking-block sterilization on non-native providers (see below).

### Provider Chain (priority order)

1. **Anthropic API** — `api.anthropic.com`, requires internet + API key
2. **Remote Ollama** — Tailscale `REMOTE_OLLAMA_IP:11434`, requires Tailscale connectivity
3. **Ollama cloud models** — local Ollama at `localhost:11434` using a cloud-routed model
4. **Local Ollama** — `localhost:11434` with a local model, fully offline fallback

### Components

- **Proxy server** (`proxy.py` or similar) — async Python; handles routing, fallback on rate-limit/network error, health checks, and a control endpoint for on-demand switching
- **Launcher** (`cc`) — shell script or small binary; starts the proxy if not already running, then launches `claude` with `ANTHROPIC_BASE_URL=http://localhost:4891`
- **Shell functions** — `use-anthropic`, `use-remote`, `use-local`, `use-auto` hit the proxy control endpoint; `claude-status` shows active provider and last health check times

### Control Endpoint

The proxy exposes an internal control API (e.g. `POST /control/use/{provider}`, `GET /control/status`) so shell commands can switch providers mid-session without restarting Claude Code.

### Fallback Logic

On startup: probe each provider in chain order, select first healthy one. During a session: on rate-limit error (HTTP 429) or network failure from the active provider, silently retry the request against the next provider in chain.

### Thinking Block Sterilization

Anthropic cryptographically signs extended-thinking blocks; other providers generate unsigned/invalid signatures. If a non-Anthropic provider's thinking blocks enter Claude Code's history, every subsequent request to Anthropic fails with `400 Invalid signature in thinking block`.

Two-layer protection:

1. **Response sterilization** — SSE responses from providers without `native_thinking = true` have their thinking blocks converted to text (`<thinking>…</thinking>`) before reaching Claude Code. This prevents the problem from occurring in future turns.

2. **Auto-retry on 400** — if Anthropic returns the signature error (e.g. history was already corrupted, or `/compact` is run on a broken session), the proxy sterilizes thinking blocks in the request messages and retries transparently. `/compact` + auto-retry is the recovery path: the compacted summary replaces all history with clean text, fully recovering the session.

Set `native_thinking = true` on any provider whose thinking signatures Anthropic will accept (currently only Anthropic itself). All other providers default to `false`.

## Implementation Stack

- **Language**: async Python ≥ 3.11 (uses stdlib `tomllib`)
- **HTTP library**: `aiohttp` — single dep for both server and client; handles SSE streaming natively
- **Project tooling**: `uv` (`uv tool install .` to install; `pip install -e .` also works)
- **Config**: TOML at `~/.config/claudeRouter/config.toml`

## Technical Gotchas (from research)

### Ollama speaks Anthropic API natively (v0.14.0+)
No protocol translation needed. The proxy only rewrites the `model` field name, auth header, and base URL — Ollama understands Anthropic-format requests as-is.

### ANTHROPIC_BASE_URL onboarding bypass bug
Claude Code interactive mode can connect directly to `api.anthropic.com` during first-time onboarding, bypassing `ANTHROPIC_BASE_URL`. Fix: ensure `"hasCompletedOnboarding": true` is set in `~/claude.json`. The `cc` launcher checks and sets this. Issues: [#36998](https://github.com/anthropics/claude-code/issues/36998), [#26935](https://github.com/anthropics/claude-code/issues/26935).

### VS Code extension env inheritance
The VS Code Claude extension reads env from the GUI session, not from `.bashrc`. Set `ANTHROPIC_BASE_URL=http://localhost:4891` in `~/.profile` (takes effect on next login) so the extension picks it up without launching from a terminal.

### Context window requirement
Claude Code requires ≥ 64k token context window. Ollama models used as backends must be configured with `num_ctx` ≥ 65536 or Claude Code will behave erratically.

### SSE mid-stream fallback limitation
Once the proxy begins streaming SSE bytes to the client, provider switching is impossible without replaying the response. Fallback only occurs before the first byte is sent.

### Thinking block signature validation
Anthropic's thinking blocks carry a cryptographic `signature` field tied to their API. Only providers with `native_thinking = true` in config produce signatures Anthropic will accept. See "Thinking Block Sterilization" in Fallback Logic above.

## Similar Projects (for reference)

- **[9router](https://github.com/decolua/9router)** — closest analog, Node.js, 40+ providers, 3-tier fallback, heavier
- **[LiteLLM](https://github.com/BerriAI/litellm)** — Python, 100+ providers, heavy dep tree; could be used as routing backend but overkill here
- **[Olla](https://github.com/thushan/olla)** — Go, Ollama-specific proxy with Anthropic API compat and failover
