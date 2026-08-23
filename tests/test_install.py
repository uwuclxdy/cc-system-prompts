import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from cc_prompts.install import install_binary

BINARY = b"a claude-shaped binary\n"
VERSION = "2.1.239"
PLATFORM = "linux-x64"


def _serve(binary: bytes, digest: str) -> tuple[str, dict[str, int]]:
    """A fake release CDN serving one version's manifest and binary."""
    hits = {"manifest": 0, "binary": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == f"/{VERSION}/manifest.json":
                hits["manifest"] += 1
                body = json.dumps({"platforms": {PLATFORM: {"checksum": digest}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == f"/{VERSION}/{PLATFORM}/claude":
                hits["binary"] += 1
                body = binary
                self.send_response(200)
            else:
                body = b"not here"
                self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.server_port}", hits


@pytest.fixture()
def fixed_platform(monkeypatch):
    monkeypatch.setattr("cc_prompts.install.detect_platform", lambda: PLATFORM)


def test_install_downloads_verifies_and_caches(fixed_platform, tmp_path):
    digest = hashlib.sha256(BINARY).hexdigest()
    base_url, hits = _serve(BINARY, digest)

    first = install_binary(VERSION, tmp_path, base_url)

    assert first == tmp_path / VERSION
    assert first.read_bytes() == BINARY
    assert first.stat().st_mode & 0o111
    second = install_binary(VERSION, tmp_path, base_url)
    assert second == first
    assert hits["binary"] == 1


def test_install_redownloads_a_tampered_cache(fixed_platform, tmp_path):
    digest = hashlib.sha256(BINARY).hexdigest()
    base_url, hits = _serve(BINARY, digest)
    install_binary(VERSION, tmp_path, base_url)
    (tmp_path / VERSION).write_bytes(b"tampered")

    install_binary(VERSION, tmp_path, base_url)

    assert (tmp_path / VERSION).read_bytes() == BINARY
    assert hits["binary"] == 2


def test_install_refuses_a_binary_that_fails_verification(fixed_platform, tmp_path):
    digest = hashlib.sha256(b"what the manifest promises").hexdigest()
    base_url, _ = _serve(BINARY, digest)

    with pytest.raises(RuntimeError, match="checksum verification"):
        install_binary(VERSION, tmp_path, base_url)

    assert not (tmp_path / VERSION).exists()


def test_install_reports_a_platform_the_manifest_lacks(fixed_platform, monkeypatch, tmp_path):
    monkeypatch.setattr("cc_prompts.install.detect_platform", lambda: "plan9-x64")
    base_url, _ = _serve(BINARY, hashlib.sha256(BINARY).hexdigest())

    with pytest.raises(RuntimeError, match="no manifest entry"):
        install_binary(VERSION, tmp_path, base_url)


def test_install_rejects_a_non_release_version(tmp_path):
    with pytest.raises(RuntimeError, match="not a release version"):
        install_binary("2.1.239/../../etc", tmp_path, "http://127.0.0.1:1")
