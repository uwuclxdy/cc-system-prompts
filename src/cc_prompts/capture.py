"""Capture per-model stock system prompts through a local HTTP recorder.

Two flavors exist, selected client-side by spawn shape (bundle 2.1.239, fn
dii): an interactive (pty) session identifies as Claude Code the CLI; a
non-interactive `claude -p` run identifies as a Claude Agent SDK agent. the
tool captures both, the cli flavor as `<name>.md`, the sdk flavor as
`<name>-sdk.md`.
"""

import argparse
import contextlib
import json
import os
import pty
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

from .normalize import normalize
from .recorder import RecorderServer, start_recorder, stop_recorder

MODELS: dict[str, str] = {
    "opus": "claude-opus-5",
    "fable": "claude-fable-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
    "deepseek-chat": "deepseek-chat",
}

DEFAULT_BIN = os.path.expanduser("~/.local/bin/claude")
# where the custom prompt lives: the path the shim hardcodes, then this checkout
CUSTOM_PROMPT_PATHS = (
    Path(os.path.expanduser("~/.claude/system-prompt/cc-sys.md")),
    Path(__file__).resolve().parents[2] / "cc-sys.md",
)
# calibrated against the 10 committed captures plus one deliberate shim'd capture:
# zero stock false positives at 40 chars and above, 50 of 52 markers hit the shim'd one
MIN_CUSTOM_MARKER = 60
ATTEMPT_TIMEOUT = 90
BOOT_WAIT = 4.0
INPUT_WAIT = 1.0
# conversation prompts run ~20k+ chars; startup helpers stay far below
MIN_SYSTEM_SIZE = 1000
CLI_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude"
SDK_IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK"

MODE_IDENTITY = {"cli": CLI_IDENTITY, "sdk": SDK_IDENTITY}


def validate_identity(system: str, mode: str) -> None:
    """Guard the artifact of record: the prompt flavor the mode asks for.

    The spawn shape decides the identity line (interactive -> cli, `-p` ->
    sdk); a capture carrying the other flavor means the runner and the wire
    disagree. fail fast rather than publish the wrong prompt.
    """
    if MODE_IDENTITY[mode] not in system:
        raise RuntimeError(
            f"capture lacks the {mode} identity line; the spawn was probably the other flavor"
        )


def seed_repo(workdir: str) -> None:
    """Make the capture's working directory a git repository of its own.

    claude stamps a `gitStatus:` block only from inside a repo, and whether the
    temp workdir lands in one is a property of `TMPDIR`: `/mnt/scratch/tmp` sits
    under a checkout, a CI runner's does not. That turned the whole block into
    drift at every machine boundary, which would flap forever between a local
    refresh and the daily one. Owning the repo fixes the block in place; its
    values are normalized away regardless, and an empty repo is enough
    (MEASURED 2026-08-23, `git init` in a repo-free dir under /var/tmp).
    """
    subprocess.run(["git", "init", "-q", workdir], check=True, capture_output=True)


def validate_gitstatus(system: str) -> None:
    """Guard the artifact of record: the `gitStatus:` block `seed_repo` buys.

    A missing block means the seed did not take, and the capture is then not
    comparable with one from any other machine.
    """
    if "gitStatus:" not in system:
        raise RuntimeError("capture carries no gitStatus block; the workdir seed did not take")


def custom_prompt_text(paths: tuple[Path, ...] = CUSTOM_PROMPT_PATHS) -> str:
    """Read the custom prompt from the first candidate that resolves, or return empty."""
    for path in paths:
        try:
            return path.read_text()
        except OSError:
            continue
    return ""


def validate_stock(system: str, custom: str) -> None:
    """Guard the artifact of record: a capture must carry the STOCK prompt.

    `--system-prompt-file` replaces the whole stock prose and leaves the identity
    line standing, so `validate_identity` passes a shim'd capture unchanged. The
    custom prompt is identity-bearing and `captures/` gets pushed, so match on
    content: a renamed shim, or a `--claude-bin` pointing anywhere else, still
    spells its own bytes into the capture. Inert when `custom` is empty, which is
    the CI case, where no shim exists to guard against.
    """
    for line in custom.splitlines():
        marker = line.strip()
        if len(marker) >= MIN_CUSTOM_MARKER and marker in system:
            raise RuntimeError(
                f"capture carries the custom prompt ({marker[:50]!r}); "
                "drive the real launcher, not the shim"
            )


def claude_version(binary: str) -> str:
    result = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, timeout=60, check=False
    )
    match = re.search(r"\d+\.\d+\.\d+", result.stdout)
    if match is None:
        raise RuntimeError(f"no version number in `{binary} --version` output: {result.stdout!r}")
    return match.group(0)


def _system_size(body: dict) -> int:
    system = body.get("system")
    if isinstance(system, str):
        return len(system)
    if isinstance(system, list):
        return sum(len(block.get("text", "")) for block in system)
    return 0


def pick_request(requests: list[dict]) -> dict | None:
    """Request carrying the largest system prompt.

    count_tokens and quota probes carry none; interactive startup may fire
    small helper prompts. the conversation prompt dwarfs both.
    """
    candidates = [body for body in requests if _system_size(body) > 0]
    return max(candidates, key=_system_size, default=None)


def extract_system(body: dict) -> str:
    system = body.get("system", [])
    if isinstance(system, str):
        return system
    return "\n".join(block.get("text", "") for block in system)


