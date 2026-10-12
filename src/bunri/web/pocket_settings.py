"""Web endpoints for the Pocket connection itself: connecting, resetting the
viewing password, and disconnecting.

These three are guarded more strictly than the rest of the app, on purpose.
The app-wide same-origin check lets a request without an Origin header
through, which is fine for queueing a job but not here: one of these
endpoints stores an upload token, one changes who can log in to the Pocket,
and one throws the connection away. So each of them requires an Origin header
that names this server exactly, and the two that carry a body also require
`application/json` -- a form cannot send that, and a cross-origin `fetch`
that tries is stopped by the CORS preflight this server never answers. The
app-wide middleware is left as it is, so no other endpoint changes behaviour.

The price is that these endpoints cannot be driven from curl. The `bunri
pocket` commands cover the same ground, and another process run by the same
user could read the token from the settings file anyway.

Responses never contain the upload token, the connection URL, a salt or a
derived key; the page identifies a connection by its fingerprint only.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from bunri.pocket.config import PocketConfig, connection_fingerprint, delete_config, read_config
from bunri.pocket.connect import ConnectError, connect_pocket, safe_connect_error
from bunri.pocket.http import PocketHTTPClient
from bunri.pocket.lock import SyncLock, SyncLockBusy
from bunri.pocket.password import (
    REQUEST_LIMIT_BYTES,
    PasswordResetError,
    ResetOutcome,
    check_password_reset_support,
    outcome_messages,
    parse_reset_request,
    run_reset,
    safe_probe_error,
)

_DEFAULT_PORT_BY_SCHEME = {"http": 80, "https": 443}
_BUSY = "別の Pocket 操作が実行中です。完了後に再実行してください。"
_NO_STORE = {"Cache-Control": "no-store"}
_CONNECT_STATUS = {"input": 400, "pocket": 502, "save": 500}


def _effective_port(scheme: str, port: int | None) -> int | None:
    return port if port is not None else _DEFAULT_PORT_BY_SCHEME.get(scheme)


def _require_browser_origin(request: Request) -> None:
    """Refuse a request that does not say it comes from this server's page."""
    origin = request.headers.get("origin")
    same_origin = False
    if origin is not None:
        try:
            parsed = urlsplit(origin)
            same_origin = (
                bool(parsed.scheme)
                and bool(parsed.hostname)
                and parsed.scheme == request.url.scheme
                and parsed.hostname == request.url.hostname
                and _effective_port(parsed.scheme, parsed.port)
                == _effective_port(request.url.scheme, request.url.port)
            )
        except ValueError:
            # "null", a malformed port, a broken IPv6 literal: not this origin.
            same_origin = False
    if not same_origin:
        raise HTTPException(
            status_code=403,
            detail="この操作はブラウザの Bunri 画面からだけ実行できます。",
        )


def _require_json(request: Request) -> None:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(
            status_code=415,
            detail="Content-Type は application/json にしてください。",
        )


async def _read_body(request: Request, headers: dict[str, str] | None = None) -> bytes:
    """Read at most REQUEST_LIMIT_BYTES; anything longer is a 413."""
    too_large = HTTPException(
        status_code=413, detail="要求が大きすぎます。", headers=headers
    )
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > REQUEST_LIMIT_BYTES:
        raise too_large
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > REQUEST_LIMIT_BYTES:
            raise too_large
    return body


