"""Entry point: python -m clauderouter or claudeRouter CLI."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from aiohttp import web

from .config import load as load_config
from .server import create_app


def _setup_logging(level: str, log_file: Path | None) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=2
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="claudeRouter — local AI provider proxy for Claude Code"
    )
    ap.add_argument("--config", type=Path, default=None,
                    help="Path to config.toml (default: ~/.config/claudeRouter/config.toml)")
    ap.add_argument("--port", type=int, default=None,
                    help="Override listen port (default: from config, usually 4891)")
    ap.add_argument("--log-level", default="INFO",
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    ap.add_argument("--no-log-file", action="store_true",
                    help="Disable rotating log file (useful when running under systemd)")
    args = ap.parse_args()

    log_file = (
        None if args.no_log_file
        else Path.home() / ".cache" / "claudeRouter" / "proxy.log"
    )
    _setup_logging(args.log_level, log_file)

    try:
        cfg = load_config(args.config)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.port:
        cfg.server.port = args.port

    app = create_app(cfg)
    web.run_app(app, host="127.0.0.1", port=cfg.server.port,
                access_log=None)


if __name__ == "__main__":
    main()
