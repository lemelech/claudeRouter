"""Tests for thinking-block signature error detection and sterilization."""

from clauderouter.router import (
    _is_thinking_signature_error,
    _sterilize_thinking_in_messages,
)


def _invalid_request_error(message: str) -> dict:
    return {"type": "error", "error": {"type": "invalid_request_error", "message": message}}


def test_matches_field_required_signature_error() -> None:
    body = _invalid_request_error(
        "messages.3.content.0.thinking.signature: Field required"
    )
    assert _is_thinking_signature_error(body) is True


def test_matches_invalid_signature_in_thinking_block() -> None:
    body = _invalid_request_error("Invalid signature in thinking block")
    assert _is_thinking_signature_error(body) is True


def test_does_not_match_unrelated_invalid_request_error() -> None:
    body = _invalid_request_error(
        "max_tokens (64000) exceeds model's maximum output tokens (32768) "
        "for model qwen3-coder-next (ref: abc123)"
    )
    assert _is_thinking_signature_error(body) is False


def test_does_not_match_empty_body() -> None:
    assert _is_thinking_signature_error({}) is False


def test_does_not_match_body_missing_error_key() -> None:
    assert _is_thinking_signature_error({"type": "error"}) is False


def test_does_not_match_different_error_type() -> None:
    body = {
        "type": "error",
        "error": {
            "type": "rate_limit_error",
            "message": "thinking.signature: Field required",
        },
    }
    assert _is_thinking_signature_error(body) is False


def test_does_not_match_partial_field_required_phrase() -> None:
    # Has "thinking.signature" but not "Field required" -> should not match.
    body = _invalid_request_error("messages.0.content.1.thinking.signature is malformed")
    assert _is_thinking_signature_error(body) is False


def test_sterilize_converts_thinking_block_to_text() -> None:
    body = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "let me think about this"},
                    {"type": "text", "text": "Here is the answer."},
                ],
            }
        ]
    }
    sterilized = _sterilize_thinking_in_messages(body)

    content = sterilized["messages"][0]["content"]
    assert content[0] == {
        "type": "text",
        "text": "<thinking>\nlet me think about this\n</thinking>",
    }
    # Other block types are left untouched.
    assert content[1] == {"type": "text", "text": "Here is the answer."}


def test_sterilize_leaves_non_list_content_untouched() -> None:
    body = {"messages": [{"role": "user", "content": "plain string content"}]}
    sterilized = _sterilize_thinking_in_messages(body)
    assert sterilized["messages"][0]["content"] == "plain string content"


def test_sterilize_does_not_mutate_original_body() -> None:
    original_block = {"type": "thinking", "thinking": "original"}
    body = {"messages": [{"role": "assistant", "content": [original_block]}]}

    _sterilize_thinking_in_messages(body)

    # Original message/content/block should be unchanged.
    assert body["messages"][0]["content"][0] == {"type": "thinking", "thinking": "original"}
