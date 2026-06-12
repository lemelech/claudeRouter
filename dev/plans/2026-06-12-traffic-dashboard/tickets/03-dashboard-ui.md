# 03 — Dashboard UI (static page)

**Depends on**: nothing (build against the `/control/traffic` and `/control/status`
JSON contracts in `plan.md` — write a fixture JSON file for local testing). Route
wiring happens in ticket 05; this ticket delivers the static asset.
**Files**: `src/clauderouter/static/dashboard.html` (new),
`dev/plans/2026-06-12-traffic-dashboard/fixtures/traffic.json` (new, for manual
testing only — not shipped), `tests/test_dashboard_static.py` (new)

## Goal

A single self-contained HTML file (inline `<style>` and `<script>`, **no external
resources / no CDN**) that polls the proxy's status endpoints and renders a live
view of routing + traffic.

## Layout / content

1. **Header bar**: current `mode` (from `/control/status`), and — if `mode ==
   "auto"` — the `effective_provider` from `/control/traffic`, visually
   highlighted (this answers "where is traffic routed to right now"). If mode is
   a forced provider name, show that directly (it *is* the effective provider).
   Show "last updated: Hh:Mm:Ss" from the poll timestamp.

2. **Provider health table** (from `/control/status`, already implemented): name,
   priority, healthy ✓/✗, last check time, last error. Highlight the row matching
   `effective_provider`.

3. **Traffic timeline**: a simple bar chart (canvas or inline SVG, your choice —
   canvas is easier for many bars) of request counts over the recent window,
   bucketed by minute (compute buckets client-side from `entries[].timestamp`),
   stacked/colored by `provider`. Use a small fixed palette (5-6 colors) keyed by
   provider name, with a legend.

4. **Provider / model usage breakdown**: for the entries currently loaded, a small
   table or horizontal bar chart showing, per provider: request count, total
   `usage.input_tokens` + `usage.output_tokens` (sum, skip entries where
   `usage` is `null`), and average `duration_ms`.

5. **Recent requests table**: one row per entry (most-recent-first, as returned),
   columns: time (HH:MM:SS), session (`session.label`), provider, model
   (`requested_model` → `translated_model` if different, else just one), status
   (color 2xx green / 4xx+5xx red), request/response sizes (humanize to KB),
   tokens (`in/out` or "—" if `usage` is null), duration (ms). If `tried` is
   non-empty, show it as a small annotation/tooltip (e.g. "fallback: anthropic →
   openrouter").

## Behavior

- On load, and then on an interval (e.g. every 1.5s via `setInterval`), `fetch`
  both `/control/status` and `/control/traffic` (relative URLs — the dashboard is
  served by the same origin) and re-render. Use `fetch(...).catch(...)` — if a
  poll fails (proxy briefly unreachable), don't crash the page; show a small
  "connection lost, retrying…" indicator and keep the last good render.
- No build step, no `npm`, no framework. Plain HTML/CSS/JS in one file.
- Keep it readable/maintainable — this is a long-lived dev tool, not a one-off.

## Tests (`tests/test_dashboard_static.py`)

Route serving is ticket 05's job, so these tests work directly with the file:

- The file exists at `src/clauderouter/static/dashboard.html` and is non-empty.
- It contains no `<script src=` / `<link href=` pointing at `http(s)://` (i.e. no
  CDN dependencies) — a simple regex/string check is sufficient.
- It references both `/control/status` and `/control/traffic` somewhere in the
  inline script (sanity check that it's wired to the right endpoints).

## Packaging note (flag for ticket 05 / reviewer)

`pyproject.toml` uses `hatchling`. Non-`.py` files under `src/clauderouter/` are
not guaranteed to be included in the built package by default — ticket 05 should
verify `uv tool install .` (or `pip install -e .`) actually makes
`static/dashboard.html` available at runtime relative to the installed package
(e.g. via `importlib.resources` or `Path(__file__).parent / "static" /
"dashboard.html"`), and add a `[tool.hatch.build.targets.wheel.force-include]` (or
similar) entry if needed. For editable/dev installs this is usually fine either
way, but worth a quick check.
