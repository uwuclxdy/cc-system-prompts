import json
import subprocess

import pytest

from cc_prompts.capture import (
    _system_size,
    custom_prompt_text,
    extract_system,
    pick_request,
    seed_repo,
    validate_entrypoint,
    validate_stock,
    write_capture,
)

CUSTOM_LINE = "an option's text names the action you will take, not just the situation."
# the billing stamp CC writes into the request's own system array, per flavor
# (MEASURED 2026-09-13 against 2.1.266 and 2.1.268); `normalize` strips it
# from the artifact
CLI_HEADER = "x-anthropic-billing-header: cc_version=2.1.268.e0e; cc_entrypoint=cli;\n"
SDK_HEADER = "x-anthropic-billing-header: cc_version=2.1.268.e0e; cc_entrypoint=sdk-cli;\n"


def test_validate_entrypoint_cli_accepts_the_cli_stamp():
    validate_entrypoint(f"{CLI_HEADER}preamble\nMore text.", "cli")


def test_validate_entrypoint_sdk_accepts_the_sdk_stamp():
    validate_entrypoint(f"{SDK_HEADER}preamble\nMore text.", "sdk")


def test_validate_entrypoint_cli_rejects_the_sdk_stamp():
    # the runner and the wire disagree: a cli spawn produced the `-p` flavor
    with pytest.raises(RuntimeError, match="cc_entrypoint"):
        validate_entrypoint(f"{SDK_HEADER}preamble\n", "cli")


def test_validate_entrypoint_sdk_rejects_the_cli_stamp():
    with pytest.raises(RuntimeError, match="cc_entrypoint"):
        validate_entrypoint(f"{CLI_HEADER}preamble\n", "sdk")


def test_validate_entrypoint_accepts_a_capture_with_no_stamp():
    # a build that stopped writing the header cannot be discriminated; the
    # capture is still a valid artifact and must reach `captures/` as a diff
    validate_entrypoint("preamble\n# Environment\nMore text.\n", "cli")


def test_validate_entrypoint_accepts_a_renamed_stamp():
    # only the OTHER flavor's spelling refuses; an unknown value is a renamed
    # vocabulary, not a disagreement
    validate_entrypoint(
        "x-anthropic-billing-header: cc_version=9.9.9.xxx; cc_entrypoint=terminal;\n", "cli"
    )


def test_validate_stock_rejects_a_capture_carrying_the_custom_prompt():
    # a shim'd spawn stamps the cli entrypoint like any other interactive one,
    # so only the body separates a shim'd capture from a stock one
    with pytest.raises(RuntimeError, match="custom prompt"):
        validate_stock(f"{CLI_HEADER}You are Claude Code.\n{CUSTOM_LINE}\n", CUSTOM_LINE)


def test_validate_stock_accepts_a_stock_capture():
    validate_stock(
        f"{CLI_HEADER}You are Claude Code.\n# Environment\nplatform: linux\n", CUSTOM_LINE
    )


def test_validate_stock_ignores_short_custom_lines():
    # cc-sys.md shares headings and one-word lines with the stock prompt; a marker
    # has to be long enough that only the custom prompt spells it
    validate_stock("# Harness\nmarkdown\n", "# Harness\nmarkdown\n")


def test_validate_stock_is_a_noop_when_the_custom_prompt_is_unreadable():
    validate_stock(f"{CLI_HEADER}You are Claude Code.\n{CUSTOM_LINE}\n", "")


def test_custom_prompt_text_returns_empty_when_no_candidate_exists(tmp_path):
    assert custom_prompt_text((tmp_path / "absent.md",)) == ""


def test_custom_prompt_text_reads_the_first_readable_candidate(tmp_path):
    second = tmp_path / "second.md"
    second.write_text("body")
    assert custom_prompt_text((tmp_path / "absent.md", second)) == "body"


def test_seed_repo_makes_the_workdir_its_own_repository(tmp_path):
    seed_repo(str(tmp_path))
    assert (tmp_path / ".git").is_dir()


