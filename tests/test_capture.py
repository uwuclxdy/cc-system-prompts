from cc_prompts.capture import extract_system, pick_request


def real_body() -> dict:
    return {
        "model": "claude-haiku-4-5-20251001",
        "system": [{"type": "text", "text": "block one"}, {"type": "text", "text": "block two"}],
    }


def test_pick_request_skips_calls_without_a_system():
    requests = [
        {"model": "claude-haiku-4-5-20251001", "messages": [], "tools": []},
        {"model": "glm-4.7", "messages": [], "tools": []},
        real_body(),
    ]
    assert pick_request(requests) is requests[2]


def test_pick_request_returns_none_when_nothing_carries_a_system():
    assert pick_request([{"model": "x"}]) is None
    assert pick_request([]) is None


def test_extract_system_joins_blocks_and_handles_plain_string():
    assert extract_system(real_body()) == "block one\nblock two"
    assert extract_system({"system": "plain"}) == "plain"
    assert extract_system({}) == ""
