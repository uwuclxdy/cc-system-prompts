import pytest

from cc_prompts.capture import (
    CLI_IDENTITY,
    SDK_IDENTITY,
    _system_size,
    custom_prompt_text,
    extract_system,
    pick_request,
    seed_repo,
    validate_gitstatus,
    validate_identity,
    validate_stock,
    write_capture,
)

CUSTOM_LINE = "an option's text names the action you will take, not just the situation."


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


def test_validate_stock_rejects_a_capture_carrying_the_custom_prompt():
    # `--system-prompt-file` keeps the identity line, so validate_identity passes a
    # shim'd capture; only the body separates the two
    with pytest.raises(RuntimeError, match="custom prompt"):
        validate_stock(f"{CLI_IDENTITY}.\n{CUSTOM_LINE}\n", CUSTOM_LINE)


def test_validate_stock_accepts_a_stock_capture():
    validate_stock(f"{CLI_IDENTITY}.\n# Environment\nplatform: linux\n", CUSTOM_LINE)


def test_validate_stock_ignores_short_custom_lines():
    # cc-sys.md shares headings and one-word lines with the stock prompt; a marker
    # has to be long enough that only the custom prompt spells it
    validate_stock("# Harness\nmarkdown\n", "# Harness\nmarkdown\n")


def test_validate_stock_is_a_noop_when_the_custom_prompt_is_unreadable():
    validate_stock(f"{CLI_IDENTITY}.\n{CUSTOM_LINE}\n", "")


def test_custom_prompt_text_returns_empty_when_no_candidate_exists(tmp_path):
    assert custom_prompt_text((tmp_path / "absent.md",)) == ""


def test_custom_prompt_text_reads_the_first_readable_candidate(tmp_path):
    second = tmp_path / "second.md"
    second.write_text("body")
    assert custom_prompt_text((tmp_path / "absent.md", second)) == "body"


def test_validate_gitstatus_accepts_a_capture_that_carries_the_block():
    validate_gitstatus("preamble\ngitStatus: This is the git status at the start\n")


def test_validate_gitstatus_rejects_a_capture_missing_the_block():
    # whether the workdir lands in a repo is a property of TMPDIR, so a missing
    # block means the seed did not take and the capture is not comparable
    with pytest.raises(RuntimeError, match="gitStatus"):
        validate_gitstatus("preamble\nno block here\n")


def test_seed_repo_makes_the_workdir_its_own_repository(tmp_path):
    seed_repo(str(tmp_path))
    assert (tmp_path / ".git").is_dir()


def test_capture_model_refuses_to_return_a_shimd_capture(monkeypatch):
    # pins the wiring, not just the guard: capture_model is where a shim'd body
    # would otherwise reach write_capture and land in `captures/`
    from cc_prompts import capture as capture_mod

    shimd = f"{SDK_IDENTITY}.\n{CUSTOM_LINE}\ngitStatus: snapshot\n" + "x" * 5000

    def fake_run(binary, model_id, config_dir, workdir, base_url, use_flag, server):
        server.requests.append({"model": model_id, "system": [{"type": "text", "text": shimd}]})

    monkeypatch.setattr(capture_mod, "_run_sdk", fake_run)
    monkeypatch.setattr(capture_mod, "custom_prompt_text", lambda: CUSTOM_LINE)
    with pytest.raises(RuntimeError, match="custom prompt"):
        capture_mod.capture_model("/nonexistent/claude", "claude-opus-5", "sdk")


def test_capture_model_returns_a_stock_capture(monkeypatch):
    # the control: same call path, same scope, a body the guard must NOT refuse
    from cc_prompts import capture as capture_mod

    stock = f"{SDK_IDENTITY}.\n# Environment\ngitStatus: snapshot\n" + "x" * 5000

    def fake_run(binary, model_id, config_dir, workdir, base_url, use_flag, server):
        server.requests.append({"model": model_id, "system": [{"type": "text", "text": stock}]})

    monkeypatch.setattr(capture_mod, "_run_sdk", fake_run)
    monkeypatch.setattr(capture_mod, "custom_prompt_text", lambda: CUSTOM_LINE)
    assert capture_mod.capture_model("/nonexistent/claude", "claude-opus-5", "sdk") == stock


def test_capture_model_refuses_a_capture_with_no_gitstatus(monkeypatch):
    # on this box TMPDIR sits under a checkout, so a dropped seed still yields a
    # block and only a runner would notice; this pins the guard's own wiring
    from cc_prompts import capture as capture_mod

    unseeded = f"{SDK_IDENTITY}.\n# Environment\n" + "x" * 5000

    def fake_run(binary, model_id, config_dir, workdir, base_url, use_flag, server):
        server.requests.append({"model": model_id, "system": [{"type": "text", "text": unseeded}]})

    monkeypatch.setattr(capture_mod, "_run_sdk", fake_run)
    monkeypatch.setattr(capture_mod, "custom_prompt_text", lambda: CUSTOM_LINE)
    with pytest.raises(RuntimeError, match="gitStatus"):
        capture_mod.capture_model("/nonexistent/claude", "claude-opus-5", "sdk")


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
