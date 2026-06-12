"""Tests for bearer auth style + api_key_env resolution (OpenRouter-style provider)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from clauderouter.config import ProviderConfig, load
from clauderouter.providers import Provider


def _make_cfg(*, auth_style: str = "bearer", api_key: str | None = "sk-or-test") -> ProviderConfig:
    return ProviderConfig(
        name="openrouter",
        priority=2,
        base_url="https://openrouter.ai/api",
        auth_style=auth_style,
        models=["claude-sonnet-4-6"],
        api_key=api_key,
    )


# ── apply_auth ───────────────────────────────────────────────────────────────

def test_bearer_sets_authorization_header() -> None:
    p = Provider(_make_cfg())
    headers = p.apply_auth({"Content-Type": "application/json"})
    assert headers["Authorization"] == "Bearer sk-or-test"


def test_bearer_strips_incoming_client_credentials() -> None:
    p = Provider(_make_cfg())
    headers = p.apply_auth({
        "Authorization": "Bearer claude-code-oauth-token",
        "x-api-key": "sk-ant-leaked",
    })
    assert headers["Authorization"] == "Bearer sk-or-test"
    assert "x-api-key" not in headers


def test_bearer_without_key_is_not_ready() -> None:
    p = Provider(_make_cfg(api_key=None))
    assert p.is_ready is False


def test_bearer_with_key_is_ready() -> None:
    p = Provider(_make_cfg())
    assert p.is_ready is True


# ── config.load() resolves api_key_env ───────────────────────────────────────

def _write_cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(body))
    return p


_OPENROUTER_TOML = """
    [server]
    port = 4891

    [[providers]]
    name = "openrouter"
    priority = 2
    base_url = "https://openrouter.ai/api"
    auth_style = "bearer"
    api_key_env = "OPENROUTER_API_KEY"
    health_path = "/v1/models"
    models = ["claude-sonnet-4-6"]

    [providers.model_map]
    "claude-sonnet-4-6" = "z-ai/glm-5"
"""


def test_load_resolves_api_key_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-abc123")
    cfg = load(_write_cfg(tmp_path, _OPENROUTER_TOML))
    pc = cfg.providers[0]
    assert pc.api_key == "sk-or-abc123"
    assert Provider(pc).is_ready is True


def test_load_missing_env_key_warns_but_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = load(_write_cfg(tmp_path, _OPENROUTER_TOML))
    pc = cfg.providers[0]
    assert pc.api_key is None
    assert Provider(pc).is_ready is False


def test_load_openrouter_translates_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-abc123")
    cfg = load(_write_cfg(tmp_path, _OPENROUTER_TOML))
    p = Provider(cfg.providers[0])
    assert p.supports_model("claude-sonnet-4-6") is True
    assert p.translate_model("claude-sonnet-4-6") == "z-ai/glm-5"
    # No prefix matching: unmapped claude models fall through to other providers
    assert p.supports_model("claude-opus-4-8") is False
