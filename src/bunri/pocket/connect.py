"""Connecting an output directory to a Bunri Pocket.

The steps are the ones `bunri pocket connect` takes: check the URL and the
token, ask the Pocket for its capabilities, then save the settings. What
differs is the reporting. Failures become a `ConnectError` whose message is
fixed text, so neither the token nor the URL -- nor a host name out of a
network error -- can end up in a response.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from bunri.pocket.config import TOKEN_REJECTED_MESSAGE, PocketConfig, connection_fingerprint, save_config, validate_base_url, validate_capabilities, validate_token
from bunri.pocket.http import PocketHTTPClient, PocketHTTPError


class ConnectError(RuntimeError):
    """`kind` is "input" (URL or token), "pocket" (the Pocket's answer) or
    "save" (writing the settings)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ConnectResult:
    connection_fingerprint: str
    warnings: tuple[str, ...]


def _pocket_failure(exc: BaseException) -> str:
    if isinstance(exc, PocketHTTPError):
        messages = {
            401: TOKEN_REJECTED_MESSAGE,
            404: "接続先に音源ポケットの API が見つかりません。URL を確認してください。",
        }
        return messages.get(exc.status, f"音源ポケットとの通信に失敗しました(HTTP {exc.status})。")
    if isinstance(exc, OSError):
        # str(exc) can name the host, so it is never used.
        return "音源ポケットに接続できませんでした。URL を確認してください。"
    return "音源ポケットの応答を解釈できませんでした。URL を確認してください。"


def safe_connect_error(exc: BaseException) -> str:
    """Return a message for a failed connection that holds no secret or URL."""
    if isinstance(exc, ConnectError):
        return str(exc)
    return "音源ポケットへの接続に失敗しました。"


def connect_pocket(
    out_dir: Path,
    url: str,
    token: str,
    *,
    client_factory: Callable[[str, str], Any] = PocketHTTPClient,
) -> ConnectResult:
    """Verify a Pocket and save it as the connection of `out_dir`.

    Nothing is saved unless the Pocket answers with capabilities this client
    can work with. The caller holds the Pocket mutation lock.
    """
    try:
        base_url = validate_base_url(url)
        token = validate_token(token)
    except ValueError as exc:
        # These messages hold none of the input. A URL's says which rule
        # failed; a token's says only that it is wrong.
        raise ConnectError("input", str(exc)) from None
    try:
        capabilities = client_factory(base_url, token).capabilities()
    except (PocketHTTPError, OSError, ValueError) as exc:
        raise ConnectError("pocket", _pocket_failure(exc)) from None
    try:
        validate_capabilities(capabilities)
    except ValueError as exc:
        raise ConnectError("pocket", str(exc)) from None
    config = PocketConfig(base_url, token)
    try:
        warnings = save_config(out_dir, config)
    except OSError:
        raise ConnectError("save", "接続設定を保存できませんでした。") from None
    return ConnectResult(connection_fingerprint(config), tuple(warnings))
