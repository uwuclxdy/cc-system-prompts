import pytest

from cc_prompts.capture import (
    CLI_IDENTITY,
    _system_size,
    extract_system,
    pick_request,
    validate_cli_identity,
)


def test_validate_cli_identity_accepts_the_cli_flavor():
    validate_cli_identity(f"preamble\n{CLI_IDENTITY}. More text.")


def test_validate_cli_identity_rejects_the_sdk_flavor():
    with pytest.raises(RuntimeError, match="CLI identity"):
        validate_cli_identity("You are a Claude agent, built on Anthropic's Claude Agent SDK.")


def test_validate_cli_identity_rejects_text_with_no_identity_at_all():
    with pytest.raises(RuntimeError, match="CLI identity"):
        validate_cli_identity("You are an interactive agent that helps with tasks.")


def helper_body() -> dict:
    # interactive startup can fire small system-bearing helper requests
    return {"model": "claude-haiku-4-5-20251001", "system": [{"type": "text", "text": "helper"}]}


def real_body() -> dict:
    return {
        "model": "claude-haiku-4-5-20251001",
        "system": [
            {"type": "text", "text": "billing block"},
            {"type": "text", "text": "x" * 5000},
        ],
    }


def test_system_size_sums_blocks_and_handles_plain_string():
    assert _system_size(real_body()) == 5013
    assert _system_size({"system": "plain"}) == 5
    assert _system_size({"messages": []}) == 0


def test_pick_request_prefers_the_largest_system():
    requests = [{"model": "x", "messages": [], "tools": []}, helper_body(), real_body()]
    assert pick_request(requests) is requests[2]


def test_pick_request_returns_none_when_nothing_carries_a_system():
    assert pick_request([{"model": "x"}]) is None
    assert pick_request([]) is None


def test_extract_system_joins_blocks_and_handles_plain_string():
    assert extract_system(real_body()) == "billing block\n" + "x" * 5000
    assert extract_system({"system": "plain"}) == "plain"
    assert extract_system({}) == ""
