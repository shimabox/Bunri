"""A loopback stand-in for a Bunri Pocket, shared by the HTTP, Web API and
real-browser tests.

It answers only what connecting and resetting the viewing password need:
capabilities, `password/reset` and `auth/params`. Everything else is a 404
with the error envelope a Pocket uses, which the status inspection reads as
"nothing stored yet". Nothing here ever talks to a real Pocket.
"""

from __future__ import annotations

import base64
import contextlib
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator

TOKEN = base64.urlsafe_b64encode(b"\x5a" * 33).decode().rstrip("=")
OTHER_TOKEN = base64.urlsafe_b64encode(b"\x3c" * 33).decode().rstrip("=")

# The parts `bunri pocket connect` checks, plus the feature flag for the reset.
CAPABILITIES: dict[str, Any] = {
    "api": {"major": 1},
    "schemas": {
        "manifest": {"major": 1, "latest": "1.1"},
        "library": {"major": 1, "latest": "1.0"},
    },
    "limits": {"media_bytes": 94_371_840, "json_bytes": 1_048_576},
    "media": {"content_types": ["audio/mpeg"], "hash": "SHA-256", "conditional_json_put": True},
    "features": {"pan_split": True, "password_reset": True},
}

# bunri-pocket v0.4.0, fixtures/auth-v1/vectors.json (PBKDF2-HMAC-SHA256,
# 600000 iterations, dk_len 32). Public test values, not real credentials.
VECTORS = (
    {
        "password": "bunri-pocket-test-passphrase",
        "salt": "AAECAwQFBgcICQoLDA0ODw",
        "derived_key": "amWJtD1HdCn6v6NI81RE6WC_ysbPDCbdF5mMBCbC-Ts",
    },
    {
        "password": "bunri-pocket-reset-passphrase",
        "salt": "EBESExQVFhcYGRobHB0eHw",
        "derived_key": "ZDdrEqwT6KETy2izxW2uK25FVChkKDdx38yVDdttiOI",
    },
)


def capabilities(**features: Any) -> dict[str, Any]:
    """A copy of CAPABILITIES with the given feature flags replaced."""
    value = json.loads(json.dumps(CAPABILITIES))
    value["features"].update(features)
    return value


def error_envelope(code: str) -> bytes:
    return json.dumps({"error": {"code": code}}).encode()


@dataclass(frozen=True)
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        """Look a header up without depending on how the sender cased it."""
        return next((value for key, value in self.headers.items() if key.lower() == name.lower()), None)


Reply = tuple[int, dict[str, str], bytes]


@dataclass
class FakePocket:
    url: str = ""
    requests: list[Recorded] = field(default_factory=list)
    capabilities_reply: Reply | None = None
    # What POST password/reset answers; None applies the request and says 204.
    reset_reply: Reply | Callable[[Recorded], Reply] | None = None
    # What GET auth/params answers; None reports the salt last applied.
    auth_params_reply: Reply | None = None
    salt: str = "c3RhcnRpbmctc2FsdC0wMQ"
    # Set to hold password/reset open until released (response timeouts).
    hold_reset: threading.Event | None = None

    def recorded(self, method: str, path: str) -> list[Recorded]:
        return [item for item in self.requests if (item.method, item.path) == (method, path)]

    def _reply(self, request: Recorded) -> Reply:
        route = (request.method, request.path)
        if route == ("GET", "/api/v1/upload/capabilities"):
            return self.capabilities_reply or (200, {}, json.dumps(CAPABILITIES).encode())
        if route == ("POST", "/api/v1/password/reset"):
            if self.hold_reset is not None:
                self.hold_reset.wait(timeout=30)
            if callable(self.reset_reply):
                return self.reset_reply(request)
            if self.reset_reply is not None:
                return self.reset_reply
            self.salt = json.loads(request.body)["kdf"]["salt"]
            return (204, {}, b"")
        if route == ("GET", "/api/v1/auth/params"):
            if self.auth_params_reply is not None:
                return self.auth_params_reply
            document = {
                "schema_version": 1,
                "kdf": {"name": "PBKDF2-HMAC-SHA256", "iterations": 600000, "salt": self.salt, "dk_len": 32},
            }
            return (200, {}, json.dumps(document).encode())
        return (404, {}, error_envelope("NOT_FOUND"))


def _handler(pocket: FakePocket) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            pass

        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            request = Recorded(
                self.command, self.path, dict(self.headers), self.rfile.read(length) if length else b""
            )
            pocket.requests.append(request)
            status, headers, body = pocket._reply(request)
            try:
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                if status != 204:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD" and status != 204:
                    self.wfile.write(body)
            except OSError:
                pass  # the client gave up waiting

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _serve

    return Handler


@contextlib.contextmanager
def running_fake_pocket() -> Iterator[FakePocket]:
    pocket = FakePocket()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(pocket))
    # The default half-second poll would add that much to every test's teardown.
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    pocket.url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield pocket
    finally:
        if pocket.hold_reset is not None:
            pocket.hold_reset.set()
        httpd.shutdown()
        thread.join()
        httpd.server_close()
