"""Capture per-model stock system prompts through a local HTTP recorder."""

import argparse
import contextlib
import os
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from .normalize import normalize
from .recorder import start_recorder, stop_recorder

MODELS: dict[str, str] = {
    "opus": "claude-opus-5",
    "fable": "claude-fable-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
    "deepseek-chat": "deepseek-chat",
}

DEFAULT_BIN = os.path.expanduser("~/.local/bin/claude")
ATTEMPT_TIMEOUT = 90


def claude_version(binary: str) -> str:
    result = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, timeout=60, check=False
    )
    match = re.search(r"\d+\.\d+\.\d+", result.stdout)
    if match is None:
        raise RuntimeError(f"no version number in `{binary} --version` output: {result.stdout!r}")
    return match.group(0)


def _run_once(binary: str, model_id: str, config_dir: str, base_url: str, use_flag: bool) -> None:
    env = os.environ | {
        "CLAUDE_CONFIG_DIR": config_dir,
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": "dummy",
        "ANTHROPIC_AUTH_TOKEN": "dummy",
    }
    cmd = [binary, "-p", "hi"]
    if use_flag:
        # ambient ANTHROPIC_MODEL leaks in from the parent and fires a stray
        # request for a different model; drop it so the capture set is deterministic
        env.pop("ANTHROPIC_MODEL", None)
        cmd += ["--model", model_id]
    else:
        # --model rejected client-side: the env var is the fallback transport
        env |= {"ANTHROPIC_MODEL": model_id}
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
        )


def pick_request(requests: list[dict]) -> dict | None:
    """First request carrying a non-empty system.

    The CLI fires count_tokens (and possibly quota probes) before the real
    messages call; those carry no system and must not win the capture.
    """
    for body in requests:
        system = body.get("system")
        if isinstance(system, str) and system:
            return body
        if isinstance(system, list) and system:
            return body
    return None


def extract_system(body: dict) -> str:
    system = body.get("system", [])
    if isinstance(system, str):
        return system
    return "\n".join(block.get("text", "") for block in system)


def capture_model(binary: str, model_id: str) -> str:
    server, port = start_recorder()
    base_url = f"http://127.0.0.1:{port}"
    try:
        with tempfile.TemporaryDirectory() as config_dir:
            Path(config_dir, ".claude.json").write_text('{"hasCompletedOnboarding": true}')
            for use_flag in (True, False):
                _run_once(binary, model_id, config_dir, base_url, use_flag)
                if server.requests:
                    break
        body = pick_request(server.requests)
        if body is None:
            raise RuntimeError(f"no request with a system reached the recorder for {model_id}")
        return extract_system(body)
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
