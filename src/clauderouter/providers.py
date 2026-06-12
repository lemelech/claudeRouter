"""Provider abstraction: auth rewriting, model translation, capability checks."""

from __future__ import annotations

from .config import ProviderConfig


class Provider:
    def __init__(self, cfg: ProviderConfig) -> None:
        self._cfg = cfg

    @property
    def name(self) -> str:
        return self._cfg.name

    @property
    def priority(self) -> int:
        return self._cfg.priority

    @property
    def base_url(self) -> str:
        return self._cfg.base_url

    @property
    def health_path(self) -> str:
        return self._cfg.health_path

    @property
    def extra_body(self) -> dict:
        return self._cfg.extra_body

    @property
    def native_thinking(self) -> bool:
        """True when this provider generates valid Anthropic thinking signatures."""
        return self._cfg.native_thinking

    @property
    def auth_style(self) -> str:
        return self._cfg.auth_style

    @property
    def is_ready(self) -> bool:
        """False if this provider requires an API key that wasn't found in the environment."""
        if self._cfg.auth_style in ("none", "passthrough"):
            return True
        return bool(self._cfg.api_key)

    def probe_model(self) -> str | None:
        """A requested model name to use for a deep health probe (first configured)."""
        if self._cfg.models:
            return self._cfg.models[0]
        if self._cfg.model_map:
            return next(iter(self._cfg.model_map))
        return None

    def supports_model(self, requested: str) -> bool:
        if requested in self._cfg.models or requested in self._cfg.model_map:
            return True
        return any(requested.startswith(p) for p in self._cfg.model_prefixes)

    def translate_model(self, requested: str) -> str:
        return self._cfg.model_map.get(requested, requested)

    def apply_auth(self, headers: dict[str, str]) -> dict[str, str]:
        style = self._cfg.auth_style
        if style == "passthrough":
            return dict(headers)   # forward Claude Code's own OAuth Bearer unchanged
        h = {k: v for k, v in headers.items()
             if k.lower() not in ("authorization", "x-api-key")}
        key = self._cfg.api_key or ""
        if style == "x-api-key":
            h["x-api-key"] = key
        elif style == "bearer":
            h["Authorization"] = f"Bearer {key}"
        # "none" → no auth header added
        return h

    def __repr__(self) -> str:
        return f"Provider({self.name!r}, priority={self.priority})"


def from_configs(cfgs: list[ProviderConfig]) -> list[Provider]:
    return [Provider(c) for c in cfgs]