def register_pocket_settings_routes(app: FastAPI, out_dir: Path) -> None:
    def _config_or_409(headers: dict[str, str] | None = None) -> PocketConfig:
        try:
            config = read_config(out_dir)
        except (OSError, ValueError):
            raise HTTPException(
                status_code=409, detail="Pocket の接続設定を確認できません。", headers=headers
            )
        if config is None:
            raise HTTPException(
                status_code=409, detail="Pocket の接続設定がありません。", headers=headers
            )
        return config

    def _connect(url: str, token: str) -> dict:
        # Held while the settings are replaced, so a running sync or delete
        # job never has its Pocket swapped underneath it.
        try:
            lock = SyncLock(out_dir).acquire()
        except SyncLockBusy:
            raise HTTPException(status_code=409, detail=_BUSY)
        except OSError:
            raise HTTPException(status_code=500, detail="接続設定を保存できませんでした。")
        try:
            try:
                replaced = read_config(out_dir) is not None
            except (OSError, ValueError):
                # Unreadable settings are simply overwritten.
                replaced = False
            result = connect_pocket(out_dir, url, token, client_factory=PocketHTTPClient)
        except ConnectError as exc:
            raise HTTPException(
                status_code=_CONNECT_STATUS.get(exc.kind, 500),
                detail=safe_connect_error(exc),
            )
        finally:
            lock.release()
        return {
            "connection_fingerprint": result.connection_fingerprint,
            "replaced": replaced,
            "warnings": list(result.warnings),
        }

    @app.post("/api/pocket/connect")
    async def connect_pocket_endpoint(request: Request) -> JSONResponse:
        _require_browser_origin(request)
        _require_json(request)
        body = await _read_body(request)
        invalid = HTTPException(status_code=400, detail="要求の形式が不正です。")
        try:
            value = json.loads(body)
        except (ValueError, RecursionError):
            raise invalid
        if (
            not isinstance(value, dict)
            or set(value) != {"url", "token"}
            or not isinstance(value["url"], str)
            or not isinstance(value["token"], str)
        ):
            raise invalid
        return JSONResponse(await run_in_threadpool(_connect, value["url"], value["token"]))

    def _reset(config: PocketConfig, salt: bytes, derived_key: bytes) -> ResetOutcome:
        client = PocketHTTPClient(config.base_url, config.token)
        try:
            check_password_reset_support(client.capabilities())
        except PasswordResetError as exc:
            raise HTTPException(
                status_code=409 if exc.kind == "unsupported" else 502,
                detail=safe_probe_error(exc),
                headers=_NO_STORE,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=502, detail=safe_probe_error(exc), headers=_NO_STORE
            )
        # No Pocket mutation lock: a reset touches only the Pocket's own
        # credentials, which sync and delete never read or write.
        return run_reset(client, salt, derived_key)

    @app.post("/api/pocket/password-reset")
    async def reset_pocket_password(
        request: Request,
        pocket_fingerprint: str | None = None,
    ) -> JSONResponse:
        _require_browser_origin(request)
        _require_json(request)
        if pocket_fingerprint is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Pocket の接続先を確認できないため再設定を中止しました。"
                    "状態を再読込して確認し直してください。"
                ),
                headers=_NO_STORE,
            )
        config = _config_or_409(_NO_STORE)
        if pocket_fingerprint != connection_fingerprint(config):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Pocket の接続先が変更されたため再設定を中止しました。"
                    "状態を再読込して確認し直してください。"
                ),
                headers=_NO_STORE,
            )
        body = await _read_body(request, _NO_STORE)
        try:
            # The page derives the key, so no password reaches this server.
            # What arrives is checked and then rebuilt, never forwarded as is.
            salt, derived_key = parse_reset_request(json.loads(body))
        except (PasswordResetError, ValueError, RecursionError):
            raise HTTPException(
                status_code=400,
                detail="要求の形式が不正です。画面を再読込してやり直してください。",
                headers=_NO_STORE,
            ) from None
        finally:
            del body
        outcome = await run_in_threadpool(_reset, config, salt, derived_key)
        del derived_key
        # A rejection by the Pocket is a result too: the page draws every
        # outcome from the state and the messages.
        return JSONResponse(
            {
                "state": outcome.state,
                "via": outcome.via,
                "messages": outcome_messages(outcome),
            },
            headers=_NO_STORE,
        )

    @app.delete("/api/pocket/connection")
    def disconnect_pocket(
        request: Request,
        pocket_fingerprint: str | None = None,
    ) -> Response:
        _require_browser_origin(request)
        if pocket_fingerprint is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Pocket の接続先を確認できないため解除を中止しました。"
                    "状態を再読込して確認し直してください。"
                ),
            )
        failed = HTTPException(status_code=500, detail="接続設定を削除できませんでした。")
        # Held so the connection of a running sync or delete job is not
        # removed underneath it.
        try:
            lock = SyncLock(out_dir).acquire()
        except SyncLockBusy:
            raise HTTPException(status_code=409, detail=_BUSY)
        except OSError:
            raise failed
        try:
            config = _config_or_409()
            if pocket_fingerprint != connection_fingerprint(config):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Pocket の接続先が変更されたため解除を中止しました。"
                        "状態を再読込して確認し直してください。"
                    ),
                )
            try:
                delete_config(out_dir)
            except (OSError, ValueError):
                raise failed
        finally:
            lock.release()
        return Response(status_code=204)
