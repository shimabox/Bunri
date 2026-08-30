from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bunri.pocket.http import PocketHTTPClient, PocketHTTPError


class Handler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, format, *args): pass
    def _record(self, body=b""): type(self).requests.append((self.command, self.path, dict(self.headers), body))
    def do_GET(self):
        self._record(); body = json.dumps({"ok": True}).encode(); self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_HEAD(self):
        self._record(); self.send_response(200); self.send_header("Content-Length", "7"); self.send_header("X-Bunri-Content-SHA256", "a" * 64); self.end_headers()
    def do_PUT(self):
        length = int(self.headers["Content-Length"]); body = self.rfile.read(length); self._record(body)
        self.send_response(201); self.send_header("Content-Length", "0"); self.send_header("ETag", '"stored"'); self.send_header("X-Bunri-Content-SHA256", self.headers["X-Bunri-Content-SHA256"]); self.end_headers()


@pytest.fixture
def server():
    Handler.requests = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler); thread = threading.Thread(target=httpd.serve_forever); thread.start()
    try: yield f"http://127.0.0.1:{httpd.server_port}"
    finally: httpd.shutdown(); thread.join(); httpd.server_close()


def test_media_put_accepts_empty_response_and_verifies_checksum(server, tmp_path):
    payload = b"abcdefg" * 4096
    path = tmp_path / "media.mp3"; path.write_bytes(payload)
    client = PocketHTTPClient(server, "secret", metadata_timeout=1, media_timeout=2)
    assert client.put_media("a" * 12, "guitar.mp3", path, len(payload), "a" * 64) == "a" * 64
    method, _, headers, body = Handler.requests[-1]
    assert method == "PUT" and body == payload and headers["Content-Length"] == str(len(payload))
    assert "Transfer-Encoding" not in headers and headers["Authorization"] == "Bearer secret"


def test_head_metadata_and_timeouts(server):
    client = PocketHTTPClient(server, "secret", metadata_timeout=1.25, media_timeout=8.5)
    assert client.head_media("a" * 12, "guitar.mp3") == ("a" * 64, 7)
    assert client.metadata_timeout == 1.25 and client.media_timeout == 8.5


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_never_followed(server, status, monkeypatch):
    def redirect(self):
        self._record(); self.send_response(status); self.send_header("Location", "/followed"); self.end_headers()
    monkeypatch.setattr(Handler, "do_GET", redirect)
    with pytest.raises(PocketHTTPError) as exc: PocketHTTPClient(server, "secret").capabilities()
    assert exc.value.status == status and len(Handler.requests) == 1 and "secret" not in repr(exc.value)


def test_response_limit(server, monkeypatch):
    def large(self):
        self._record(); body=b"x"*20; self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    monkeypatch.setattr(Handler, "do_GET", large)
    with pytest.raises(PocketHTTPError) as exc: PocketHTTPClient(server, "secret")._request("GET", "x", limit=10)
    assert exc.value.status == 413


def test_unsupported_schema_error_keeps_supported_major(server, monkeypatch):
    def unsupported(self):
        self._record()
        body = json.dumps({"error": {"code": "UNSUPPORTED_SCHEMA_MAJOR", "supported_schema_major": 2}}).encode()
        self.send_response(409); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    monkeypatch.setattr(Handler, "do_GET", unsupported)
    with pytest.raises(PocketHTTPError) as exc:
        PocketHTTPClient(server, "secret").get_json("manifest/aaaaaaaaaaaa")
    assert (exc.value.status, exc.value.code, exc.value.supported_major) == (409, "UNSUPPORTED_SCHEMA_MAJOR", 2)
    assert "secret" not in str(exc.value)
