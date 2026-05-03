# claudeRouter

A local proxy that sits between Claude Code and AI providers, enabling seamless mid-session switching and automatic fallback across backends.

## Problem

Claude Code is locked to a single provider per session. If Anthropic rate-limits, the remote machine is unreachable, or there's no internet, you're stuck — you have to restart with a different config.

## Solution

Run a lightweight local HTTP proxy that Claude Code always talks to via `ANTHROPIC_BASE_URL=http://localhost:4891`. The proxy forwards requests to whichever backend is currently active. Switching providers is a shell command — no Claude Code restart required.

## Provider Chain

Priority order for auto-detection and fallback:

1. **Anthropic API** — full Claude models, requires internet + API credits
2. **Remote Ollama** (Tailscale `REMOTE_OLLAMA_IP:11434`) — capable home machine, requires Tailscale connectivity
3. **Ollama cloud models** (local Ollama, cloud-routed model) — requires internet, no Anthropic credits needed
4. **Local Ollama** (`localhost:11434`) — fully offline, always last resort

## Switching

- **Automatic:** on startup, the proxy probes each provider in order and picks the first healthy one
- **Auto-fallback:** if the active provider returns a rate-limit error or network failure mid-session, the proxy silently retries the next in the chain
- **On-demand:** shell commands (`use-anthropic`, `use-remote`, `use-local`, `use-auto`) hit a proxy control endpoint to switch immediately — works mid-session
- **Status:** `claude-status` shows the current active provider and last health check times

## How It Works

Ollama exposes an Anthropic-compatible API, so the proxy is mostly pass-through. Per-provider it rewrites:
- `Authorization` / `x-api-key` headers
- The `model` field in the request body (e.g. `claude-sonnet-4-6` → `qwen3.5`)
- Base URL

Streaming (SSE) is passed through transparently.

## Components

- **Proxy server** — async Python, handles routing, fallback, health checks, and a control endpoint
- **Launcher** (`cc`) — starts the proxy if not running, then launches `claude` with the right env vars
- **Shell integration** — `use-*` switching functions, `claude-status`, auto-start on login (optional)
