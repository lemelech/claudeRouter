# claudeRouter

A local proxy that sits between Claude Code and AI providers, enabling seamless mid-session switching and automatic fallback across backends.

## Problem

Claude Code is locked to a single provider per session. If Anthropic rate-limits, the remote machine is unreachable, or there's no internet, you're stuck — you have to restart with a different config.

## Solution

Run a lightweight local HTTP proxy that Claude Code always talks to via `ANTHROPIC_BASE_URL=http://localhost:4891`. The proxy forwards requests to whichever backend is currently active. Switching providers is a shell command — no Claude Code restart required.

## Provider Chain

Priority order for auto-detection and fallback:

1. **Anthropic API** — full Claude models, requires internet + API credits
2. **OpenRouter** (`openrouter.ai/api`) — Anthropic-compatible endpoint; open-weights models that benchmark near Sonnet/Opus at a fraction of the price (DeepSeek V4, GLM-5, …)
3. **Remote Ollama** (Tailscale `REMOTE_OLLAMA_IP:11434`) — capable home machine, requires Tailscale connectivity
4. **Ollama cloud models** (local Ollama, cloud-routed model) — requires internet, no Anthropic credits needed
5. **Local Ollama** (`localhost:11434`) — fully offline, always last resort

## Switching

- **Automatic:** on startup, the proxy probes each provider in order and picks the first healthy one
- **Auto-fallback:** if the active provider returns a rate-limit error or network failure mid-session, the proxy silently retries the next in the chain
- **On-demand:** shell commands (`use-anthropic`, `use-remote`, `use-local`, `use-auto`) hit a proxy control endpoint to switch immediately — works mid-session
- **Status:** `claude-status` shows the current active provider and last health check times

## Dashboard

Open **`http://localhost:4891/dashboard`** in a browser (pin the tab) for a live view of where your traffic is going. It's a single self-contained page — no CDN, no build step — that polls the proxy every ~1.5s.

It shows:
- **Current mode** and, in `auto` mode, the **effective provider** the chain would pick right now
- **Provider health** — priority, healthy/unhealthy, last check time, last error (the effective provider's row is highlighted)
- **Traffic timeline** — request counts bucketed by minute, colored per provider
- **Usage breakdown** — per provider: request count, total input/output tokens, average latency
- **Recent requests** — one row each: time, session, provider, model (`requested → translated` when they differ), status, request/response sizes, tokens, duration, and the fallback chain when a request was retried

### Sessions

Each request is attributed to a **session** = the PID and working directory of the connecting process, resolved once per connection via `/proc`. This is the only OS-level signal that groups a main Claude Code session with its subagent fan-out (they share one connection pool). It's Linux-only and best-effort — anything it can't resolve (non-Linux, IPv6 loopback, permissions, races) shows as `unknown` and never affects proxying.

### Request log

Traffic is held in an in-memory ring buffer (most recent N requests) and also appended to a JSONL file for persistence across restarts — one JSON object per line, handy for `jq`/`grep` after the fact:

```bash
tail -f ~/.local/state/claudeRouter/requests.jsonl | jq .
```

Logging is fully non-blocking and exception-isolated: it never delays or alters a response, and SSE streams are passed through byte-for-byte (token usage is observed in passing, not buffered).

### Configuration

Under `[server]` in `~/.config/claudeRouter/config.toml` (see `config.example.toml`):

| Key | Default | Meaning |
| --- | --- | --- |
| `traffic_log_path` | `~/.local/state/claudeRouter/requests.jsonl` | JSONL persistence file. Set to `""` to disable file writes (ring buffer / dashboard still work). |
| `traffic_log_ring_size` | `500` | Recent requests kept in memory for the dashboard. |
| `traffic_log_queue_size` | `1000` | Max entries buffered before new ones are dropped (under a burst). |

### Endpoints

- `GET /dashboard` — the HTML page above
- `GET /control/traffic` — JSON the page polls: `{ "mode", "effective_provider", "entries": [...] }`, entries most-recent-first. Scrape it directly if you want your own tooling.

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

## Setup

### 1. Install

```bash
git clone <this-repo> ~/claudeRouter
cd ~/claudeRouter
uv tool install .          # puts `claudeRouter` on PATH (~/.local/bin)
# or: pip install -e .     # fallback if you don't have uv
```

### 2. Configure

```bash
mkdir -p ~/.config/claudeRouter
cp config.example.toml ~/.config/claudeRouter/config.toml
```

Edit `~/.config/claudeRouter/config.toml`:
- Fill in the `model_map` entries for each Ollama provider (replace all `"TBD"` values with actual model names you have pulled, e.g. `"qwen3-coder:14b"`)
- Ensure `ANTHROPIC_API_KEY` is set in your environment

### 3. Set environment variables

Add to `~/.profile` so both terminal shells and the VS Code GUI session inherit them:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENROUTER_API_KEY="sk-or-..."   # if using the OpenRouter provider
export ANTHROPIC_BASE_URL="http://localhost:4891"
```

Log out and back in for the VS Code extension to pick these up.

### 4. Start the proxy (systemd user service — recommended)

```bash
mkdir -p ~/.config/systemd/user
cp ~/claudeRouter/systemd/claudeRouter.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now claudeRouter
```

Verify: `curl http://localhost:4891/control/health` should return `ok`.

Logs: `journalctl --user -u claudeRouter -f`

### 5. Shell integration

Add to `~/.bashrc` or `~/.zshrc`:

```bash
source ~/claudeRouter/shell/claudeRouter.sh
```

Symlink the `cc` launcher onto your PATH (optional but convenient):

```bash
ln -sf ~/claudeRouter/bin/cc ~/.local/bin/cc
```

### 6. Use

```bash
cc                   # launches Claude Code via the proxy (also starts proxy if not running)
claude-status        # show active provider + health of all providers
use-anthropic        # force Anthropic for subsequent requests
use-openrouter       # force OpenRouter
use-remote           # force Remote Ollama (Tailscale)
use-cloud            # force Ollama cloud-routed model
use-local            # force local Ollama
use-auto             # back to automatic chain selection
```

## Known Limitations

- **No mid-stream fallback.** Once the proxy starts streaming an SSE response to the client, it cannot switch providers. Provider switching only happens before the first byte is sent.
- **Model names must be pre-configured.** Ollama providers will only receive requests for models listed in their `model_map` in the config. Requests for unlisted models fall through to the next provider.
- **Context window.** Ollama models must be run with `num_ctx >= 65536` or Claude Code will behave erratically.
