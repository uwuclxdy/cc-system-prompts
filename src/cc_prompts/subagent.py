"""Wire-capture a Task subagent's own system prompt.

A rejected request shows what the MAIN loop is sent and nothing about what a
subagent is sent: nothing ever spawns one, so no subagent request exists to
observe. Answering one request with a forged `Agent` tool_use makes the CLI
spawn one for real, and that subagent's own request reaches the same recorder.

Two things come out of that one spawn. The subagent prompt itself is stock and
publishable, so it lands in `captures/` like any other. And driving the SHIM
instead of the launcher answers whether a session's `--system-prompt-file`
reaches its subagents: the shim'd parent is the positive control, without which
a clean subagent is not evidence about inheritance, only about the run.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from .capture import (
    DEFAULT_BIN,
    MIN_SYSTEM_SIZE,
    MODELS,
    _system_size,
    capture_header,
    capture_workspace,
    claude_version,
    custom_markers,
    custom_prompt_text,
    extract_system,
    pick_request,
    runner_for,
)
from .normalize import normalize
from .recorder import RecorderServer, start_recorder, stop_recorder

# the one launcher `capture` refuses, and the only one that gives the
# inheritance question a parent whose prompt the subagent could have inherited
SHIM_BIN = os.path.expanduser("~/.local/shims/claude")
# the spawning tool is `Agent` on the wire as of 2.1.241 and was `Task` before,
# so the name is read back from the request's own tool list rather than assumed
AGENT_TOOL_NAMES = ("Agent", "Task")
# built into every install, so a throwaway CLAUDE_CONFIG_DIR still resolves it
PROBE_AGENT_TYPE = "general-purpose"
PROBE_PROMPT = "cc-prompts subagent probe: reply with the word ok and stop."
# CC stamps this on a subagent's billing header; it is the producer's own verdict
# on which side of the spawn a request came from, so nothing here has to infer it
SUBAGENT_MARK = "cc_is_subagent=true"
PROBE_TIMEOUT = 150.0


def sse_event(payload: dict) -> str:
    return f"event: {payload['type']}\ndata: {json.dumps(payload)}\n\n"


def agent_tool_name(body: dict) -> str | None:
    """The spawning tool's name as this build spells it, from the request's own tool list."""
    names = {tool.get("name") for tool in body.get("tools", [])}
    return next((name for name in AGENT_TOOL_NAMES if name in names), None)


def agent_tool_use_sse(model: str, tool_name: str, subagent_type: str = PROBE_AGENT_TYPE) -> str:
    """A streamed assistant turn whose only content is a call to the spawning tool."""
    tool_input = {
        "description": "probe subagent prompt",
        "prompt": PROBE_PROMPT,
        "subagent_type": subagent_type,
        # the default backgrounds the spawn, and a `-p` parent can exit before a
        # backgrounded subagent has issued its first request
        "run_in_background": False,
    }
    return "".join(
        sse_event(event)
        for event in (
            {
                "type": "message_start",
                "message": {
                    "id": "msg_ccprompts_probe",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_ccprompts_probe",
                    "name": tool_name,
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(tool_input)},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 20},
            },
            {"type": "message_stop"},
        )
    )


def is_subagent_request(body: dict) -> bool:
    return SUBAGENT_MARK in extract_system(body)


def has_subagent_request(server: RecorderServer) -> bool:
    return any(is_subagent_request(body) for body in server.requests)


def pick_subagent_request(requests: list[dict]) -> dict | None:
    return pick_request([body for body in requests if is_subagent_request(body)])


def pick_parent_request(requests: list[dict]) -> dict | None:
    return pick_request([body for body in requests if not is_subagent_request(body)])


class ForgeAgentCall:
    """Answer the first conversation request with an `Agent` tool_use, reject the rest.

    One spawn is the whole probe: firing again would nest subagents and the
    second prompt answers no question the first did not.
    """

    def __init__(self, subagent_type: str = PROBE_AGENT_TYPE) -> None:
        self.subagent_type = subagent_type
        self.fired = False
        self.tools_seen: list[str] = []

    def __call__(self, body: dict) -> str | None:
        if self.fired or not body.get("stream"):
            return None
        if _system_size(body) <= MIN_SYSTEM_SIZE or is_subagent_request(body):
            return None
        tool_name = agent_tool_name(body)
        if tool_name is None:
            # a rename would otherwise surface as "no subagent spawned", which
            # reads like a broken probe rather than a moved target
            self.tools_seen = sorted(str(tool.get("name")) for tool in body.get("tools", []))
            return None
        self.fired = True
        return agent_tool_use_sse(body.get("model", ""), tool_name, self.subagent_type)


def capture_pair(
    binary: str, model_id: str, mode: str, subagent_type: str = PROBE_AGENT_TYPE
) -> tuple[str, str]:
    """Return the parent's system prompt and its subagent's, from one spawn."""
    responder = ForgeAgentCall(subagent_type)
    server, port = start_recorder(responder)
    base_url = f"http://127.0.0.1:{port}"
    try:
        with capture_workspace() as (workdir, config_dir):
            runner = runner_for(mode)
            for use_flag in (True, False):
                runner(
                    binary,
                    model_id,
                    config_dir,
                    workdir,
                    base_url,
                    use_flag,
                    server,
                    ready=has_subagent_request,
                    timeout=PROBE_TIMEOUT,
                )
                if has_subagent_request(server):
                    break
        if responder.tools_seen and not responder.fired:
            raise RuntimeError(
                f"no tool named any of {AGENT_TOOL_NAMES} in this build; "
                f"the request offered {responder.tools_seen}"
            )
        parent = pick_parent_request(server.requests)
        subagent = pick_subagent_request(server.requests)
        if parent is None:
            raise RuntimeError(f"no parent request reached the recorder for {model_id}")
        if subagent is None:
            raise RuntimeError(
                "the forged spawning call produced no subagent request; "
                f"{len(server.requests)} requests reached the recorder"
            )
        return extract_system(parent), extract_system(subagent)
    finally:
        stop_recorder(server)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="opus", choices=list(MODELS))
    parser.add_argument("--mode", default="cli", choices=("cli", "sdk"))
    parser.add_argument(
        "--claude-bin",
        default=DEFAULT_BIN,
        help=f"launcher to drive; {SHIM_BIN} gives the inheritance verdict its control",
    )
    parser.add_argument("--subagent-type", default=PROBE_AGENT_TYPE)
    parser.add_argument("--out", type=Path, help="write the normalized subagent prompt here")
    args = parser.parse_args(argv)

    custom = custom_prompt_text()
    parent, subagent = capture_pair(
        args.claude_bin, MODELS[args.model], args.mode, args.subagent_type
    )
    in_parent = custom_markers(parent, custom)
    in_subagent = custom_markers(subagent, custom)
    print(f"parent   {len(parent):6d} chars, {len(in_parent):3d} custom-prompt markers")
    print(f"subagent {len(subagent):6d} chars, {len(in_subagent):3d} custom-prompt markers")

    # one predicate, two readings: the subagent inherited the parent's custom
    # prompt, and the capture about to be written is not the stock artifact
    if in_subagent:
        print(f"LEAK: the custom prompt reaches the subagent ({in_subagent[0][:50]!r})")
        return 1
    if args.out:
        version = claude_version(args.claude_bin)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        header = capture_header(MODELS[args.model], version, "subagent")
        args.out.write_text(header + normalize(subagent) + "\n")
        print(f"wrote {args.out}")
    if in_parent:
        print("the custom prompt does not reach the subagent")
    elif custom and not args.out:
        # without a shim'd parent there is nothing the subagent could have
        # inherited, so a clean subagent is not evidence about inheritance. a
        # run writing a capture was never asking, so it does not hear about it
        print(f"no inheritance control: {args.claude_bin} left the parent prompt stock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