def test_seed_repo_gives_the_workdir_a_local_identity(tmp_path):
    seed_repo(str(tmp_path))
    shown = subprocess.run(
        ["git", "-C", str(tmp_path), "config", "--local", "user.name"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert shown.stdout.strip() == "capture"


def test_capture_model_refuses_to_return_a_shimd_capture(monkeypatch):
    # pins the wiring, not just the guard: capture_model is where a shim'd body
    # would otherwise reach write_capture and land in `captures/`
    from cc_prompts import capture as capture_mod

    shimd = f"{SDK_HEADER}You are a Claude agent.\n{CUSTOM_LINE}\n" + "x" * 5000

    def fake_run(binary, model_id, config_dir, workdir, base_url, use_flag, server):
        server.requests.append({"model": model_id, "system": [{"type": "text", "text": shimd}]})

    monkeypatch.setattr(capture_mod, "_run_sdk", fake_run)
    monkeypatch.setattr(capture_mod, "custom_prompt_text", lambda: CUSTOM_LINE)
    with pytest.raises(RuntimeError, match="custom prompt"):
        capture_mod.capture_model("/nonexistent/claude", "claude-opus-5", "sdk")


def test_capture_model_refuses_a_capture_of_the_wrong_flavor(monkeypatch):
    # the wiring for the flavor guard: an sdk-mode capture whose wire stamp
    # says the spawn was interactive must not reach write_capture
    from cc_prompts import capture as capture_mod

    miswired = f"{CLI_HEADER}You are Claude Code.\n# Environment\n" + "x" * 5000

    def fake_run(binary, model_id, config_dir, workdir, base_url, use_flag, server):
        server.requests.append({"model": model_id, "system": [{"type": "text", "text": miswired}]})

    monkeypatch.setattr(capture_mod, "_run_sdk", fake_run)
    monkeypatch.setattr(capture_mod, "custom_prompt_text", lambda: CUSTOM_LINE)
    with pytest.raises(RuntimeError, match="cc_entrypoint"):
        capture_mod.capture_model("/nonexistent/claude", "claude-opus-5", "sdk")


def test_capture_model_returns_a_stock_capture(monkeypatch):
    # the control: same call path, same scope, a body the guards must NOT refuse
    from cc_prompts import capture as capture_mod

    stock = f"{SDK_HEADER}You are a Claude agent.\n# Environment\n" + "x" * 5000

    def fake_run(binary, model_id, config_dir, workdir, base_url, use_flag, server):
        server.requests.append({"model": model_id, "system": [{"type": "text", "text": stock}]})

    monkeypatch.setattr(capture_mod, "_run_sdk", fake_run)
    monkeypatch.setattr(capture_mod, "custom_prompt_text", lambda: CUSTOM_LINE)
    assert capture_mod.capture_model("/nonexistent/claude", "claude-opus-5", "sdk") == stock


def test_capture_model_cli_accepts_the_blockless_2_1_268_shape(monkeypatch):
    # the shape that reddened the daily runs: CC 2.1.268's cli prompt carries no
    # gitStatus block and no machine-context lines (MEASURED 2026-09-13 against
    # the checksum-verified binary); it is a valid stock artifact and must land
    # as a drift diff, not die in a guard
    from cc_prompts import capture as capture_mod

    blockless = (
        f"{CLI_HEADER}You are Claude Code, Anthropic's official CLI for Claude.\n"
        "# Environment\n - The most recent Claude models are the Claude 5 family.\n" + "x" * 5000
    )

    def fake_run(binary, model_id, config_dir, workdir, base_url, use_flag, server):
        server.requests.append({"model": model_id, "system": [{"type": "text", "text": blockless}]})

    monkeypatch.setattr(capture_mod, "_run_interactive", fake_run)
    monkeypatch.setattr(capture_mod, "custom_prompt_text", lambda: CUSTOM_LINE)
    assert capture_mod.capture_model("/nonexistent/claude", "claude-opus-5", "cli") == blockless


def test_capture_model_cli_accepts_a_prompt_with_no_identity_line(monkeypatch):
    # the class, not the instance: the identity line is tracked prose like any
    # other line, so a future release rewording or dropping it must not refuse
    # the capture either
    from cc_prompts import capture as capture_mod

    wordless = (
        "x-anthropic-billing-header: cc_version=9.9.9.xxx; cc_entrypoint=cli;\n"
        "# Environment\n - Some future shape of the prompt.\n" + "x" * 5000
    )

    def fake_run(binary, model_id, config_dir, workdir, base_url, use_flag, server):
        server.requests.append({"model": model_id, "system": [{"type": "text", "text": wordless}]})

    monkeypatch.setattr(capture_mod, "_run_interactive", fake_run)
    monkeypatch.setattr(capture_mod, "custom_prompt_text", lambda: CUSTOM_LINE)
    assert capture_mod.capture_model("/nonexistent/claude", "claude-opus-5", "cli") == wordless


def test_write_capture_names_sdk_files_with_the_suffix(tmp_path):
    target = write_capture(tmp_path, "opus", "claude-opus-5", "2.1.239", "body", "sdk")
    assert target == tmp_path / "opus-sdk.md"
    assert (tmp_path / "opus-sdk.md").read_text() == "body\n"
    meta = json.loads((tmp_path / "meta.json").read_text())
    assert meta["captures"] == {"opus-sdk.md": {"model": "claude-opus-5"}}
    assert meta["version"] == "2.1.239"


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
