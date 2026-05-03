"""Load and validate ~/.config/claudeRouter/config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProviderConfig:
    name: str
    priority: int
    base_url: str
    auth_style: str           # "x-api-key" | "bearer" | "none"
    models: list[str]
    model_map: dict[str, str] = field(default_factory=dict)
    api_key_env: str | None = None
    api_key: str | None = None
    health_path: str = "/api/tags"


@dataclass
class ServerConfig:
    port: int = 4891
    health_interval_secs: int = 30


@dataclass
class Config:
    server: ServerConfig
    providers: list[ProviderConfig]


def _config_path() -> Path:
    if env := os.environ.get("CLAUDEROUTER_CONFIG"):
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "claudeRouter" / "config.toml"


def load(path: Path | str | None = None) -> Config:
    p = Path(path) if path is not None else _config_path()
    if not p.exists():
        raise FileNotFoundError(
            f"Config not found: {p}\n"
            f"Copy config.example.toml to {p} and fill in your values."
        )

    with open(p, "rb") as f:
        raw: dict[str, Any] = tomllib.load(f)

    server = ServerConfig(**{k: v for k, v in raw.get("server", {}).items()
                              if k in ServerConfig.__dataclass_fields__})

    providers: list[ProviderConfig] = []
    seen_names: set[str] = set()
    seen_priorities: set[int] = set()

    for p_raw in raw.get("providers", []):
        name = p_raw.get("name", "")
        if not name:
            raise ValueError("Each provider must have a 'name' field.")
        if name in seen_names:
            raise ValueError(f"Duplicate provider name: {name!r}")
        seen_names.add(name)

        priority = int(p_raw.get("priority", 99))
        if priority in seen_priorities:
            raise ValueError(f"Duplicate provider priority: {priority}")
        seen_priorities.add(priority)

        auth_style = p_raw.get("auth_style", "none")
        if auth_style not in ("x-api-key", "bearer", "none"):
            raise ValueError(
                f"Provider {name!r}: auth_style must be 'x-api-key', 'bearer', or 'none'."
            )

        api_key: str | None = None
        api_key_env: str | None = p_raw.get("api_key_env")
        if api_key_env:
            api_key = os.environ.get(api_key_env)
            if not api_key:
                # Warn but don't crash — other providers may still work
                print(f"[claudeRouter] WARNING: {api_key_env} not set; "
                      f"provider {name!r} will not be usable.")

        providers.append(ProviderConfig(
            name=name,
            priority=priority,
            base_url=p_raw.get("base_url", "").rstrip("/"),
            auth_style=auth_style,
            models=list(p_raw.get("models", [])),
            model_map=dict(p_raw.get("model_map", {})),
            api_key_env=api_key_env,
            api_key=api_key,
            health_path=p_raw.get("health_path", "/api/tags"),
        ))

    providers.sort(key=lambda p: p.priority)
    return Config(server=server, providers=providers)
