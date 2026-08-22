import pytest

from cc_prompts.capture import (
    CLI_IDENTITY,
    SDK_IDENTITY,
    _system_size,
    extract_system,
    pick_request,
    validate_identity,
    write_capture,
)


def test_validate_identity_cli_accepts_the_cli_flavor():
    validate_identity(f"preamble\n{CLI_IDENTITY}. More text.", "cli")


def test_validate_identity_cli_rejects_the_sdk_flavor():
    with pytest.raises(RuntimeError, match="cli identity"):
        validate_identity(f"{SDK_IDENTITY}.", "cli")


def test_validate_identity_cli_rejects_text_with_no_identity_at_all():
    with pytest.raises(RuntimeError, match="cli identity"):
        validate_identity("You are an interactive agent that helps with tasks.", "cli")


def test_validate_identity_sdk_accepts_the_sdk_flavor():
    validate_identity(f"preamble\n{SDK_IDENTITY}. More text.", "sdk")


def test_validate_identity_sdk_rejects_the_cli_flavor():
    with pytest.raises(RuntimeError, match="sdk identity"):
        validate_identity(f"{CLI_IDENTITY}.", "sdk")


def test_write_capture_names_sdk_files_with_the_suffix(tmp_path):
    target = write_capture(tmp_path, "opus", "claude-opus-5", "2.1.239", "body", "sdk")
    assert target == tmp_path / "opus-sdk.md"
    assert (
        (tmp_path / "opus-sdk.md")
        .read_text()
        .startswith(
            "observed "  # header; date varies per run
        )
    )


def test_write_capture_names_cli_files_without_a_suffix(tmp_path):
    target = write_capture(tmp_path, "opus", "claude-opus-5", "2.1.239", "body", "cli")
    assert target == tmp_path / "opus.md"


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
