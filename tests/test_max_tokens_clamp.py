"""Tests for max_tokens clamp-and-retry on provider output-token-limit 400s."""

from clauderouter.router import _clamp_max_tokens, _max_output_tokens_from_error


def _ollama_error(message: str) -> dict:
    return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}


def test_extracts_limit_from_ollama_max_tokens_error() -> None:
    body = _ollama_error(
        "max_tokens (64000) exceeds model's maximum output tokens (32768) "
        "for model qwen3-coder-next (ref: abc123)"
    )
    assert _max_output_tokens_from_error(body) == 32768


def test_returns_none_for_unrelated_error() -> None:
    body = _ollama_error("Invalid signature in thinking block")
    assert _max_output_tokens_from_error(body) is None


def test_returns_none_for_empty_body() -> None:
    assert _max_output_tokens_from_error({}) is None


def test_clamp_sets_max_tokens() -> None:
    body = {"model": "m", "max_tokens": 64000, "messages": []}
    clamped = _clamp_max_tokens(body, 32768)
    assert clamped["max_tokens"] == 32768
    assert body["max_tokens"] == 64000  # original untouched


def test_clamp_lowers_thinking_budget_below_limit() -> None:
    body = {
        "max_tokens": 64000,
        "thinking": {"type": "enabled", "budget_tokens": 48000},
    }
    clamped = _clamp_max_tokens(body, 32768)
    assert clamped["max_tokens"] == 32768
    assert clamped["thinking"]["budget_tokens"] == 32767


def test_clamp_keeps_thinking_budget_already_below_limit() -> None:
    body = {
        "max_tokens": 64000,
        "thinking": {"type": "enabled", "budget_tokens": 10000},
    }
    clamped = _clamp_max_tokens(body, 32768)
    assert clamped["thinking"]["budget_tokens"] == 10000
