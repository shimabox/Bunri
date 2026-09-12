"""Adversarial review PoCs for the `bunri pocket` commands and the Web sync / delete paths.

Each ``test_poc_*`` test is a reproduction test for a finding and passes after
the corresponding fix. Each ``test_guard_*`` test is evidence that a suspected
path is closed. Nothing here modifies implementation code.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rich.console import Console
from typer.testing import CliRunner

from bunri.package_metadata import (
    PackageMetadata,
    SourceIdentity,
    TargetMetadata,
    write_package_metadata,
)
from bunri.pocket.config import PocketConfig, connection_fingerprint, save_config
from bunri.pocket.http import PocketHTTPClient
from bunri.pocket.service import LibraryTrack
from bunri.pocket.sync import SyncResult

TOKEN = base64.urlsafe_b64encode(b"\x5a" * 33).decode().rstrip("=")
DIGEST = "a" * 40
SONG_ID = DIGEST[:12]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "timed out waiting for condition"


def _make_local_song(out_dir: Path, *, name: str = "Song", digest: str = DIGEST) -> Path:
    package = out_dir / name
    package.mkdir(parents=True)
    write_package_metadata(
        package / ".bunri-package.json",
        PackageMetadata(
            name,
            name,
            SourceIdentity("sha1", digest, digest[:12]),
            (TargetMetadata("guitar", ("mp3",)),),
        ),
    )
    for suffix in ("original.mp3", "guitar.mp3", "guitar.backing.mp3"):
        (package / f"{name}.{suffix}").write_bytes(b"audio")
    return package


class _RawTCPServer:
    """Accepts connections, records every request byte, answers with `reply`."""

    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.received: list[bytes] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                chunks = []
                try:
                    while True:
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        if b"\r\n\r\n" in b"".join(chunks):
                            break
                except OSError:
                    pass
                self.received.append(b"".join(chunks))
                try:
                    conn.sendall(self.reply)
                except OSError:
                    pass

    def close(self) -> None:
        self._sock.close()


def _run_bunri_pocket(args: list[str], *, cwd: Path, stdin: str = "") -> subprocess.CompletedProcess:
    code = "import sys; from bunri.cli import dispatch; sys.argv = ['bunri', 'pocket'] + sys.argv[1:]; dispatch()"
    env = {**os.environ, "COLUMNS": "300", "NO_COLOR": "1", "TERM": "dumb"}
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


# --------------------------------------------------------------------------
# Finding 1: a malformed HTTP status line from the shelf (http.client.BadStatusLine,
# an http.client.HTTPException that is neither OSError nor RuntimeError nor
# ValueError) escapes every `except` in the pocket CLI and lands as a raw
# traceback. With the pinned Typer (0.26.8, show_locals=False) the token is not
# in that traceback; the second test shows the same escape prints the bearer
# token as soon as locals are rendered (older Typer defaults, or a developer
# enabling pretty_exceptions_show_locals), because `_request`'s frame holds
# `request_headers`.
# --------------------------------------------------------------------------


@pytest.fixture()
def garbage_server():
    server = _RawTCPServer(b"NOPE\r\n\r\n")
    yield server
    server.close()


@pytest.mark.parametrize("command", ["connect", "delete"])
def test_poc_cli_transport_garbage_is_reported_without_traceback(tmp_path, garbage_server, command):
    out = tmp_path / "out"
    out.mkdir()
    url = f"http://127.0.0.1:{garbage_server.port}"
    if command == "connect":
        result = _run_bunri_pocket(["connect", url, "-o", str(out), "--token-stdin"], cwd=tmp_path, stdin=TOKEN + "\n")
    else:
        save_config(out, PocketConfig(url, TOKEN))
        result = _run_bunri_pocket(["delete", "--song-id", SONG_ID, "--yes", "-o", str(out)], cwd=tmp_path)
    combined = result.stdout + result.stderr
    assert TOKEN not in combined, "upload token printed to the terminal:\n" + combined[-3000:]
    assert "Traceback" not in combined and "BadStatusLine" not in combined, (
        "transport error escaped the CLI's error handling as a raw traceback:\n" + combined[-1500:]
    )


def test_poc_cli_transport_garbage_never_prints_token_even_with_locals(tmp_path, garbage_server):
    out = tmp_path / "out"
    out.mkdir()
    save_config(out, PocketConfig(f"http://127.0.0.1:{garbage_server.port}", TOKEN))
    code = (
        "import sys; import bunri.pocket.cli as m; m.app.pretty_exceptions_show_locals = True; "
        "sys.argv = ['bunri pocket'] + sys.argv[1:]; m.app(prog_name='bunri pocket')"
    )
    env = {**os.environ, "COLUMNS": "300", "NO_COLOR": "1", "TERM": "dumb"}
    result = subprocess.run(
        [sys.executable, "-c", code, "delete", "--song-id", SONG_ID, "--yes", "-o", str(out)],
        cwd=tmp_path, capture_output=True, text=True, env=env, timeout=60,
    )
    combined = result.stdout + result.stderr
    assert TOKEN not in combined, "bearer token rendered from `_request` locals:\n" + combined[-3000:]


# --------------------------------------------------------------------------
# Finding 2: a plain-text loopback shelf is silently routed through the
# process-wide http_proxy, carrying the bearer token off the host.
# --------------------------------------------------------------------------


def test_poc_http_proxy_env_does_not_carry_loopback_token_offhost(monkeypatch):
    proxy = _RawTCPServer(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
    try:
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.port}")
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        # Port 9 is closed on loopback: the only way this call can get a 200
        # is by going through the proxy instead of the configured host.
        client = PocketHTTPClient("http://localhost:9", TOKEN)
        try:
            client.capabilities()
        except OSError:
            pass
        assert not any(TOKEN.encode() in request for request in proxy.received), (
            "Authorization header for a loopback shelf was sent to the proxy from http_proxy"
        )
    finally:
        proxy.close()


# --------------------------------------------------------------------------
# Finding 3: original.mp3 (uploaded by default) carries the input file's tags
# and the ffmpeg build string through the normalize -> encode pipeline.
# --------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg required")
def test_poc_original_mp3_carries_no_input_tags_or_encoder_version(tmp_path):
    from bunri.audio import encode_mp3, normalize_to_wav

    source = tmp_path / "input.m4a"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-metadata", "title=Secret Demo",
            "-metadata", "artist=Alice",
            "-metadata", "comment=purchased by alice@example.com",
            "-metadata", "album=Home Recordings 2026",
            "-c:a", "aac", str(source),
        ],
        check=True,
    )
    wav = tmp_path / "input.wav"
    original = tmp_path / "Song.original.mp3"
    normalize_to_wav(source, wav)  # package.py: _normalize_step
    encode_mp3(wav, original)  # package.py: _export_mp3(input_wav, ...original.mp3)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(original)],
        capture_output=True, text=True, check=True,
    )
    tags = json.loads(probe.stdout).get("format", {}).get("tags", {})
    leaked = {k: v for k, v in tags.items() if k.lower() in {"comment", "artist", "album", "encoder"}}
    assert not leaked, f"uploaded original.mp3 carries input tags / encoder build: {leaked}"


# --------------------------------------------------------------------------
# Finding 4: a queued Web sync is not bound to the shelf the page displayed.
# --------------------------------------------------------------------------


def test_poc_queued_web_sync_uploads_to_the_shelf_the_page_showed(tmp_path, monkeypatch):
    import bunri.pocket.service as service_module
    from bunri.web.app import create_app

    _make_local_song(tmp_path)
    shelf_a = "https://shelf-a.invalid"
    shelf_b = "https://shelf-b.invalid"
    save_config(tmp_path, PocketConfig(shelf_a, TOKEN))

    uploaded_to: list[str] = []
    monkeypatch.setattr(
        service_module,
        "synchronize",
        lambda package, client, **kwargs: uploaded_to.append(client.base_url) or SyncResult(),
    )

    release = threading.Event()
    running = threading.Event()

    def blocking_runner(upload_path, out_dir, title, target, log_path):
        running.set()
        release.wait(timeout=20)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("blocked\n", encoding="utf-8")
        return 1

    app = create_app(tmp_path, runner=blocking_runner)
    with TestClient(app) as c:
        try:
            # A separation job occupies the single worker for minutes.
            c.post("/api/jobs", files={"file": ("busy.mp3", b"busy-bytes", "audio/mpeg")}, data={"title": "Busy"})
            assert running.wait(timeout=5)
            # The page shows shelf A and the user clicks "upload".
            shown = c.get("/api/pocket/job").json()["connection_fingerprint"]
            assert shown == connection_fingerprint(PocketConfig(shelf_a, TOKEN))
            queued = c.post(
                f"/api/pocket/sync/{SONG_ID}?pocket_fingerprint={shown}"
            )
            assert queued.status_code == 202
            # Meanwhile `bunri pocket connect <shelf B>` rewrites the config.
            save_config(tmp_path, PocketConfig(shelf_b, TOKEN))
        finally:
            release.set()
            job_id = queued.json()["job_id"]
            _wait_until(lambda: c.get(f"/api/jobs/{job_id}").json()["status"] in ("done", "error"))
        finished = c.get(f"/api/jobs/{job_id}").json()

    assert uploaded_to == [], f"connection change still uploaded to {uploaded_to}"
    assert finished["status"] == "error"
    assert finished["error"] == (
        "接続先が変更されたため同期を中止しました。"
        "状態を再読込して確認し直してください"
    )


# --------------------------------------------------------------------------
# Finding 5: `bunri pocket delete --select` renders remote titles as rich
# markup and passes terminal escape sequences through unchanged.
# --------------------------------------------------------------------------


def _run_select(monkeypatch, tmp_path, titles: list[str]) -> tuple[str, object]:
    import bunri.pocket.cli as cli_module

    buffer = io.StringIO()
    monkeypatch.setattr(cli_module, "console", Console(file=buffer, force_terminal=True, width=200))
    monkeypatch.setattr(cli_module, "read_config", lambda out: PocketConfig("https://shelf.invalid", TOKEN))
    monkeypatch.setattr(cli_module, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(cli_module, "PocketHTTPClient", lambda *a, **k: object())
    tracks = tuple(LibraryTrack(song_id=f"{i:012x}", title=title) for i, title in enumerate(titles, 1))
    monkeypatch.setattr(cli_module, "list_library_tracks", lambda out, client=None: tracks)
    result = CliRunner().invoke(cli_module.app, ["delete", "--select", "-o", str(tmp_path)], input="1\nn\n")
    return buffer.getvalue(), result


@pytest.mark.parametrize("sequence", ["\x1bc", "\x1bM"], ids=["RIS-reset", "reverse-index"])
def test_poc_select_listing_does_not_pass_terminal_escapes_from_remote_titles(monkeypatch, tmp_path, sequence):
    hostile = f"Real Song{sequence}  1. Decoy Song — 000000000002"
    output, _ = _run_select(monkeypatch, tmp_path, ["Real Song", hostile])
    assert sequence not in output, "remote title escape sequence reached the terminal verbatim"


def test_poc_select_listing_does_not_interpret_remote_titles_as_markup(monkeypatch, tmp_path):
    output, _ = _run_select(monkeypatch, tmp_path, ["[bold red]Styled[/]", "Plain"])
    assert "[bold red]Styled[/]" in output, "remote title was interpreted as rich markup"


def test_poc_select_listing_does_not_crash_on_unbalanced_remote_markup(monkeypatch, tmp_path):
    _, result = _run_select(monkeypatch, tmp_path, ["[/x] closing tag", "Plain"])
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"remote title crashed the command: {result.exception!r}"
    )


# --------------------------------------------------------------------------
# Finding 6 (low): /api/pocket/status turns a malformed remote status line
# into a 500 instead of the "unknown" state every other transport error gets.
# --------------------------------------------------------------------------


def test_poc_pocket_status_reports_unknown_on_malformed_remote_status_line(tmp_path, monkeypatch):
    from bunri.web.app import create_app

    save_config(tmp_path, PocketConfig("https://shelf.invalid", TOKEN))

    class BadStatusOpener:
        def open(self, request, timeout):
            raise http.client.BadStatusLine("NOPE")

    monkeypatch.setattr(
        "bunri.pocket.http.urllib.request.build_opener",
        lambda *_handlers: BadStatusOpener(),
    )
    app = create_app(tmp_path, runner=lambda *args: 0)
    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.get("/api/pocket/status")
    assert response.status_code == 200, response.text
    assert response.json().get("state") == "unknown"


# --------------------------------------------------------------------------
# Guard evidence (expected to pass)
# --------------------------------------------------------------------------


def test_guard_pocket_mutations_reject_cross_origin_requests(tmp_path):
    from bunri.web.app import create_app

    _make_local_song(tmp_path)
    save_config(tmp_path, PocketConfig("http://127.0.0.1:1", TOKEN))
    fingerprint = connection_fingerprint(PocketConfig("http://127.0.0.1:1", TOKEN))
    app = create_app(tmp_path, runner=lambda *args: 0)
    with TestClient(app) as c:
        headers = {"Origin": "http://evil.example"}
        assert c.post("/api/pocket/sync", headers=headers).status_code == 403
        assert c.post(f"/api/pocket/sync/{SONG_ID}", headers=headers).status_code == 403
        assert c.delete(
            f"/api/songs/x?pocket=true&pocket_fingerprint={fingerprint}", headers=headers
        ).status_code == 403
        assert c.delete("/api/songs/x", headers=headers).status_code == 403
        null_origin = {"Origin": "null"}
        assert c.post("/api/pocket/sync", headers=null_origin).status_code == 403
    assert not list((tmp_path / "web" / "jobs").glob("*.json")) if (tmp_path / "web" / "jobs").exists() else True


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def test_guard_pocket_get_endpoints_do_not_mutate_output_dir(tmp_path):
    from bunri.web.app import create_app

    _make_local_song(tmp_path)
    save_config(tmp_path, PocketConfig("http://127.0.0.1:1", TOKEN))
    app = create_app(tmp_path, runner=lambda *args: 0)
    with TestClient(app) as c:
        before = _snapshot(tmp_path)
        assert c.get("/api/pocket/job").status_code == 200
        assert c.get("/api/pocket/status").status_code == 200
        after = _snapshot(tmp_path)
    assert before == after
    assert not (tmp_path / ".pocket" / "sync.lock").exists()


def test_guard_pocket_status_and_job_responses_never_contain_token_or_url(tmp_path):
    from bunri.web.app import create_app

    _make_local_song(tmp_path)
    save_config(tmp_path, PocketConfig("http://127.0.0.1:1", TOKEN))
    app = create_app(tmp_path, runner=lambda *args: 0)
    with TestClient(app) as c:
        for path in ("/api/pocket/job", "/api/pocket/status", "/api/songs", "/api/jobs"):
            text = c.get(path).text
            assert TOKEN not in text and "127.0.0.1:1" not in text, path


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
def test_guard_self_signed_certificate_is_rejected_before_any_request(tmp_path):
    import ssl
    from http.server import BaseHTTPRequestHandler, HTTPServer

    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
         "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1"],
        check=True, capture_output=True,
    )
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get("Authorization", ""))
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = PocketHTTPClient(f"https://127.0.0.1:{server.server_port}", TOKEN)
        with pytest.raises(OSError):
            client.capabilities()
    finally:
        server.shutdown()
    assert received == []


def test_guard_connect_rewrites_config_atomically_with_0600_and_drops_old_token(tmp_path):
    old = base64.urlsafe_b64encode(b"\x01" * 33).decode().rstrip("=")
    save_config(tmp_path, PocketConfig("https://old.invalid", old))
    (tmp_path / ".pocket" / "config.json").chmod(0o644)
    save_config(tmp_path, PocketConfig("https://new.invalid", TOKEN))
    config = tmp_path / ".pocket" / "config.json"
    assert oct(config.stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / ".pocket").stat().st_mode & 0o777) == "0o700"
    leftovers = [p.name for p in (tmp_path / ".pocket").iterdir() if p.name != "config.json"]
    assert leftovers == []
    assert old not in config.read_text()


def test_guard_sync_never_reads_through_symlinks(tmp_path):
    from bunri.pocket.local import LocalPreflightError, preflight
    from bunri.pocket.service import inventory

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.mp3").write_bytes(b"private")
    out = tmp_path / "out"
    out.mkdir()
    # 1. a symlinked package directory is not a package at all
    (out / "Alias").symlink_to(outside)
    # 2. a real package whose original.mp3 is a symlink to outside is refused
    package = _make_local_song(out)
    (package / "Song.original.mp3").unlink()
    (package / "Song.original.mp3").symlink_to(outside / "private.mp3")
    with pytest.raises(LocalPreflightError, match="通常ファイルではありません"):
        preflight(out, "Song")
    with pytest.raises(Exception, match="通常ファイルではありません"):
        inventory(out)
    # 3. with --no-original the symlinked original is simply not part of the upload set
    package_without_original = preflight(out, "Song", include_original=False)
    assert all(asset.path.resolve().is_relative_to(out.resolve()) for asset in package_without_original.assets)
    assert "Alias" not in {p.directory.name for p in [package_without_original]}


def test_guard_nfc_and_nfd_selectors_resolve_to_the_same_song_id(tmp_path):
    import unicodedata

    from bunri.pocket.service import resolve_delete_target

    nfc = unicodedata.normalize("NFC", "ガ")
    nfd = unicodedata.normalize("NFD", "ガ")
    _make_local_song(tmp_path, name=nfd)
    stored = [p.name for p in tmp_path.iterdir() if p.is_dir()][0]
    if not (tmp_path / nfc).exists():
        pytest.skip("filesystem distinguishes NFC and NFD; the conflict path is covered by test_pocket_service")
    by_nfc = resolve_delete_target(tmp_path, nfc)
    by_nfd = resolve_delete_target(tmp_path, nfd)
    assert by_nfc.song_id == by_nfd.song_id == SONG_ID
    assert by_nfc.safe_name == by_nfd.safe_name == stored
