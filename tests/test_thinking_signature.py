"""Tests for thinking-block signature error detection and sterilization."""

import gzip
import zlib

from clauderouter.router import (
    _decompress_all,
    _is_thinking_signature_error,
    _recompress,
    _sterilize_thinking_in_content,
    _sterilize_thinking_in_messages,
    _sterilize_thinking_in_response,
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


# ── _sterilize_thinking_in_content / _sterilize_thinking_in_response ────────


def test_sterilize_content_converts_thinking_block_to_text() -> None:
    content = [
        {"type": "thinking", "thinking": "pondering the question"},
        {"type": "text", "text": "Here is the answer."},
    ]
    new_content = _sterilize_thinking_in_content(content)

    assert new_content[0] == {
        "type": "text",
        "text": "<thinking>\npondering the question\n</thinking>",
    }
    assert new_content[1] == {"type": "text", "text": "Here is the answer."}


def test_sterilize_content_leaves_tool_use_block_untouched() -> None:
    tool_use_block = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "Read",
        "input": {"file_path": "/tmp/foo.txt"},
    }
    content = [
        {"type": "thinking", "thinking": "I should read the file"},
        tool_use_block,
    ]
    new_content = _sterilize_thinking_in_content(content)

    assert new_content[0] == {
        "type": "text",
        "text": "<thinking>\nI should read the file\n</thinking>",
    }
    assert new_content[1] == tool_use_block


def test_sterilize_content_does_not_mutate_original() -> None:
    original_block = {"type": "thinking", "thinking": "original"}
    content = [original_block]

    _sterilize_thinking_in_content(content)

    assert content[0] == {"type": "thinking", "thinking": "original"}


def test_sterilize_response_converts_first_content_block() -> None:
    body = {
        "id": "msg_1",
        "type": "message",
        "content": [
            {"type": "thinking", "thinking": "let me think about this"},
            {"type": "text", "text": "Here is the answer."},
        ],
    }
    sterilized = _sterilize_thinking_in_response(body)

    assert sterilized["content"][0] == {
        "type": "text",
        "text": "<thinking>\nlet me think about this\n</thinking>",
    }
    assert sterilized["content"][1] == {"type": "text", "text": "Here is the answer."}
    # Other top-level fields preserved.
    assert sterilized["id"] == "msg_1"
    assert sterilized["type"] == "message"


def test_sterilize_response_with_tool_use_block_untouched() -> None:
    tool_use_block = {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "Bash",
        "input": {"command": "ls"},
    }
    body = {
        "content": [
            {"type": "thinking", "thinking": "I'll run a command"},
            tool_use_block,
        ],
    }
    sterilized = _sterilize_thinking_in_response(body)

    assert sterilized["content"][0]["type"] == "text"
    assert sterilized["content"][0]["text"] == "<thinking>\nI'll run a command\n</thinking>"
    assert sterilized["content"][1] == tool_use_block


def test_sterilize_response_with_no_thinking_blocks_returns_equivalent_content() -> None:
    body = {
        "content": [
            {"type": "text", "text": "Hello"},
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}},
        ],
    }
    sterilized = _sterilize_thinking_in_response(body)

    assert sterilized["content"] == body["content"]


def test_sterilize_response_with_missing_content_key_returned_unchanged() -> None:
    # body.get("content") is None, not a list, so the function returns the
    # original body unchanged (no content key added).
    body = {"id": "msg_1", "type": "message"}
    sterilized = _sterilize_thinking_in_response(body)
    assert sterilized is body  # identity check: same object
    assert "content" not in sterilized  # no content key injected
    assert sterilized["id"] == "msg_1"
    assert sterilized["type"] == "message"


def test_sterilize_response_with_non_list_content_returned_unchanged() -> None:
    body = {"id": "msg_1", "content": "plain string content"}
    sterilized = _sterilize_thinking_in_response(body)
    assert sterilized == body
    assert sterilized["content"] == "plain string content"


def test_sterilize_response_does_not_mutate_original_body() -> None:
    original_block = {"type": "thinking", "thinking": "original"}
    body = {"content": [original_block]}

    _sterilize_thinking_in_response(body)

    assert body["content"][0] == {"type": "thinking", "thinking": "original"}


# ── _recompress / _decompress_all round-trip ─────────────────────────────────


_SAMPLE = b'{"id": "msg_1", "content": [{"type": "text", "text": "hello world"}]}'


def test_recompress_gzip_round_trips_with_decompress_all() -> None:
    recompressed = _recompress(_SAMPLE, "gzip")
    assert recompressed != _SAMPLE  # actually compressed, not passthrough
    assert _decompress_all(recompressed, "gzip") == _SAMPLE
    # Also valid via stdlib gzip directly.
    assert gzip.decompress(recompressed) == _SAMPLE


def test_recompress_deflate_round_trips_with_decompress_all() -> None:
    recompressed = _recompress(_SAMPLE, "deflate")
    assert recompressed != _SAMPLE
    assert _decompress_all(recompressed, "deflate") == _SAMPLE
    # Also valid via stdlib zlib directly.
    assert zlib.decompress(recompressed) == _SAMPLE


def test_recompress_x_gzip_alias_round_trips() -> None:
    recompressed = _recompress(_SAMPLE, "x-gzip")
    assert _decompress_all(recompressed, "x-gzip") == _SAMPLE
    assert gzip.decompress(recompressed) == _SAMPLE


def test_recompress_empty_encoding_is_passthrough() -> None:
    # No Content-Encoding -> data is returned unchanged (matches _decompress_all's
    # passthrough for unsupported/absent encodings).
    recompressed = _recompress(_SAMPLE, "")
    assert recompressed == _SAMPLE
    assert _decompress_all(recompressed, "") == _SAMPLE


def test_recompress_identity_encoding_is_passthrough() -> None:
    recompressed = _recompress(_SAMPLE, "identity")
    assert recompressed == _SAMPLE
    assert _decompress_all(recompressed, "identity") == _SAMPLE


def test_recompress_empty_body_round_trips_for_gzip_and_deflate() -> None:
    for enc in ("gzip", "deflate"):
        recompressed = _recompress(b"", enc)
        assert _decompress_all(recompressed, enc) == b""