def _spawn_env(config_dir: str, base_url: str, model_id: str, use_flag: bool) -> dict[str, str]:
    # ambient CLAUDE_*/ANTHROPIC_* from the parent leaks into the child and
    # fires stray requests for other models; scrub them all and set our own
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CLAUDE_", "ANTHROPIC_"))
    } | {
        "CLAUDE_CONFIG_DIR": config_dir,
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": "dummy",
        "ANTHROPIC_AUTH_TOKEN": "dummy",
        "TERM": "xterm-256color",
    }
    if use_flag:
        env.pop("ANTHROPIC_MODEL", None)
    else:
        # --model rejected client-side: the env var is the fallback transport
        env |= {"ANTHROPIC_MODEL": model_id}
    return env


def _run_interactive(
    binary: str,
    model_id: str,
    config_dir: str,
    workdir: str,
    base_url: str,
    use_flag: bool,
    server: RecorderServer,
) -> None:
    env = _spawn_env(config_dir, base_url, model_id, use_flag)
    args = [binary]
    if use_flag:
        args += ["--model", model_id]

    pid, fd = pty.fork()
    if pid == 0:
        try:
            os.chdir(workdir)
            os.environ.clear()
            os.environ.update(env)
            os.execv(args[0], args)
        except OSError:
            os._exit(127)
    try:
        time.sleep(BOOT_WAIT)
        with contextlib.suppress(OSError):
            # the TUI asks whether to use the dummy env api key; Enter accepts
            os.write(fd, b"\r")
            time.sleep(INPUT_WAIT)
            os.write(fd, b"hi\r")
        deadline = time.monotonic() + ATTEMPT_TIMEOUT
        while time.monotonic() < deadline:
            if any(_system_size(body) > MIN_SYSTEM_SIZE for body in server.requests):
                break
            time.sleep(0.25)
    finally:
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            if os.waitpid(pid, os.WNOHANG)[0]:
                break
            time.sleep(0.2)
        else:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(fd)


def _run_sdk(
    binary: str,
    model_id: str,
    config_dir: str,
    workdir: str,
    base_url: str,
    use_flag: bool,
    server: RecorderServer,
) -> None:
    del server  # the subprocess exits on its own; the recorder keeps the body
    env = _spawn_env(config_dir, base_url, model_id, use_flag)
    cmd = [binary, "-p", "hi"]
    if use_flag:
        cmd += ["--model", model_id]
    # a timeout may still have landed the request; the recorder decides success
    with contextlib.suppress(subprocess.TimeoutExpired):
        subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=ATTEMPT_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
            cwd=workdir,  # keep the parent repo's live context out of the prompt
        )


def capture_model(binary: str, model_id: str, mode: str) -> str:
    server, port = start_recorder()
    base_url = f"http://127.0.0.1:{port}"
    try:
        with tempfile.TemporaryDirectory() as workdir, tempfile.TemporaryDirectory() as config_dir:
            Path(config_dir, ".claude.json").write_text(
                json.dumps(
                    {
                        "hasCompletedOnboarding": True,
                        "projects": {workdir: {"hasTrustDialogAccepted": True}},
                    }
                )
            )
            seed_repo(workdir)
            runner = _run_interactive if mode == "cli" else _run_sdk
            for use_flag in (True, False):
                runner(binary, model_id, config_dir, workdir, base_url, use_flag, server)
                if pick_request(server.requests) is not None:
                    break
        body = pick_request(server.requests)
        if body is None:
            raise RuntimeError(f"no request with a system reached the recorder for {model_id}")
        system = extract_system(body)
        validate_identity(system, mode)
        validate_stock(system, custom_prompt_text())
        validate_gitstatus(system)
        return system
    finally:
        stop_recorder(server)


def write_capture(
    out_dir: Path, name: str, model_id: str, version: str, text: str, mode: str
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    header = f"observed {date.today().isoformat()} (wire capture, CC {version}, {model_id})\n\n"
    suffix = "-sdk" if mode == "sdk" else ""
    target = out_dir / f"{name}{suffix}.md"
    target.write_text(header + normalize(text) + "\n")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="*", default=list(MODELS), help="subset of model names")
    parser.add_argument(
        "--mode",
        choices=("both", "cli", "sdk"),
        default="both",
        help="capture flavor; both writes `<name>.md` and `<name>-sdk.md`",
    )
    parser.add_argument("--out", type=Path, default=Path("captures"), help="output directory")
    parser.add_argument("--claude-bin", default=DEFAULT_BIN, help="claude launcher path")
    args = parser.parse_args(argv)

    unknown = [name for name in args.models if name not in MODELS]
    if unknown:
        parser.error(f"unknown models: {', '.join(unknown)}")

    modes = ("cli", "sdk") if args.mode == "both" else (args.mode,)
    version = claude_version(args.claude_bin)
    failures: list[str] = []
    for name in args.models:
        model_id = MODELS[name]
        for mode in modes:
            try:
                text = capture_model(args.claude_bin, model_id, mode)
            except RuntimeError as err:
                print(f"FAIL {mode} {name}: {err}", file=sys.stderr)
                failures.append(f"{mode} {name}")
                continue
            target = write_capture(args.out, name, model_id, version, text, mode)
            print(f"ok {mode} {name} -> {target}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
