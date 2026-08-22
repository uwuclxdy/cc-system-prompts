"""Stdlib HTTP recorder: dumps each request body, then answers 400.

The CLI constructs its system prompt client-side, so one rejected request per
model is enough to observe the full prompt.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class RecorderServer(ThreadingHTTPServer):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.requests: list[dict] = []
        self.bad_requests = 0


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            self.server.requests.append(json.loads(raw))
        except ValueError:
            self.server.bad_requests += 1
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


def start_recorder() -> tuple[RecorderServer, int]:
    server = RecorderServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def stop_recorder(server: RecorderServer) -> None:
    server.shutdown()
    server.server_close()
