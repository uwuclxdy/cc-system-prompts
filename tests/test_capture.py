from cc_prompts.capture import _system_size, extract_system, pick_request


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
