# claudeRouter shell integration
# Source this from ~/.bashrc or ~/.zshrc:
#   source /path/to/claudeRouter/shell/claudeRouter.sh

_CLAUDE_ROUTER_URL="http://localhost:4891"

use-anthropic()  { curl -fsS -X POST "${_CLAUDE_ROUTER_URL}/control/use/anthropic"     | _cr_print_mode; }
use-remote()     { curl -fsS -X POST "${_CLAUDE_ROUTER_URL}/control/use/remote-ollama" | _cr_print_mode; }
use-cloud()      { curl -fsS -X POST "${_CLAUDE_ROUTER_URL}/control/use/ollama-cloud"  | _cr_print_mode; }
use-local()      { curl -fsS -X POST "${_CLAUDE_ROUTER_URL}/control/use/local-ollama"  | _cr_print_mode; }
use-auto()       { curl -fsS -X POST "${_CLAUDE_ROUTER_URL}/control/use/auto"          | _cr_print_mode; }

claude-status() {
    local raw
    raw=$(curl -fsS "${_CLAUDE_ROUTER_URL}/control/status" 2>/dev/null) || {
        echo "[claudeRouter] proxy not running (${_CLAUDE_ROUTER_URL})" >&2
        return 1
    }
    python3 - <<EOF
import json, sys
d = json.loads('''${raw}''')
print(f"mode: {d['mode']}")
for p in d['providers']:
    ok  = "✓" if p['healthy'] else "✗"
    ts  = (p['last_check'] or "never")[:19].replace("T"," ")
    err = f"  ({p['last_error']})" if p['last_error'] else ""
    print(f"  {ok} [{p['priority']}] {p['name']:<20}  {ts}{err}")
EOF
}

_cr_print_mode() {
    python3 -c "import json,sys; d=json.load(sys.stdin); print('mode:', d.get('mode','?'))" 2>/dev/null \
        || echo "(no response — is the proxy running?)" >&2
}
