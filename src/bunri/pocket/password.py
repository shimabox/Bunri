"""Viewing-password reset for a Bunri Pocket, shared by the CLI and the Web UI.

The password itself never leaves this machine: what a Pocket receives is the
KDF parameters and the PBKDF2 derived key. Every message built here is fixed
text, so no password, key, token or connection URL can reach a terminal, a
log or an HTTP response through an error.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from bunri.pocket.http import PASSWORD_RESET_LIMIT_BYTES, PocketHTTPError
from bunri.pocket.protocol import password_reset_supported

KDF_NAME = "PBKDF2-HMAC-SHA256"
PBKDF2_ITERATIONS = 600_000
DK_LEN = 32
SALT_LEN = 16
MIN_PASSWORD_CODEPOINTS = 12
REQUEST_LIMIT_BYTES = PASSWORD_RESET_LIMIT_BYTES
# How often, and how far apart, the Pocket is asked whether a reset whose
# response was lost has been applied. Read at call time.
CONFIRM_ATTEMPTS = 3
CONFIRM_INTERVAL_SECONDS = 5.0

UNCHANGED = "閲覧パスワードは変わっていません。"

_ERROR_MESSAGES = {
    "too_short": "閲覧パスワードは 12 文字(Unicode コードポイント)以上にしてください。",
    "not_utf8": "パスワードに UTF-8 にできない文字が含まれています。",
    "unsupported": "この音源ポケットは閲覧パスワードの再設定に未対応です。音源ポケットを 0.4.0 以降へ更新してください。",
    "undetermined": "音源ポケットの対応状況を確認できませんでした。",
    "invalid_request": "要求の形式が不正です。",
}
# Codes assigned here after a response arrived; a Pocket never returns them,
# so they say nothing about whether the reset was written.
_LOST_RESPONSE_CODES = ("UNEXPECTED_RESPONSE", "RESPONSE_TOO_LARGE")
_FORMAT_REJECTION_STATUSES = (400, 405, 411, 413, 415, 422)
_AFTER_COMPLETION = [
    "音源ポケットにログインしていたすべての端末はログアウトされました。新しいパスワードで入り直してください。",
    "直前にログインに失敗していた場合は、待ち時間(最大 60 秒)が明けるまでログインできないことがあります。",
    "曲と upload token、Bunri の接続設定はそのままです。",
]

ResetState = Literal["completed", "not_applied", "unknown", "retry_later"]
ConfirmState = Literal["completed", "not_applied", "unknown"]


class PasswordResetError(RuntimeError):
    """A reset stopped before anything was sent. The message is fixed text."""

    def __init__(self, kind: str) -> None:
        super().__init__(_ERROR_MESSAGES[kind])
        self.kind = kind


@dataclass(frozen=True)
class ResetOutcome:
    state: ResetState
    via: Literal["response", "auth_params"]
    # Kept only to choose the wording; never shown as it is.
    error: BaseException | None = field(default=None, repr=False)


def validate_new_password(password: str) -> bytes:
    """Check the length and return the UTF-8 bytes the key is derived from.

    The string is used exactly as typed: no Unicode normalization and no
    trimming, so the bytes match what a Pocket's own login form produces.
    """
    if len(password) < MIN_PASSWORD_CODEPOINTS:
        raise PasswordResetError("too_short")
    try:
        return password.encode("utf-8")
    except UnicodeEncodeError:
        # The original exception quotes the offending text.
        raise PasswordResetError("not_utf8") from None


def generate_salt() -> bytes:
    return secrets.token_bytes(SALT_LEN)


def derive_key(password_utf8: bytes, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password_utf8, salt, PBKDF2_ITERATIONS, dklen=DK_LEN)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode_canonical(text: object) -> bytes:
    if not isinstance(text, str) or re.fullmatch(r"[A-Za-z0-9_-]*", text) is None or len(text) % 4 == 1:
        raise PasswordResetError("invalid_request")
    decoded = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    # Leftover bits in the last character decode to the same bytes; only the
    # one spelling that encoding produces is accepted.
    if b64url(decoded) != text:
        raise PasswordResetError("invalid_request")
    return decoded


def decode_b64url(text: str, length: int) -> bytes:
    """Decode unpadded, canonical base64url of exactly `length` bytes."""
    decoded = _decode_canonical(text)
    if len(decoded) != length:
        raise PasswordResetError("invalid_request")
    return decoded


def build_reset_body(salt: bytes, derived_key: bytes) -> bytes:
    if len(salt) != SALT_LEN or len(derived_key) != DK_LEN:
        raise ValueError("salt または derived key の長さが不正です")
    document = {
        "kdf": {
            "name": KDF_NAME,
            "iterations": PBKDF2_ITERATIONS,
            "salt": b64url(salt),
            "dk_len": DK_LEN,
        },
        "derived_key": b64url(derived_key),
    }
    return json.dumps(document, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _is_int(value: object, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def parse_reset_request(value: object) -> tuple[bytes, bytes]:
    """Check a reset document derived elsewhere and return (salt, derived key)."""
    if not isinstance(value, dict) or set(value) != {"kdf", "derived_key"}:
        raise PasswordResetError("invalid_request")
    kdf = value["kdf"]
    if not isinstance(kdf, dict) or set(kdf) != {"name", "iterations", "salt", "dk_len"}:
        raise PasswordResetError("invalid_request")
    if kdf["name"] != KDF_NAME or not _is_int(kdf["iterations"], PBKDF2_ITERATIONS) or not _is_int(kdf["dk_len"], DK_LEN):
        raise PasswordResetError("invalid_request")
    return decode_b64url(kdf["salt"], SALT_LEN), decode_b64url(value["derived_key"], DK_LEN)


def check_password_reset_support(capabilities: object) -> None:
    """Tell an older Pocket apart from an answer that cannot be read."""
    if not isinstance(capabilities, dict):
        raise PasswordResetError("undetermined")
    if not password_reset_supported(capabilities):
        raise PasswordResetError("unsupported")


def _salt_check(client: Any, salt: bytes) -> Literal["match", "mismatch", "unreadable"]:
    try:
        value = client.auth_params()
    except (PocketHTTPError, OSError):
        return "unreadable"
    kdf = value.get("kdf") if isinstance(value, dict) else None
    try:
        current = _decode_canonical(kdf.get("salt") if isinstance(kdf, dict) else None)
    except PasswordResetError:
        return "unreadable"
    if len(current) < SALT_LEN:
        return "unreadable"
    return "match" if current == salt else "mismatch"


def confirm_reset(
    client: Any, salt: bytes, *, sleep: Callable[[float], None] = time.sleep
) -> ConfirmState:
    """Ask the Pocket whether a reset whose response was lost took effect.

    The salt is fresh random bytes for every reset, so finding it in the
    public KDF parameters means this very request was applied. A different
    salt is checked again after a wait, because a request that timed out on
    this side can still finish on the other.
    """
    last = "unreadable"
    for attempt in range(CONFIRM_ATTEMPTS):
        if attempt:
            sleep(CONFIRM_INTERVAL_SECONDS)
        last = _salt_check(client, salt)
        if last == "match":
            return "completed"
    return "not_applied" if last == "mismatch" else "unknown"


def run_reset(
    client: Any,
    salt: bytes,
    derived_key: bytes,
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_confirm: Callable[[], None] | None = None,
) -> ResetOutcome:
    """Send the reset and classify what is known about its result.

    `on_confirm` is called once, just before the Pocket is asked for the
    result of a request whose response was lost.
    """
    body = build_reset_body(salt, derived_key)
    del derived_key
    try:
        client.reset_password(body)
    except PocketHTTPError as exc:
        # The code is looked at before the status: see _LOST_RESPONSE_CODES.
        if exc.code not in _LOST_RESPONSE_CODES:
            if 400 <= exc.status < 500:
                return ResetOutcome("not_applied", "response", exc)
            if exc.status == 503:
                return ResetOutcome("retry_later", "response", exc)
        lost: BaseException = exc
    except OSError as exc:
        lost = exc
    else:
        return ResetOutcome("completed", "response")
    finally:
        del body
    if on_confirm is not None:
        on_confirm()
    return ResetOutcome(confirm_reset(client, salt, sleep=sleep), "auth_params", lost)


def reset_with_password(
    client: Any,
    password: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_confirm: Callable[[], None] | None = None,
) -> ResetOutcome:
    password_utf8 = validate_new_password(password)
    del password
    salt = generate_salt()
    derived_key = derive_key(password_utf8, salt)
    del password_utf8
    return run_reset(client, salt, derived_key, sleep=sleep, on_confirm=on_confirm)


def _http_label(exc: PocketHTTPError, *, with_code: bool = True) -> str:
    # The code comes from the response body, so only the shape a Pocket uses
    # for its error codes is ever repeated.
    code = exc.code if with_code and isinstance(exc.code, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", exc.code) else None
    return f"HTTP {exc.status}" + (f" {code}" if code else "")


def _rate_limited(exc: PocketHTTPError) -> str:
    message = "音源ポケットが要求を制限しました。しばらく待って再実行してください。"
    return message + (f"(目安: {exc.retry_after} 秒後)" if exc.retry_after else "")


def safe_probe_error(exc: BaseException) -> str:
    """Wording for a failure before the reset request was sent."""
    if isinstance(exc, PasswordResetError):
        message = str(exc)
    elif isinstance(exc, PocketHTTPError):
        messages = {
            401: "upload token が無効です。音源ポケットで token を再発行した場合は接続し直してください。",
            404: "接続先に音源ポケットの API が見つかりません。接続先 URL を確認してください。",
            503: "音源ポケットを利用できません。時間をおいて再実行してください。",
        }
        if exc.status == 429:
            message = _rate_limited(exc)
        else:
            message = messages.get(exc.status, f"音源ポケットとの通信に失敗しました({_http_label(exc)})。")
    elif isinstance(exc, OSError):
        message = "音源ポケットに接続できませんでした。"
    else:
        message = _ERROR_MESSAGES["undetermined"]
    return message + UNCHANGED


def _rejection_message(exc: BaseException | None) -> str:
    # Chosen from the status and code alone: the generic wording of
    # PocketHTTPError describes 422 as a document mismatch, which is wrong for
    # a rejected KDF.
    if not isinstance(exc, PocketHTTPError):
        return "音源ポケットが要求を拒否しました。"
    if exc.status == 401:
        return "upload token が無効です。音源ポケットで token を再発行した場合は接続し直してください。"
    if exc.status == 403:
        if exc.code == "ORIGIN_MISMATCH":
            return "音源ポケットが Origin の不一致として拒否しました。接続先 URL と音源ポケットの設定を確認してください。"
        return "音源ポケットの手前で要求が拒否されました(HTTP 403)。接続先の保護設定を確認してください。"
    if exc.status == 404:
        return "音源ポケットに再設定の経路がありません。音源ポケットを 0.4.0 以降へ更新するか、接続先 URL を確認してください。"
    if exc.status == 422 and exc.code == "UNSUPPORTED_KDF":
        return "この Bunri の鍵導出方式を音源ポケットが受け付けません。Bunri を更新してください。"
    if exc.status in _FORMAT_REJECTION_STATUSES:
        return f"音源ポケットが要求の形式を拒否しました({_http_label(exc)})。Bunri と音源ポケットの版の組み合わせを確認してください。"
    if exc.status == 429:
        return _rate_limited(exc)
    return f"音源ポケットが要求を拒否しました({_http_label(exc)})。"


def outcome_messages(outcome: ResetOutcome) -> list[str]:
    """The lines shown for a result, the same in the terminal and the page."""
    if outcome.state == "completed":
        first = (
            "閲覧パスワードを決め直しました。"
            if outcome.via == "response"
            else "音源ポケットからの応答を確認できなかったため、結果を問い合わせました。再設定は完了していました。"
        )
        return [first, *_AFTER_COMPLETION]
    if outcome.state == "retry_later":
        return ["音源ポケットの認証情報または保存領域を利用できませんでした。時間をおいて同じ操作を再実行してください。"]
    if outcome.state == "not_applied" and outcome.via == "response":
        return [_rejection_message(outcome.error), UNCHANGED]
    if outcome.state == "not_applied":
        return [
            "音源ポケットからの応答を確認できなかったため、結果を問い合わせました。再設定は反映されていません。閲覧パスワードは以前のままです。",
            "同じ操作をもう一度実行してください。再実行では今回と同じパスワードを使ってください(遅れて届いた前回の要求が後から反映されても結果が変わらないようにするため)。",
        ]
    return [
        "音源ポケットからの応答を確認できなかったため、結果を問い合わせましたが、確認できませんでした。",
        "再設定が完了したかどうかは分かりません。新しいパスワードでログインできれば完了しています。古いパスワードで入れる場合は完了していません。同じ操作をもう一度実行しても安全です。再実行では今回と同じパスワードを使ってください。",
    ]
