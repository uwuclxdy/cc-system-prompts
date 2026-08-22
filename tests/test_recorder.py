import json
import urllib.error
import urllib.request

from cc_prompts.recorder import start_recorder, stop_recorder


def test_recorder_dumps_body_and_answers_400():
    server, port = start_recorder()
    try:
        body = json.dumps({"model": "x", "system": [{"type": "text", "text": "sys"}]}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/messages", data=body, method="POST"
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as err:
            assert err.code == 400
        else:
            raise AssertionError("expected HTTP 400")
        assert server.requests[0]["model"] == "x"
        assert server.bad_requests == 0
    finally:
        stop_recorder(server)


def test_recorder_counts_undecodable_bodies():
    server, port = start_recorder()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/messages", data=b"not json", method="POST"
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as err:
            assert err.code == 400
        assert server.requests == []
        assert server.bad_requests == 1
    finally:
        stop_recorder(server)
