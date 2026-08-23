"""The PATH shim that puts `cc-sys.md` in front of every `claude` spawn.

Driven end to end against a stub `claude` that records its argv, because the
behaviour worth pinning is what reaches the real binary's command line.
"""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parents[1] / "shim" / "claude"
INSTALLED = Path(os.path.expanduser("~/.local/shims/claude"))
PROMPT_BODY = "custom prompt body\nsecond line of the custom prompt\n"


@pytest.fixture
def shim(tmp_path):
    """Run the shim against a stub `claude`; return (argv, merged prompt text)."""
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    argv_out = tmp_path / "argv.txt"
    stub = home / ".local" / "bin" / "claude"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARGV_OUT"\n')
    stub.chmod(0o755)

    # the shim hardcodes this path off $HOME and ignores the environment, so the
    # only way to steer it is to move $HOME
    prompt = home / ".claude" / "system-prompt" / "cc-sys.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text(PROMPT_BODY)

    def run(*args, cwd=None, env_extra=None):
        env = {
            "PATH": f"{stub.parent}:/usr/bin:/bin",
            "HOME": str(home),
            "SHELL": "/bin/bash",
            "ARGV_OUT": str(argv_out),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
            "CLAUDE_SYSTEM_PROMPT_FILE": "/environment/must/not/win.md",
        } | (env_extra or {})
        result = subprocess.run(
            [str(SHIM), *args], env=env, cwd=cwd or tmp_path, check=True, capture_output=True
        )
        assert result.stdout == b"", "the shim must print nothing of its own"
        argv = argv_out.read_text().splitlines()
        # a passthrough hands the caller's own flag straight on, and that path
        # need not exist, so read back only what the shim itself wrote
        merged = ""
        if argv[:1] == ["--system-prompt-file"] and Path(argv[1]).is_file():
            merged = Path(argv[1]).read_text()
        return argv, merged

    run.home = home
    run.prompt = prompt
    run.runtime = tmp_path / "run"
    return run


def test_shim_prepends_the_flag_and_keeps_the_callers_args(shim):
    argv, _ = shim("-p", "hi", "--", "trailing")
    assert argv[0] == "--system-prompt-file"
    assert argv[2:] == ["-p", "hi", "--", "trailing"]


def test_shim_hands_over_the_prompt_body_before_anything_it_adds(shim):
    _, merged = shim("-p", "hi")
    assert merged.startswith(PROMPT_BODY)


def test_shim_rebuilds_the_environment_block(shim, tmp_path):
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    _, merged = shim("-p", "hi", cwd=workdir)
    # the block reproduces claude's own wording, trailing space included
    assert "\n# Environment\nYou have been invoked in the following environment: \n" in merged
    assert f" - Primary working directory: {workdir}\n" in merged
    assert " - Platform: linux\n" in merged
    assert " - Shell: bash\n" in merged
    assert " - OS Version: Linux " in merged


def test_shim_reports_a_git_repository_from_inside_one(shim, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    _, merged = shim("-p", "hi", cwd=repo)
    assert " - Is a git repository: true\n" in merged


def test_shim_reports_no_git_repository_outside_one(shim):
    # not tmp_path: TMPDIR can itself sit under a repo (it does on this box, and
    # claude answers `true` there), which would make the negative case untestable
    with tempfile.TemporaryDirectory(dir="/var/tmp") as outside:
        if any((Path(p) / ".git").exists() for p in (outside, *Path(outside).parents)):
            pytest.skip("/var/tmp is inside a git repository here")
        _, merged = shim("-p", "hi", cwd=outside)
    assert " - Is a git repository: false\n" in merged


def test_shim_names_the_model_the_caller_spelled(shim):
    _, merged = shim("--model", "claude-opus-5", "-p", "hi")
    assert " - The exact model ID is claude-opus-5.\n" in merged
    _, merged = shim("--model=claude-haiku-4-5-20251001", "-p", "hi")
    assert " - The exact model ID is claude-haiku-4-5-20251001.\n" in merged


def test_shim_omits_the_model_line_when_the_caller_leaves_it_to_the_account(shim):
    _, merged = shim("-p", "hi")
    assert "The exact model ID is" not in merged


@pytest.mark.parametrize(
    "flag",
    [
        "--system-prompt",
        "--system-prompt-file",
        "--append-system-prompt",
        "--append-system-prompt-file",
    ],
)
def test_shim_passes_through_when_the_caller_picked_its_own_prompt(shim, flag):
    argv, _ = shim(flag, "/somewhere/else.md")
    assert argv == [flag, "/somewhere/else.md"]


def test_shim_ignores_a_prompt_path_from_the_environment(shim):
    argv, merged = shim("-p", "hi")
    assert argv[1] != "/environment/must/not/win.md"
    assert merged.startswith(PROMPT_BODY)


def test_shim_fails_open_when_the_prompt_file_is_missing(shim):
    shim.prompt.unlink()
    argv, _ = shim("-p", "hi")
    assert argv == ["-p", "hi"]


def test_shim_fails_open_when_the_prompt_path_is_a_directory(shim):
    # `-r` passes a directory, and claude rejects one; the shim must not hand it over
    shim.prompt.unlink()
    shim.prompt.mkdir()
    argv, _ = shim("-p", "hi")
    assert argv == ["-p", "hi"]


def test_shim_falls_back_to_the_plain_prompt_when_the_merge_cannot_be_written(shim, tmp_path):
    # a runtime dir that cannot hold a directory: the flag still names the prompt
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    argv, merged = shim("-p", "hi", env_extra={"XDG_RUNTIME_DIR": str(blocker)})
    assert argv[0] == "--system-prompt-file"
    assert merged == PROMPT_BODY


def _age(path):
    old = 1_700_000_000
    os.utime(path, (old, old))


def test_shim_prunes_stale_spawn_files(shim):
    shim("-p", "hi")
    spawn_dir = shim.runtime / "cc-sys-prompt"
    stale = spawn_dir / "999999.md"
    stale.write_text("old spawn")
    _age(stale)
    fresh = next(p for p in spawn_dir.iterdir() if p != stale)
    shim("-p", "hi")
    assert not stale.exists()
    assert fresh.exists()


def test_shim_spares_an_old_spawn_file_whose_process_is_alive(shim):
    # a session up longer than the window still owns its prompt file, and whether
    # claude re-reads it after startup is not the shim's to assume
    shim("-p", "hi")
    spawn_dir = shim.runtime / "cc-sys-prompt"
    live = spawn_dir / f"{os.getpid()}.md"
    live.write_text("a long-running session's prompt")
    _age(live)
    shim("-p", "hi")
    assert live.exists()


def test_shim_keeps_its_spawn_directory_private(shim):
    shim("-p", "hi")
    spawn_dir = shim.runtime / "cc-sys-prompt"
    assert spawn_dir.stat().st_mode & 0o077 == 0
    for spawn in spawn_dir.iterdir():
        assert spawn.stat().st_mode & 0o077 == 0


@pytest.mark.skipif(not INSTALLED.exists(), reason="no shim installed on this box")
def test_the_installed_shim_matches_this_checkout():
    assert INSTALLED.read_text() == SHIM.read_text()
