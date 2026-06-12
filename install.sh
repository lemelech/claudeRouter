#!/usr/bin/env bash
# install.sh — set up claudeRouter on a new machine
#
# What this does:
#   1. Installs the claudeRouter package via uv tool install
#   2. Installs and enables the systemd user service (Linux only)
#   3. Copies config.example.toml if no config exists yet
#   4. Adds ANTHROPIC_BASE_URL to ~/.profile if missing
#   5. Symlinks bin/cc, bin/ollama-ctx, bin/switch-mode into ~/.local/bin
#   6. Installs /switch-mode slash command into ~/.claude/commands/
#
# Usage:
#   cd ~/claudeRouter && bash install.sh
#
# Re-running is safe (idempotent).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/claudeRouter"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
BIN_DIR="$HOME/.local/bin"
PROFILE="$HOME/.profile"

green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
step()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# ── 1. Install package ────────────────────────────────────────────────────────
step "Installing claudeRouter package via uv"
if ! command -v uv &>/dev/null; then
    echo "Error: uv not found. Install it first: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi
# --editable: the installed tool links back to $REPO_DIR/src, so future
# `git pull`s take effect on the next `systemctl --user restart claudeRouter`
# without needing to re-run this install step.
uv tool install --force --reinstall --editable "$REPO_DIR"
green "  ✓ claudeRouter installed to $BIN_DIR (editable — future code updates apply on service restart)"

# ── 2. Symlink bin scripts ────────────────────────────────────────────────────
step "Linking bin scripts into $BIN_DIR"
mkdir -p "$BIN_DIR"
for script in cc ollama-ctx switch-mode; do
    ln -sf "$REPO_DIR/bin/$script" "$BIN_DIR/$script"
    chmod +x "$REPO_DIR/bin/$script"
    green "  ✓ $BIN_DIR/$script"
done

# ── 2b. Claude Code slash command ────────────────────────────────────────────
CLAUDE_COMMANDS_DIR="$HOME/.claude/commands"
mkdir -p "$CLAUDE_COMMANDS_DIR"
cat > "$CLAUDE_COMMANDS_DIR/switch-mode.md" << 'EOF'
Run the shell command `switch-mode $ARGUMENTS` and show the output.
If no argument is given, run `switch-mode` interactively to display a menu of available providers from the running claudeRouter proxy, then switch to the selected one.
EOF
green "  ✓ Claude slash command: /switch-mode"

# ── 3. Config file ────────────────────────────────────────────────────────────
step "Checking config"
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_DIR/config.toml" ]; then
    cp "$REPO_DIR/config.example.toml" "$CONFIG_DIR/config.toml"
    yellow "  ✎ Created $CONFIG_DIR/config.toml — edit it to set your model mappings"
else
    green "  ✓ Config already exists: $CONFIG_DIR/config.toml"
fi

# ── 4. env vars — write to both ~/.profile (login) and ~/.bashrc (interactive) ─
step "Checking shell env vars"
BASHRC="$HOME/.bashrc"
for rc in "$PROFILE" "$BASHRC"; do
    if ! grep -q "ANTHROPIC_BASE_URL" "$rc" 2>/dev/null; then
        cat >> "$rc" << 'EOF'

# claudeRouter proxy — redirect Claude Code to local provider router
export ANTHROPIC_BASE_URL=http://localhost:4891
EOF
        green "  ✓ Added ANTHROPIC_BASE_URL to $rc"
    else
        green "  ✓ ANTHROPIC_BASE_URL already in $rc"
    fi
done
yellow "  ⚠  Open a new terminal (or run: export ANTHROPIC_BASE_URL=http://localhost:4891) to apply now"

# ── 5. Systemd user service (Linux only) ─────────────────────────────────────
if [[ "$(uname -s)" == "Linux" ]] && command -v systemctl &>/dev/null; then
    step "Installing systemd user service"
    mkdir -p "$SYSTEMD_DIR"
    cp "$REPO_DIR/systemd/claudeRouter.service" "$SYSTEMD_DIR/claudeRouter.service"
    systemctl --user daemon-reload
    systemctl --user enable --now claudeRouter
    green "  ✓ claudeRouter.service enabled and running"
    systemctl --user is-active claudeRouter && true
else
    step "Skipping systemd (not Linux or systemctl not available)"
    yellow "  Start manually: claudeRouter  or  nohup claudeRouter &"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
printf '\n'
green "╔══════════════════════════════════════════╗"
green "║  claudeRouter installed successfully!    ║"
green "╚══════════════════════════════════════════╝"
printf '\n'
echo "Next steps:"
echo "  1. Edit $CONFIG_DIR/config.toml"
echo "     - Fill in remote Ollama model names (replace TBD entries)"
echo "  2. Log out + back in so VS Code picks up ANTHROPIC_BASE_URL"
echo "  3. Run 'cc' instead of 'claude' in the terminal"
echo "  4. From any terminal: switch-mode            (interactive menu)"
echo "     From within Claude: /switch-mode          (slash command)"
echo "     Or directly:        switch-mode <name>    (e.g. switch-mode auto)"
