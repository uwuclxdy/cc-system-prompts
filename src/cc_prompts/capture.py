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
from collections.abc import Callable, Iterator
from pathlib import Path

from .meta import record_capture
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
# the spawn's own verdict on the wire: CC writes a billing header into the
# request's system array and `normalize` strips it from the artifact (MEASURED
# 2026-09-13 against 2.1.266 and 2.1.268: the interactive flavor stamps `cli`,
# the `-p` flavor `sdk-cli`)
MODE_ENTRYPOINT = {"cli": "cli", "sdk": "sdk-cli"}
_ENTRYPOINT = re.compile(r"\bcc_entrypoint=([^;\s]+)")


def validate_entrypoint(system: str, mode: str) -> None:
    """Guard the artifact of record: the prompt flavor the mode asks for.

    Keyed on the billing stamp CC itself writes, never on prompt prose: the
    prose is the tracked variable, so a reworded identity line or a dropped
    block is drift that must reach `captures/` as a diff, not die here. Only a
    stamp positively naming the other flavor means the runner and the wire
    disagree; a missing or renamed stamp cannot discriminate and passes.
    """
    stamped = _ENTRYPOINT.search(system)
    if stamped is None:
        return
    other = next(value for key, value in MODE_ENTRYPOINT.items() if key != mode)
    if stamped.group(1) == other:
        raise RuntimeError(
            f"capture's cc_entrypoint stamp is {stamped.group(1)!r}, the other flavor's; "
            f"the {mode} spawn was probably wired wrong"
        )


def seed_repo(workdir: str) -> None:
    """Make the capture's working directory a git repository of its own.

    Through 2.1.267 the prompt derived lines from the workdir's repo (the
    `gitStatus:` block, `Is a git repository:`), and whether the temp workdir
    landed in one is a property of `TMPDIR`: `/mnt/scratch/tmp` sits under a
    checkout, a CI runner's does not. That turned the whole block into drift at
    every machine boundary, which would flap forever between a local refresh
    and the daily one. Owning the repo fixed those values in place. 2.1.268
    dropped the repo state from the system array (MEASURED 2026-09-13); the
    gitStatus reminder and the session facts ride the request's messages
    instead (MEASURED 2026-09-23 on 2.1.281), outside what a capture records.
    The seed stays as the determinism mechanism for any version that derives
    the system array from the repo again.

    The local identity was the same story one level down: the block's
    `Git user:` line appeared only when git resolved one, so a throwaway-repo
    identity settled it, touching no config outside `workdir`.
    """
    subprocess.run(["git", "init", "-q", workdir], check=True, capture_output=True)
    for key, value in (("user.name", "capture"), ("user.email", "capture@example.invalid")):
        subprocess.run(
            ["git", "-C", workdir, "config", key, value], check=True, capture_output=True
        )


def custom_prompt_text(paths: tuple[Path, ...] = CUSTOM_PROMPT_PATHS) -> str:
    """Read the custom prompt from the first candidate that resolves, or return empty."""
    for path in paths:
        try:
            return path.read_text()
        except OSError:
            continue
    return ""


def custom_markers(system: str, custom: str) -> list[str]:
    """Lines of the custom prompt long enough that only it spells them, found in `system`."""
    return [
        marker
        for line in custom.splitlines()
        if len(marker := line.strip()) >= MIN_CUSTOM_MARKER and marker in system
    ]


def validate_stock(system: str, custom: str) -> None:
    """Guard the artifact of record: a capture must carry the STOCK prompt.

    A shim'd spawn stamps the cli entrypoint like any other interactive one, so
    `validate_entrypoint` passes a shim'd capture unchanged. The custom prompt
    is identity-bearing and `captures/` gets pushed, so match on content: a
    renamed shim, or a `--claude-bin` pointing anywhere else, still spells its
    own bytes into the capture. Inert when `custom` is empty, which is the CI
    case, where no shim exists to guard against.
    """
    found = custom_markers(system, custom)
    if found:
        raise RuntimeError(
            f"capture carries the custom prompt ({found[0][:50]!r}); "
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


def _spawn_env(
    config_dir: str,
    base_url: str,
    model_id: str,
    use_flag: bool,
    extra_env: dict[str, str] | None = None,
    no_dummy_keys: bool = False,
) -> dict[str, str]:
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
    if no_dummy_keys:
        # let the config's stored credentials answer, so a gate only real
        # credentials pass (the OAuth one behind the scratchpad block) can fire
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
    if extra_env:
        # a probe trigger rides a CLAUDE_*/ANTHROPIC_* var the scrub would
        # drop, so --env applies after it
        env |= extra_env
    return env


def has_conversation_request(server: RecorderServer) -> bool:
    return any(_system_size(body) > MIN_SYSTEM_SIZE for body in server.requests)


def _run_interactive(
    binary: str,
    model_id: str,
    config_dir: str,
    workdir: str,
    base_url: str,
    use_flag: bool,
    server: RecorderServer,
    ready: Callable[[RecorderServer], bool] = has_conversation_request,
    timeout: float = ATTEMPT_TIMEOUT,
    extra_env: dict[str, str] | None = None,
    no_dummy_keys: bool = False,
) -> None:
    env = _spawn_env(config_dir, base_url, model_id, use_flag, extra_env, no_dummy_keys)
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
            # Enter answers the TUI's dummy-key dialog with its default, "No
            # (recommended)": the session declines the env api key and falls back
            # to ANTHROPIC_AUTH_TOKEN (MEASURED 2026-09-23 on 2.1.281)
            os.write(fd, b"\r")
            time.sleep(INPUT_WAIT)
            os.write(fd, b"hi\r")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready(server):
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
    ready: Callable[[RecorderServer], bool] = has_conversation_request,
    timeout: float = ATTEMPT_TIMEOUT,
    extra_env: dict[str, str] | None = None,
    no_dummy_keys: bool = False,
) -> None:
    del server, ready  # the subprocess exits on its own; the recorder keeps the body
    env = _spawn_env(config_dir, base_url, model_id, use_flag, extra_env, no_dummy_keys)
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
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            cwd=workdir,  # keep the parent repo's live context out of the prompt
        )


@contextlib.contextmanager
def capture_workspace() -> Iterator[tuple[str, str]]:
    """A throwaway workdir + config dir, onboarded, trusted, and seeded as a repo."""
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
        yield workdir, config_dir


def runner_for(mode: str) -> Callable[..., None]:
    return _run_interactive if mode == "cli" else _run_sdk


def capture_model(binary: str, model_id: str, mode: str) -> str:
    server, port = start_recorder()
    base_url = f"http://127.0.0.1:{port}"
    try:
        with capture_workspace() as (workdir, config_dir):
            runner = runner_for(mode)
            for use_flag in (True, False):
                runner(binary, model_id, config_dir, workdir, base_url, use_flag, server)
                if pick_request(server.requests) is not None:
                    break
        body = pick_request(server.requests)
        if body is None:
            raise RuntimeError(f"no request with a system reached the recorder for {model_id}")
        system = extract_system(body)
        validate_entrypoint(system, mode)
        validate_stock(system, custom_prompt_text())
        return system
    finally:
        stop_recorder(server)


def write_capture(
    out_dir: Path, name: str, model_id: str, version: str, text: str, mode: str
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "-sdk" if mode == "sdk" else ""
    filename = f"{name}{suffix}.md"
    target = out_dir / filename
    target.write_text(normalize(text) + "\n")
    record_capture(out_dir, filename, model_id, version)
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
