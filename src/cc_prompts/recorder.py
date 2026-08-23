"""Stdlib HTTP recorder: dumps each request body, then answers 400.

The CLI constructs its system prompt client-side, so one rejected request per
model is enough to observe the full prompt.

A `responder` lifts that ceiling for the one question a rejected request cannot
answer: what a Task subagent's own prompt looks like. The callback may hand back
a server-sent-event body for a request, and the CLI then acts on it -- a forged
tool_use for the `Agent` tool makes it spawn a subagent, whose own request
reaches this same recorder.
"""

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

Responder = Callable[[dict], str | None]


class RecorderServer(ThreadingHTTPServer):
    def __init__(self, *args: object, responder: Responder | None = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.requests: list[dict] = []
        self.bad_requests = 0
        self.responder = responder


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        body: dict | None = None
        try:
            body = json.loads(raw)
        except ValueError:
            self.server.bad_requests += 1
        if body is not None:
            self.server.requests.append(body)
        stream = self.server.responder(body) if self.server.responder and body else None
        if stream is None:
            self._reject()
            return
        payload = stream.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _reject(self) -> None:
        payload = json.dumps(
            {"type": "error", "error": {"type": "api_error", "message": "capture recorder"}}
        ).encode()
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        # keep the CLI's stderr readable; failures surface via the capture report
        pass


def start_recorder(responder: Responder | None = None) -> tuple[RecorderServer, int]:
    server = RecorderServer(("127.0.0.1", 0), _Handler, responder=responder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def stop_recorder(server: RecorderServer) -> None:
    server.shutdown()
    server.server_close()
