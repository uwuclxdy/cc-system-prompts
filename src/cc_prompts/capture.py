"""Capture per-model stock system prompts through a local HTTP recorder.

The spawn must be interactive (pty): `claude -p` marks the session
non-interactive, and the CLI then identifies as an Agent SDK agent instead of
Claude Code (bundle 2.1.239, fn dii: isNonInteractive -> SDK identity).
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
ATTEMPT_TIMEOUT = 90
BOOT_WAIT = 4.0
INPUT_WAIT = 1.0
# conversation prompts run ~20k+ chars; startup helpers stay far below
MIN_SYSTEM_SIZE = 1000
CLI_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude"


def validate_cli_identity(system: str) -> None:
    """Guard the artifact of record: the stock CLI prompt.

    A non-interactive spawn (claude -p) silently produces the Agent SDK
    flavor instead; upstream could also flip the identity line. fail fast
    rather than publish the wrong prompt.
    """
    if CLI_IDENTITY not in system:
        raise RuntimeError(
            "capture lacks the CLI identity line; the spawn was probably non-interactive"
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


def capture_model(binary: str, model_id: str) -> str:
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
            for use_flag in (True, False):
                _run_interactive(binary, model_id, config_dir, workdir, base_url, use_flag, server)
                if pick_request(server.requests) is not None:
                    break
        body = pick_request(server.requests)
        if body is None:
            raise RuntimeError(f"no request with a system reached the recorder for {model_id}")
        system = extract_system(body)
        validate_cli_identity(system)
        return system
    finally:
        stop_recorder(server)


def write_capture(out_dir: Path, name: str, model_id: str, version: str, text: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    header = f"observed {date.today().isoformat()} (wire capture, CC {version}, {model_id})\n\n"
    target = out_dir / f"{name}.md"
    target.write_text(header + normalize(text) + "\n")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="*", default=list(MODELS), help="subset of model names")
    parser.add_argument("--out", type=Path, default=Path("captures"), help="output directory")
    parser.add_argument("--claude-bin", default=DEFAULT_BIN, help="claude launcher path")
    args = parser.parse_args(argv)

    unknown = [name for name in args.models if name not in MODELS]
    if unknown:
        parser.error(f"unknown models: {', '.join(unknown)}")

    version = claude_version(args.claude_bin)
    failures: list[str] = []
    for name in args.models:
        model_id = MODELS[name]
        try:
            text = capture_model(args.claude_bin, model_id)
        except RuntimeError as err:
            print(f"FAIL {name}: {err}", file=sys.stderr)
            failures.append(name)
            continue
        target = write_capture(args.out, name, model_id, version, text)
        print(f"ok {name} -> {target}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
