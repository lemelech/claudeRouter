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

    def supports_model(self, requested: str) -> bool:
        return requested in self._cfg.models or requested in self._cfg.model_map

    def translate_model(self, requested: str) -> str:
        return self._cfg.model_map.get(requested, requested)

    def apply_auth(self, headers: dict[str, str]) -> dict[str, str]:
        h = {k: v for k, v in headers.items()
             if k.lower() not in ("authorization", "x-api-key")}
        style = self._cfg.auth_style
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
