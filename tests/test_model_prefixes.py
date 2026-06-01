"""Tests for model_prefixes support in ProviderConfig + Provider.supports_model."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from clauderouter.config import ProviderConfig, load
from clauderouter.providers import Provider


def _make_cfg(
    *,
    models: list[str] | None = None,
    model_map: dict[str, str] | None = None,
    model_prefixes: list[str] | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        name="test",
        priority=1,
        base_url="http://localhost",
        auth_style="none",
        models=models or [],
        model_map=model_map or {},
        model_prefixes=model_prefixes or [],
    )


# ── supports_model ───────────────────────────────────────────────────────────

def test_supports_model_exact_match_in_models() -> None:
    p = Provider(_make_cfg(models=["claude-opus-4-7"]))
    assert p.supports_model("claude-opus-4-7") is True


def test_supports_model_match_via_model_map_key() -> None:
    p = Provider(_make_cfg(model_map={"claude-sonnet-4-6": "qwen3:14b"}))
    assert p.supports_model("claude-sonnet-4-6") is True


def test_supports_model_prefix_match() -> None:
    p = Provider(_make_cfg(model_prefixes=["claude-"]))
    assert p.supports_model("claude-opus-4-8") is True
    assert p.supports_model("claude-opus-5-0") is True
    assert p.supports_model("claude-new-model-99") is True


def test_supports_model_no_match_returns_false() -> None:
    p = Provider(_make_cfg(models=["claude-opus-4-7"]))
    assert p.supports_model("gpt-4") is False
    assert p.supports_model("claude-opus-4-8") is False  # not in models, no prefix


def test_supports_model_empty_model_prefixes_default() -> None:
    p = Provider(_make_cfg(models=["claude-opus-4-7"]))
    # default model_prefixes is [] — provider still rejects unknown
    assert p.supports_model("anything-else") is False


def test_supports_model_prefix_does_not_match_unrelated() -> None:
    p = Provider(_make_cfg(model_prefixes=["claude-"]))
    assert p.supports_model("gpt-4") is False
    assert p.supports_model("llama3:8b") is False
    assert p.supports_model("") is False


def test_supports_model_multiple_prefixes() -> None:
    p = Provider(_make_cfg(model_prefixes=["claude-", "gpt-"]))
    assert p.supports_model("claude-opus-9-9") is True
    assert p.supports_model("gpt-5-turbo") is True
    assert p.supports_model("llama3:8b") is False


# ── config.load() parses model_prefixes ──────────────────────────────────────

def _write_cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(body))
    return p


def test_load_parses_model_prefixes_when_present(tmp_path: Path) -> None:
    p = _write_cfg(tmp_path, """
        [server]
        port = 4891

        [[providers]]
        name = "anthropic"
        priority = 1
        base_url = "https://api.anthropic.com"
        auth_style = "passthrough"
        models = ["claude-opus-4-7"]
        model_prefixes = ["claude-"]
    """)
    cfg = load(p)
    assert len(cfg.providers) == 1
    assert cfg.providers[0].model_prefixes == ["claude-"]


def test_load_model_prefixes_absent_defaults_to_empty(tmp_path: Path) -> None:
    p = _write_cfg(tmp_path, """
        [server]
        port = 4891

        [[providers]]
        name = "anthropic"
        priority = 1
        base_url = "https://api.anthropic.com"
        auth_style = "passthrough"
        models = ["claude-opus-4-7"]
    """)
    cfg = load(p)
    assert cfg.providers[0].model_prefixes == []


def test_load_then_supports_model_end_to_end(tmp_path: Path) -> None:
    p = _write_cfg(tmp_path, """
        [server]
        port = 4891

        [[providers]]
        name = "anthropic"
        priority = 1
        base_url = "https://api.anthropic.com"
        auth_style = "passthrough"
        models = ["claude-opus-4-7"]
        model_prefixes = ["claude-"]

        [[providers]]
        name = "ollama"
        priority = 2
        base_url = "http://localhost:11434"
        auth_style = "none"
        models = ["claude-sonnet-4-6"]
    """)
    cfg = load(p)
    anthropic = Provider(cfg.providers[0])
    ollama = Provider(cfg.providers[1])

    # Anthropic accepts any future claude-* model via prefix
    assert anthropic.supports_model("claude-opus-4-8") is True
    assert anthropic.supports_model("claude-opus-99-x") is True
    # Ollama, with no model_prefixes, still rejects unknown
    assert ollama.supports_model("claude-opus-4-8") is False
    assert ollama.supports_model("claude-sonnet-4-6") is True


# ── translate_model unchanged when only prefix matches ───────────────────────

def test_translate_model_returns_request_unchanged_when_no_map_entry() -> None:
    p = Provider(_make_cfg(model_prefixes=["claude-"]))
    # No model_map entry → pass through (correct for Anthropic passthrough auth)
    assert p.translate_model("claude-opus-4-8") == "claude-opus-4-8"
