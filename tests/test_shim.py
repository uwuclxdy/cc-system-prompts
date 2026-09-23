"""The PATH shim that puts `cc-sys.md` in front of every `claude` spawn.

Driven end to end against a stub `claude` that records its argv, because the
behaviour worth pinning is what reaches the real binary's command line.
"""

import os
import subprocess
from pathlib import Path

import pytest

SHIM = Path(__file__).resolve().parents[1] / "shim" / "claude"
INSTALLED = Path(os.path.expanduser("~/.local/shims/claude"))
PROMPT_BODY = "custom prompt body\nsecond line of the custom prompt\n"


@pytest.fixture
def shim(tmp_path):
    """Run the shim against a stub `claude`; return (argv, handed prompt text)."""
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

    def run(*args, env_extra=None):
        env = {
            "PATH": f"{stub.parent}:/usr/bin:/bin",
            "HOME": str(home),
            "ARGV_OUT": str(argv_out),
            "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        } | (env_extra or {})
        result = subprocess.run(
            [str(SHIM), *args], env=env, cwd=tmp_path, check=True, capture_output=True
        )
        assert result.stdout == b"", "the shim must print nothing of its own"
        argv = argv_out.read_text().splitlines()
        # a passthrough hands the caller's own flag straight on, and that path
        # need not exist, so read back only when argv[1] names a readable file
        handed = ""
        if (
            argv[:1] == ["--system-prompt-file"]
            and Path(argv[1]).is_file()
            and os.access(argv[1], os.R_OK)
        ):
            handed = Path(argv[1]).read_text()
        return argv, handed

    run.home = home
    run.prompt = prompt
    run.runtime = tmp_path / "run"
    return run


def test_shim_prepends_the_flag_and_keeps_the_callers_args(shim):
    argv, _ = shim("-p", "hi", "--", "trailing")
    assert argv[0] == "--system-prompt-file"
    assert argv[2:] == ["-p", "hi", "--", "trailing"]


def test_shim_hands_over_the_prompt_body(shim):
    _, handed = shim("-p", "hi")
    assert handed == PROMPT_BODY


def test_shim_hands_the_prompt_file_itself_over_with_no_merge_dir(shim):
    argv, _ = shim("-p", "hi")
    assert argv[1] == str(shim.prompt)
    assert not (shim.runtime / "cc-sys-prompt").exists()


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


@pytest.mark.parametrize(
    "arg",
    [
        "--system-prompt=/somewhere.md",
        "--system-prompt-file=/somewhere.md",
        "--append-system-prompt=/somewhere.md",
        "--append-system-prompt-file=/somewhere.md",
    ],
)
def test_shim_passes_through_when_the_caller_spells_the_flag_with_a_value(shim, arg):
    argv, _ = shim(arg)
    assert argv == [arg]


def test_shim_ignores_a_prompt_path_from_the_environment(shim, tmp_path):
    decoy = tmp_path / "decoy.md"
    decoy.write_text("a prompt that must not win\n")
    argv, _ = shim("-p", "hi", env_extra={"CLAUDE_SYSTEM_PROMPT_FILE": str(decoy)})
    assert argv[1] == str(shim.prompt)


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


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_shim_fails_open_when_the_prompt_file_is_unreadable(shim):
    shim.prompt.chmod(0)
    argv, _ = shim("-p", "hi")
    assert argv == ["-p", "hi"]


def _recursion_env(shim, tmp_path, path_head):
    """PATH whose first entry still resolves `claude` to the shim itself."""
    stub_dir = shim.home / ".local" / "bin"
    return {
        "PATH": f"{path_head}:{stub_dir}:/usr/bin:/bin",
        "HOME": str(shim.home),
        "ARGV_OUT": str(tmp_path / "argv.txt"),
    }


def test_shim_does_not_exec_itself_when_its_own_dir_is_first_on_path(shim, tmp_path):
    # $HOME moved off the real home, so the old prefix strip removes nothing and
    # `command -v claude` resolves to the shim; the shim must skip itself by
    # identity and exec the stub, not recurse until the timeout kills it
    env = _recursion_env(shim, tmp_path, str(SHIM.parent))
    subprocess.run(
        [str(SHIM), "-p", "hi"], env=env, cwd=tmp_path, capture_output=True, timeout=5, check=True
    )
    argv = (tmp_path / "argv.txt").read_text().splitlines()
    assert argv[0] == "--system-prompt-file"
    assert argv[2:] == ["-p", "hi"]


def test_shim_skips_a_symlink_to_itself_on_path(shim, tmp_path):
    # a `claude` symlink in some other PATH dir points at the shim; `$0` then
    # names that symlink, and the identity skip must still beat the recursion
    link_dir = tmp_path / "links"
    link_dir.mkdir()
    link = link_dir / "claude"
    link.symlink_to(SHIM)
    env = _recursion_env(shim, tmp_path, str(link_dir))
    subprocess.run(
        [str(link), "-p", "hi"], env=env, cwd=tmp_path, capture_output=True, timeout=5, check=True
    )
    argv = (tmp_path / "argv.txt").read_text().splitlines()
    assert argv[0] == "--system-prompt-file"
    assert argv[2:] == ["-p", "hi"]


def test_shim_resolves_a_claude_elsewhere_on_path_before_the_home_fallback(shim, tmp_path):
    # the walk's own job: a `claude` in a PATH dir other than $HOME/.local/bin wins over the
    # fallback, and the shim's own dir first on PATH is still skipped by identity
    other = tmp_path / "elsewhere"
    other.mkdir()
    stub = other / "claude"
    stub.write_text('#!/bin/sh\nprintf "ELSEWHERE\\n%s\\n" "$@" > "$ARGV_OUT"\n')
    stub.chmod(0o755)
    env = _recursion_env(shim, tmp_path, str(SHIM.parent))
    env["PATH"] = f"{SHIM.parent}:{other}:{shim.home / '.local' / 'bin'}:/usr/bin:/bin"
    subprocess.run(
        [str(SHIM), "-p", "hi"], env=env, cwd=tmp_path, capture_output=True, timeout=5, check=True
    )
    lines = (tmp_path / "argv.txt").read_text().splitlines()
    assert lines[0] == "ELSEWHERE"
    assert lines[1] == "--system-prompt-file"


@pytest.mark.skipif(not INSTALLED.exists(), reason="no shim installed on this box")
def test_the_installed_shim_matches_this_checkout():
    assert INSTALLED.read_text() == SHIM.read_text()
