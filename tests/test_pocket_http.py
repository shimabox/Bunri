from __future__ import annotations

import http.client
import json
import socketserver
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bunri import __version__ as bunri_version
from bunri.pocket.http import PocketHTTPClient, PocketHTTPError
from pocket_fake import error_envelope, running_fake_pocket


def _clear_proxy_environment(monkeypatch):
    for name in (
        "http_proxy",
        "HTTP_PROXY",
        "https_proxy",
        "HTTPS_PROXY",
        "all_proxy",
        "ALL_PROXY",
        "no_proxy",
        "NO_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_opener_uses_only_the_https_environment_proxy(monkeypatch):
    captured = []

    def build_opener(*handlers):
        captured.extend(handlers)
        return object()

    _clear_proxy_environment(monkeypatch)
    monkeypatch.setenv("http_proxy", "http://http-proxy.invalid:8080")
    monkeypatch.setenv("https_proxy", "http://https-proxy.invalid:8443")
    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    PocketHTTPClient("https://example.invalid", "secret")

    proxy_handlers = [
        handler for handler in captured
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {
        "https": "http://https-proxy.invalid:8443"
    }


class _ConnectRecordingHandler(socketserver.BaseRequestHandler):
    received: list[bytes] = []

    def handle(self):
        chunks = []
        self.request.settimeout(2)
        while True:
            chunk = self.request.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\r\n\r\n" in b"".join(chunks):
                break
        type(self).received.append(b"".join(chunks))
        self.request.sendall(
            b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )


def test_https_proxy_receives_connect_without_authorization(monkeypatch):
    proxy = socketserver.ThreadingTCPServer(
        ("127.0.0.1", 0), _ConnectRecordingHandler
    )
    proxy.daemon_threads = True
    thread = threading.Thread(target=proxy.serve_forever)
    thread.start()
    _ConnectRecordingHandler.received = []
    try:
        _clear_proxy_environment(monkeypatch)
        monkeypatch.setenv(
            "https_proxy", f"http://127.0.0.1:{proxy.server_address[1]}"
        )

        client = PocketHTTPClient("https://example.invalid", "secret-token")
        with pytest.raises(OSError):
            client.capabilities()
    finally:
        proxy.shutdown()
        thread.join()
        proxy.server_close()

    assert len(_ConnectRecordingHandler.received) == 1
    request = _ConnectRecordingHandler.received[0]
    assert request.startswith(b"CONNECT example.invalid:443 HTTP/")
    assert b"authorization:" not in request.lower()
    assert b"secret-token" not in request


@pytest.mark.parametrize("failure_point", ["open", "response_read", "error_read"])
def test_http_protocol_exceptions_are_converted_to_safe_os_errors(failure_point):
    class BrokenResponse:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            raise http.client.IncompleteRead(b"partial")

    class BrokenErrorBody:
        def read(self, _limit):
            raise http.client.RemoteDisconnected("private transport detail")

        def close(self):
            pass

    class BrokenOpener:
        def open(self, request, timeout):
            if failure_point == "open":
                raise http.client.BadStatusLine("private status detail")
            if failure_point == "error_read":
                raise urllib.error.HTTPError(
                    request.full_url,
                    500,
                    "private reason",
                    {},
                    BrokenErrorBody(),
                )
            return BrokenResponse()

    client = PocketHTTPClient(
        "https://example.invalid", "secret-token", opener=BrokenOpener()
    )
    with pytest.raises(OSError) as caught:
        client._request("GET", "capabilities")

    assert str(caught.value).startswith("Pocket の応答を解釈できません: ")
    assert "private" not in str(caught.value)
    assert "secret-token" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


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
    def do_DELETE(self):
        self._record(); self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers()


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


def test_requests_identify_the_client_by_name(server):
    client = PocketHTTPClient(server, "secret", metadata_timeout=1)
    client.capabilities()
    _, _, headers, _ = Handler.requests[-1]
    assert headers["User-Agent"] == f"bunri/{bunri_version}"
    assert not headers["User-Agent"].startswith("Python-urllib")


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


def test_delete_track_uses_api_route_bearer_user_agent_no_body_and_30_second_timeout(server):
    class RecordingOpener:
        def __init__(self):
            self.timeout = None

        def open(self, request, timeout):
            self.timeout = timeout
            return __import__("urllib.request").request.urlopen(request, timeout=timeout)

    opener = RecordingOpener()
    PocketHTTPClient(server, "delete-secret", opener=opener, metadata_timeout=1).delete_track("abcdef123456")
    method, path, headers, body = Handler.requests[-1]
    assert (method, path, body) == ("DELETE", "/api/v1/tracks/abcdef123456", b"")
    assert headers["Authorization"] == "Bearer delete-secret"
    assert headers["User-Agent"] == f"bunri/{bunri_version}"
    assert "Origin" not in headers and "Cookie" not in headers and "Content-Length" not in headers
    assert opener.timeout == 30


@pytest.mark.parametrize("status", [401, 404, 409, 422, 429, 503])
def test_delete_track_rejects_every_non_204_without_disclosing_secret(server, monkeypatch, status):
    def rejected(self):
        self._record()
        body = json.dumps({"error": {"code": "REJECTED"}}).encode()
        self.send_response(status)
        self.send_header("Retry-After", "tomorrow" if status == 429 else "")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    monkeypatch.setattr(Handler, "do_DELETE", rejected)
    with pytest.raises(PocketHTTPError) as caught:
        PocketHTTPClient(server, "delete-secret").delete_track("abcdef123456")
    assert caught.value.status == status
    assert "delete-secret" not in str(caught.value)
    assert "http://" not in str(caught.value)
    if status == 429:
        assert caught.value.retry_after is None


# --------------------------------------------------------------------------
# Viewing-password reset and the public KDF parameters
# --------------------------------------------------------------------------

RESET_BODY = b'{"kdf":{"name":"PBKDF2-HMAC-SHA256","iterations":600000,"salt":"EBESExQVFhcYGRobHB0eHw","dk_len":32},"derived_key":"ZDdrEqwT6KETy2izxW2uK25FVChkKDdx38yVDdttiOI"}'


@pytest.fixture
def pocket():
    with running_fake_pocket() as fake:
        yield fake


def test_reset_password_sends_the_contract_request_and_accepts_204(pocket):
    client = PocketHTTPClient(pocket.url, "reset-secret", metadata_timeout=5)
    assert client.reset_password(RESET_BODY) is None
    (request,) = pocket.requests
    assert (request.method, request.path, request.body) == ("POST", "/api/v1/password/reset", RESET_BODY)
    assert request.header("Authorization") == "Bearer reset-secret"
    assert request.header("User-Agent") == f"bunri/{bunri_version}"
    assert request.header("Content-Type") == "application/json"
    assert request.header("Content-Length") == str(len(RESET_BODY))
    assert request.header("Origin") is None and request.header("Cookie") is None


@pytest.mark.parametrize(
    ("status", "body"),
    [(200, b""), (200, b'{"ok":true}'), (201, b"")],
)
def test_reset_password_treats_any_other_2xx_as_unexpected(pocket, status, body):
    pocket.reset_reply = (status, {}, body)
    with pytest.raises(PocketHTTPError) as caught:
        PocketHTTPClient(pocket.url, "reset-secret").reset_password(RESET_BODY)
    assert (caught.value.status, caught.value.code) == (status, "UNEXPECTED_RESPONSE")
    assert "reset-secret" not in str(caught.value) and "http://" not in str(caught.value)


def test_reset_password_reports_an_oversized_response(pocket, monkeypatch):
    import bunri.pocket.http as http_module

    monkeypatch.setattr(http_module, "JSON_LIMIT_BYTES", 16)
    pocket.reset_reply = (200, {}, b"x" * 17)
    with pytest.raises(PocketHTTPError) as caught:
        PocketHTTPClient(pocket.url, "reset-secret").reset_password(RESET_BODY)
    assert (caught.value.status, caught.value.code) == (413, "RESPONSE_TOO_LARGE")
    assert len(pocket.requests) == 1


@pytest.mark.parametrize(
    ("status", "code", "headers", "retry_after"),
    [
        (401, "UNAUTHORIZED", {}, None),
        (403, "ORIGIN_MISMATCH", {}, None),
        (404, "NOT_FOUND", {}, None),
        (422, "UNSUPPORTED_KDF", {}, None),
        (503, "AUTH_UNAVAILABLE", {}, None),
        (429, "RATE_LIMITED", {"Retry-After": "42"}, "42"),
    ],
)
def test_reset_password_maps_the_error_envelope(pocket, status, code, headers, retry_after):
    pocket.reset_reply = (status, headers, error_envelope(code))
    with pytest.raises(PocketHTTPError) as caught:
        PocketHTTPClient(pocket.url, "reset-secret").reset_password(RESET_BODY)
    assert (caught.value.status, caught.value.code, caught.value.retry_after) == (status, code, retry_after)
    assert "reset-secret" not in str(caught.value)


def test_reset_password_does_not_follow_a_redirect(pocket):
    pocket.reset_reply = (302, {"Location": "/api/v1/auth/params"}, b"")
    with pytest.raises(PocketHTTPError) as caught:
        PocketHTTPClient(pocket.url, "reset-secret").reset_password(RESET_BODY)
    assert caught.value.status == 302
    assert [(item.method, item.path) for item in pocket.requests] == [("POST", "/api/v1/password/reset")]


def test_reset_password_timeout_is_an_os_error(pocket):
    pocket.hold_reset = threading.Event()
    client = PocketHTTPClient(pocket.url, "reset-secret", metadata_timeout=0.3)
    with pytest.raises(OSError) as caught:
        client.reset_password(RESET_BODY)
    assert not isinstance(caught.value, PocketHTTPError)
    assert "reset-secret" not in str(caught.value)


def test_reset_password_refuses_an_oversized_body_before_sending():
    sent = []

    class Recording:
        def open(self, request, timeout):
            sent.append(len(request.data))
            raise OSError("stopped by the test")

    client = PocketHTTPClient("https://example.invalid", "reset-secret", opener=Recording())
    with pytest.raises(PocketHTTPError) as caught:
        client.reset_password(b"x" * 4097)
    assert (caught.value.status, caught.value.code) == (413, "PAYLOAD_TOO_LARGE")
    assert sent == []
    with pytest.raises(OSError):
        client.reset_password(b"x" * 4096)
    assert sent == [4096]


def test_auth_params_is_read_without_the_upload_token(pocket):
    client = PocketHTTPClient(pocket.url, "reset-secret", metadata_timeout=5)
    value = client.auth_params()
    assert value == {
        "schema_version": 1,
        "kdf": {"name": "PBKDF2-HMAC-SHA256", "iterations": 600000, "salt": pocket.salt, "dk_len": 32},
    }
    (request,) = pocket.requests
    assert (request.method, request.path) == ("GET", "/api/v1/auth/params")
    assert request.header("User-Agent") == f"bunri/{bunri_version}"
    assert request.header("Authorization") is None
    assert b"reset-secret" not in json.dumps(request.headers).encode()


@pytest.mark.parametrize(("status", "code"), [(405, "METHOD_NOT_ALLOWED"), (503, "AUTH_UNAVAILABLE")])
def test_auth_params_reports_http_errors(pocket, status, code):
    pocket.auth_params_reply = (status, {}, error_envelope(code))
    with pytest.raises(PocketHTTPError) as caught:
        PocketHTTPClient(pocket.url, "reset-secret").auth_params()
    assert (caught.value.status, caught.value.code) == (status, code)


@pytest.mark.parametrize("body", [b"<html>not json</html>", b"\xff\xfe", b""])
def test_auth_params_rejects_a_body_that_is_not_json(pocket, body):
    pocket.auth_params_reply = (200, {}, body)
    with pytest.raises(PocketHTTPError) as caught:
        PocketHTTPClient(pocket.url, "reset-secret").auth_params()
    assert (caught.value.status, caught.value.code) == (422, "INVALID_DOCUMENT")


def test_capabilities_still_carry_the_upload_token(pocket):
    PocketHTTPClient(pocket.url, "reset-secret").capabilities()
    (request,) = pocket.requests
    assert request.path == "/api/v1/upload/capabilities"
    assert request.header("Authorization") == "Bearer reset-secret"
