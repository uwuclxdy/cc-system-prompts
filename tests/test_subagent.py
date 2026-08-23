import json

from cc_prompts.subagent import (
    PROBE_AGENT_TYPE,
    SUBAGENT_MARK,
    ForgeAgentCall,
    agent_tool_name,
    agent_tool_use_sse,
    is_subagent_request,
    pick_parent_request,
    pick_subagent_request,
)


def _body(system: str, *, stream: bool = True, tools: tuple[str, ...] = ("Agent", "Bash")) -> dict:
    return {
        "model": "claude-opus-5",
        "stream": stream,
        "system": [{"type": "text", "text": system}],
        "tools": [{"name": name} for name in tools],
    }


LARGE = "x" * 5000
PARENT = _body(LARGE)
SUBAGENT = _body(f"x-anthropic-billing-header: {SUBAGENT_MARK}\n{LARGE}")


def _events(stream: str) -> list[dict]:
    return [
        json.loads(block.split("data: ", 1)[1]) for block in stream.split("\n\n") if block.strip()
    ]


def test_agent_tool_name_reads_the_name_off_the_request():
    assert agent_tool_name(_body(LARGE, tools=("Agent", "Bash"))) == "Agent"


def test_agent_tool_name_accepts_the_older_spelling():
    assert agent_tool_name(_body(LARGE, tools=("Task", "Bash"))) == "Task"


def test_agent_tool_name_is_none_when_the_build_spells_it_some_third_way():
    # a rename has to surface as a named failure rather than as "nothing spawned"
    assert agent_tool_name(_body(LARGE, tools=("Bash", "Read"))) is None


def test_sse_carries_a_tool_use_for_the_named_tool():
    events = _events(agent_tool_use_sse("claude-opus-5", "Agent"))
    assert [event["type"] for event in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[1]["content_block"] == {
        "type": "tool_use",
        "id": "toolu_ccprompts_probe",
        "name": "Agent",
        "input": {},
    }
    assert events[4]["delta"]["stop_reason"] == "tool_use"


def test_sse_tool_input_names_the_agent_type_and_stays_in_the_foreground():
    events = _events(agent_tool_use_sse("claude-opus-5", "Agent", "Explore"))
    tool_input = json.loads(events[2]["delta"]["partial_json"])
    assert tool_input["subagent_type"] == "Explore"
    # a backgrounded spawn lets a `-p` parent exit before the subagent's request
    assert tool_input["run_in_background"] is False
    assert set(tool_input) >= {"description", "prompt"}


def test_responder_fires_on_the_parent_conversation_request():
    responder = ForgeAgentCall()
    stream = responder(PARENT)
    assert stream is not None
    assert PROBE_AGENT_TYPE in stream
    assert responder.fired


def test_responder_fires_only_once():
    # a second forged call would nest subagents and answer nothing new
    responder = ForgeAgentCall()
    assert responder(PARENT) is not None
    assert responder(PARENT) is None


def test_responder_ignores_the_subagents_own_request():
    responder = ForgeAgentCall()
    assert responder(SUBAGENT) is None
    assert not responder.fired


def test_responder_ignores_startup_helpers_and_unstreamed_requests():
    responder = ForgeAgentCall()
    assert responder(_body("tiny")) is None
    assert responder(_body(LARGE, stream=False)) is None
    assert not responder.fired


def test_responder_records_the_tool_list_when_the_name_moved():
    responder = ForgeAgentCall()
    assert responder(_body(LARGE, tools=("Bash", "Read"))) is None
    assert responder.tools_seen == ["Bash", "Read"]


def test_subagent_requests_are_told_apart_by_the_billing_header():
    assert is_subagent_request(SUBAGENT)
    assert not is_subagent_request(PARENT)


def test_pickers_split_a_mixed_request_log():
    requests = [_body("tiny"), PARENT, SUBAGENT]
    assert pick_parent_request(requests) is PARENT
    assert pick_subagent_request(requests) is SUBAGENT


def test_pickers_return_none_when_their_side_never_arrived():
    assert pick_subagent_request([PARENT]) is None
    assert pick_parent_request([SUBAGENT]) is None
