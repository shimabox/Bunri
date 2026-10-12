"""Viewing-password reset: key derivation, the request document, and how a
result is classified when the response is lost. No server is involved; the
client is a small fake."""

from __future__ import annotations

import base64
import json
import unicodedata

import pytest

from bunri.pocket import password as password_module
from bunri.pocket.http import PocketHTTPError
from bunri.pocket.password import (
    PasswordResetError,
    ResetOutcome,
    b64url,
    build_reset_body,
    check_password_reset_support,
    confirm_reset,
    decode_b64url,
    derive_key,
    generate_salt,
    outcome_messages,
    parse_reset_request,
    reset_with_password,
    run_reset,
    safe_probe_error,
    validate_new_password,
)
from pocket_fake import VECTORS, capabilities

URL = "https://pocket.invalid/api/v1/password/reset"
SALT = bytes(range(16, 32))
OTHER_SALT = bytes(range(16))
KEY = bytes(range(32))


def _raw(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _params(salt: bytes) -> dict:
    return {
        "schema_version": 1,
        "kdf": {"name": "PBKDF2-HMAC-SHA256", "iterations": 600000, "salt": b64url(salt), "dk_len": 32},
    }


class FakeClient:
    """Answers reset_password and auth_params from prepared values."""

    def __init__(self, reset=None, params=()):
        self.reset = reset
        self.params = list(params)
        self.bodies: list[bytes] = []
        self.auth_calls = 0

    def reset_password(self, body: bytes) -> None:
        self.bodies.append(body)
        if isinstance(self.reset, BaseException):
            raise self.reset

    def auth_params(self):
        self.auth_calls += 1
        item = self.params.pop(0) if self.params else OSError("no answer")
        if isinstance(item, BaseException):
            raise item
        return item


class Sleeps(list):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


# --------------------------------------------------------------------------
# Derivation and the request document
# --------------------------------------------------------------------------


def test_public_vectors_derive_the_published_keys():
    first, second = VECTORS
    derived = [
        b64url(derive_key(vector["password"].encode("utf-8"), _raw(vector["salt"])))
        for vector in VECTORS
    ]
    assert derived[1] == second["derived_key"] == "ZDdrEqwT6KETy2izxW2uK25FVChkKDdx38yVDdttiOI"
    assert derived[0] == first["derived_key"] == "amWJtD1HdCn6v6NI81RE6WC_ysbPDCbdF5mMBCbC-Ts"
    assert derived[0] != derived[1]


def test_reset_body_has_exactly_the_contract_shape():
    vector = VECTORS[1]
    body = build_reset_body(_raw(vector["salt"]), _raw(vector["derived_key"]))
    assert body.isascii() and len(body) <= 4096
    value = json.loads(body)
    # The request example in the "認証" section of bunri-pocket v0.4.0's
    # docs/protocol-v1.md.
    assert value == {
        "kdf": {
            "name": "PBKDF2-HMAC-SHA256",
            "iterations": 600000,
            "salt": "EBESExQVFhcYGRobHB0eHw",
            "dk_len": 32,
        },
        "derived_key": "ZDdrEqwT6KETy2izxW2uK25FVChkKDdx38yVDdttiOI",
    }
    assert list(value) == ["kdf", "derived_key"]
    assert list(value["kdf"]) == ["name", "iterations", "salt", "dk_len"]
    assert b" " not in body and b"=" not in body


def test_generate_salt_is_sixteen_fresh_bytes():
    first, second = generate_salt(), generate_salt()
    assert len(first) == len(second) == 16
    assert first != second


def test_b64url_has_no_padding_or_standard_alphabet():
    encoded = b64url(b"\xfb\xff\xfe" * 11 + b"\xff")
    assert not set("=+/") & set(encoded)
    assert decode_b64url(encoded, 34) == b"\xfb\xff\xfe" * 11 + b"\xff"


@pytest.mark.parametrize(
    "text",
    [
        "AAECAwQFBgcICQoLDA0ODx",  # leftover bits of the last character are not zero
        "AAECAwQFBgcICQoLDA0ODw==",  # padded
        "AAECAwQFBgcICQoLDA0O",  # 15 bytes
        "AAECAwQFBgcICQoLDA0ODxA",  # 17 bytes
        "AAECAwQFBgcICQoLDA0OD+",
        "AAECAwQFBgcICQoLDA0OD/",
        "AAECAwQFBgcICQoLDA0ODw\n",
        "A",
        "",
    ],
)
def test_decode_b64url_rejects_everything_but_the_canonical_form(text):
    with pytest.raises(PasswordResetError) as caught:
        decode_b64url(text, 16)
    assert caught.value.kind == "invalid_request"


def test_decode_b64url_accepts_the_canonical_form():
    assert decode_b64url("AAECAwQFBgcICQoLDA0ODw", 16) == bytes(range(16))


def _request_document() -> dict:
    return json.loads(build_reset_body(SALT, KEY))


def test_parse_reset_request_accepts_the_contract_example():
    assert parse_reset_request(_request_document()) == (SALT, KEY)


def _mutations():
    def change(path, value):
        def apply(document):
            target = document
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
        return apply

    def drop(path):
        def apply(document):
            target = document
            for part in path[:-1]:
                target = target[part]
            del target[path[-1]]
        return apply

    return {
        "extra top-level key": change(["password"], "leaked-plain-text"),
        "missing derived_key": drop(["derived_key"]),
        "missing kdf": drop(["kdf"]),
        "extra kdf key": change(["kdf", "hash"], "SHA-256"),
        "missing dk_len": drop(["kdf", "dk_len"]),
        "kdf is a list": change(["kdf"], ["PBKDF2-HMAC-SHA256"]),
        "other kdf name": change(["kdf", "name"], "PBKDF2-HMAC-SHA1"),
        "fewer iterations": change(["kdf", "iterations"], 1000),
        "iterations as text": change(["kdf", "iterations"], "600000"),
        "iterations as float": change(["kdf", "iterations"], 600000.0),
        "iterations as bool": change(["kdf", "iterations"], True),
        "other dk_len": change(["kdf", "dk_len"], 64),
        "salt of 15 bytes": change(["kdf", "salt"], b64url(SALT[:15])),
        "salt as number": change(["kdf", "salt"], 16),
        "non-canonical salt": change(["kdf", "salt"], "EBESExQVFhcYGRobHB0eHx"),
        "derived_key of 31 bytes": change(["derived_key"], b64url(KEY[:31])),
        "derived_key as list": change(["derived_key"], [b64url(KEY)]),
    }


@pytest.mark.parametrize("name", sorted(_mutations()))
def test_parse_reset_request_rejects_any_other_shape_without_echoing_it(name):
    document = _request_document()
    _mutations()[name](document)
    with pytest.raises(PasswordResetError) as caught:
        parse_reset_request(document)
    assert caught.value.kind == "invalid_request"
    assert str(caught.value) == "要求の形式が不正です。"
    assert "leaked-plain-text" not in str(caught.value)


@pytest.mark.parametrize("value", [None, [], "text", 1])
def test_parse_reset_request_rejects_non_objects(value):
    with pytest.raises(PasswordResetError) as caught:
        parse_reset_request(value)
    assert caught.value.kind == "invalid_request"


# --------------------------------------------------------------------------
# Password checks
# --------------------------------------------------------------------------


def test_password_needs_twelve_code_points():
    with pytest.raises(PasswordResetError) as caught:
        validate_new_password("a" * 11)
    assert caught.value.kind == "too_short"
    assert str(caught.value) == "閲覧パスワードは 12 文字(Unicode コードポイント)以上にしてください。"
    assert validate_new_password("a" * 12) == b"a" * 12
    # Twelve characters outside the BMP are twelve code points, not 24 units.
    assert validate_new_password("\U0001f3b8" * 12) == "\U0001f3b8".encode("utf-8") * 12


def test_password_is_not_normalized():
    text = "パスワードがじゅうにもじいじょう"
    nfc, nfd = unicodedata.normalize("NFC", text), unicodedata.normalize("NFD", text)
    assert nfc != nfd
    nfc_bytes, nfd_bytes = validate_new_password(nfc), validate_new_password(nfd)
    assert nfc_bytes != nfd_bytes
    assert derive_key(nfc_bytes, SALT) != derive_key(nfd_bytes, SALT)


def test_lone_surrogate_is_refused_without_quoting_it():
    with pytest.raises(PasswordResetError) as caught:
        validate_new_password("valid-prefix-\ud800-suffix")
    assert caught.value.kind == "not_utf8"
    assert str(caught.value) == "パスワードに UTF-8 にできない文字が含まれています。"
    assert "\ud800" not in str(caught.value) and "valid-prefix" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__ is True


def test_surrounding_whitespace_is_kept():
    assert validate_new_password("  twelve chars\t\n ") == b"  twelve chars\t\n "


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------


def test_supporting_pocket_passes_the_check():
    check_password_reset_support(capabilities())


@pytest.mark.parametrize(
    "value",
    [
        capabilities(password_reset=False),
        capabilities(password_reset=1),
        capabilities(password_reset="true"),
        {"api": {"major": 1}, "features": {"pan_split": True}},
        {"api": {"major": 1}},
        {"features": {"password_reset": True}},
        {"api": {"major": 2}, "features": {"password_reset": True}},
        {"api": {"major": True}, "features": {"password_reset": True}},
        {},
    ],
)
def test_other_capabilities_mean_an_older_pocket(value):
    with pytest.raises(PasswordResetError) as caught:
        check_password_reset_support(value)
    assert caught.value.kind == "unsupported"
    assert safe_probe_error(caught.value) == (
        "この音源ポケットは閲覧パスワードの再設定に未対応です。音源ポケットを 0.4.0 以降へ更新してください。"
        "閲覧パスワードは変わっていません。"
    )


@pytest.mark.parametrize("value", [[], ["features"], "text", None, 1])
def test_unreadable_capabilities_are_not_called_unsupported(value):
    with pytest.raises(PasswordResetError) as caught:
        check_password_reset_support(value)
    assert caught.value.kind == "undetermined"
    message = safe_probe_error(caught.value)
    assert message == "音源ポケットの対応状況を確認できませんでした。閲覧パスワードは変わっていません。"
    assert "未対応" not in message


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (PocketHTTPError(401, "UNAUTHORIZED", URL), "upload token が無効です。音源ポケットで token を再発行した場合は接続し直してください。"),
        (PocketHTTPError(404, None, URL), "接続先に音源ポケットの API が見つかりません。接続先 URL を確認してください。"),
        (PocketHTTPError(429, "RATE_LIMITED", URL), "音源ポケットが要求を制限しました。しばらく待って再実行してください。"),
        (PocketHTTPError(429, "RATE_LIMITED", URL, retry_after="30"), "音源ポケットが要求を制限しました。しばらく待って再実行してください。(目安: 30 秒後)"),
        (PocketHTTPError(503, "AUTH_UNAVAILABLE", URL), "音源ポケットを利用できません。時間をおいて再実行してください。"),
        (PocketHTTPError(500, "INTERNAL", URL), "音源ポケットとの通信に失敗しました(HTTP 500 INTERNAL)。"),
        (PocketHTTPError(500, None, URL), "音源ポケットとの通信に失敗しました(HTTP 500)。"),
        (PocketHTTPError(500, "[red]not a code[/red]", URL), "音源ポケットとの通信に失敗しました(HTTP 500)。"),
        (OSError("getaddrinfo failed for pocket.invalid"), "音源ポケットに接続できませんでした。"),
    ],
)
def test_probe_failures_say_the_password_is_unchanged(exc, expected):
    message = safe_probe_error(exc)
    assert message == expected + "閲覧パスワードは変わっていません。"
    assert "pocket.invalid" not in message


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_204_is_completed_from_the_response():
    client = FakeClient()
    outcome = run_reset(client, SALT, KEY)
    assert (outcome.state, outcome.via, outcome.error) == ("completed", "response", None)
    assert client.bodies == [build_reset_body(SALT, KEY)]
    assert client.auth_calls == 0


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "UNAUTHORIZED"),
        (403, "ORIGIN_MISMATCH"),
        (404, "NOT_FOUND"),
        (422, "UNSUPPORTED_KDF"),
        (429, "RATE_LIMITED"),
        (400, "INVALID_REQUEST"),
        (413, "PAYLOAD_TOO_LARGE"),
    ],
)
def test_a_rejection_by_the_pocket_is_not_applied_without_asking(status, code):
    client = FakeClient(PocketHTTPError(status, code, URL))
    outcome = run_reset(client, SALT, KEY)
    assert (outcome.state, outcome.via) == ("not_applied", "response")
    assert client.auth_calls == 0


def test_503_asks_for_a_retry_without_asking_for_the_result():
    client = FakeClient(PocketHTTPError(503, "AUTH_UNAVAILABLE", URL))
    outcome = run_reset(client, SALT, KEY)
    assert (outcome.state, outcome.via) == ("retry_later", "response")
    assert client.auth_calls == 0


@pytest.mark.parametrize(
    "failure",
    [
        OSError("timed out"),
        PocketHTTPError(502, None, URL),
        PocketHTTPError(500, "INTERNAL", URL),
        PocketHTTPError(200, "UNEXPECTED_RESPONSE", URL),
        PocketHTTPError(413, "RESPONSE_TOO_LARGE", URL),
        PocketHTTPError(302, None, URL),
    ],
)
@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ([_params(SALT)], "completed"),
        ([_params(OTHER_SALT)] * 3, "not_applied"),
        ([OSError("down")] * 3, "unknown"),
    ],
)
def test_a_lost_response_is_settled_by_asking_the_pocket(monkeypatch, failure, params, expected):
    monkeypatch.setattr(password_module, "CONFIRM_INTERVAL_SECONDS", 0)
    client = FakeClient(failure, list(params))
    announced = []
    outcome = run_reset(client, SALT, KEY, sleep=Sleeps(), on_confirm=lambda: announced.append(1))
    assert (outcome.state, outcome.via) == (expected, "auth_params")
    assert outcome.error is failure
    assert announced == [1]
    assert client.auth_calls >= 1


def test_outcome_repr_does_not_carry_the_error():
    outcome = ResetOutcome("not_applied", "response", PocketHTTPError(401, "UNAUTHORIZED", URL))
    assert "pocket.invalid" not in repr(outcome) and "error" not in repr(outcome)


def test_reset_with_password_derives_from_a_fresh_salt():
    client = FakeClient()
    password = "correct horse battery"
    outcome = reset_with_password(client, password)
    assert outcome.state == "completed"
    document = json.loads(client.bodies[0])
    salt, derived_key = parse_reset_request(document)
    assert derived_key == derive_key(password.encode("utf-8"), salt)


def test_reset_with_password_sends_nothing_for_a_short_password():
    client = FakeClient()
    with pytest.raises(PasswordResetError):
        reset_with_password(client, "short")
    assert client.bodies == []


# --------------------------------------------------------------------------
# Asking for the result
# --------------------------------------------------------------------------


def test_confirm_matches_at_once_without_waiting():
    client, sleeps = FakeClient(params=[_params(SALT)]), Sleeps()
    assert confirm_reset(client, SALT, sleep=sleeps) == "completed"
    assert client.auth_calls == 1 and sleeps == []


def test_confirm_waits_for_a_request_that_arrives_late():
    client, sleeps = FakeClient(params=[_params(OTHER_SALT), _params(OTHER_SALT), _params(SALT)]), Sleeps()
    assert confirm_reset(client, SALT, sleep=sleeps) == "completed"
    assert client.auth_calls == 3 and sleeps == [5.0, 5.0]


def test_confirm_reports_not_applied_after_three_mismatches():
    client, sleeps = FakeClient(params=[_params(OTHER_SALT)] * 3), Sleeps()
    assert confirm_reset(client, SALT, sleep=sleeps) == "not_applied"
    assert client.auth_calls == 3 and len(sleeps) == 2


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ([OSError("down")] * 3, "unknown"),
        ([_params(OTHER_SALT), _params(OTHER_SALT), OSError("down")], "unknown"),
        ([OSError("down"), OSError("down"), _params(OTHER_SALT)], "not_applied"),
    ],
)
def test_confirm_goes_by_the_last_attempt(params, expected):
    client, sleeps = FakeClient(params=params), Sleeps()
    assert confirm_reset(client, SALT, sleep=sleeps) == expected
    assert client.auth_calls == 3 and len(sleeps) == 2


@pytest.mark.parametrize(
    "unreadable",
    [
        ["not", "an", "object"],
        "text",
        {"schema_version": 1},
        {"kdf": "PBKDF2-HMAC-SHA256"},
        {"kdf": {"salt": 16}},
        {"kdf": {"salt": "EBESExQVFhcYGRobHB0eHx"}},  # non-canonical spelling of SALT
        {"kdf": {"salt": b64url(SALT[:15])}},
        PocketHTTPError(503, "AUTH_UNAVAILABLE", URL),
        PocketHTTPError(404, None, URL),
        PocketHTTPError(422, "INVALID_DOCUMENT", URL),
    ],
)
def test_confirm_treats_an_unusable_answer_as_unreadable(unreadable):
    client = FakeClient(params=[unreadable] * 3)
    assert confirm_reset(client, SALT, sleep=Sleeps()) == "unknown"
    assert client.auth_calls == 3


def test_confirm_compares_bytes_and_accepts_a_longer_salt():
    assert confirm_reset(FakeClient(params=[{"kdf": {"salt": b64url(SALT)}}]), SALT, sleep=Sleeps()) == "completed"
    longer = SALT + b"\x00"
    client = FakeClient(params=[{"kdf": {"salt": b64url(longer)}}] * 3)
    assert confirm_reset(client, SALT, sleep=Sleeps()) == "not_applied"


def test_confirm_reads_attempts_and_interval_when_called(monkeypatch):
    monkeypatch.setattr(password_module, "CONFIRM_ATTEMPTS", 5)
    monkeypatch.setattr(password_module, "CONFIRM_INTERVAL_SECONDS", 0.25)
    client, sleeps = FakeClient(params=[_params(OTHER_SALT)] * 5), Sleeps()
    assert confirm_reset(client, SALT, sleep=sleeps) == "not_applied"
    assert client.auth_calls == 5 and sleeps == [0.25] * 4


# --------------------------------------------------------------------------
# Wording
# --------------------------------------------------------------------------

AFTER_COMPLETION = [
    "音源ポケットにログインしていたすべての端末はログアウトされました。新しいパスワードで入り直してください。",
    "直前にログインに失敗していた場合は、待ち時間(最大 60 秒)が明けるまでログインできないことがあります。",
    "曲と upload token、Bunri の接続設定はそのままです。",
]
UNCHANGED = "閲覧パスワードは変わっていません。"


def test_completed_wording():
    assert outcome_messages(ResetOutcome("completed", "response")) == [
        "閲覧パスワードを決め直しました。", *AFTER_COMPLETION,
    ]
    assert outcome_messages(ResetOutcome("completed", "auth_params", OSError("x"))) == [
        "音源ポケットからの応答を確認できなかったため、結果を問い合わせました。再設定は完了していました。",
        *AFTER_COMPLETION,
    ]


def test_wording_after_asking_for_the_result():
    assert outcome_messages(ResetOutcome("not_applied", "auth_params", OSError("x"))) == [
        "音源ポケットからの応答を確認できなかったため、結果を問い合わせました。再設定は反映されていません。閲覧パスワードは以前のままです。",
        "同じ操作をもう一度実行してください。再実行では今回と同じパスワードを使ってください(遅れて届いた前回の要求が後から反映されても結果が変わらないようにするため)。",
    ]
    assert outcome_messages(ResetOutcome("unknown", "auth_params", OSError("x"))) == [
        "音源ポケットからの応答を確認できなかったため、結果を問い合わせましたが、確認できませんでした。",
        "再設定が完了したかどうかは分かりません。新しいパスワードでログインできれば完了しています。古いパスワードで入れる場合は完了していません。同じ操作をもう一度実行しても安全です。再実行では今回と同じパスワードを使ってください。",
    ]
    assert outcome_messages(ResetOutcome("retry_later", "response", PocketHTTPError(503, "AUTH_UNAVAILABLE", URL))) == [
        "音源ポケットの認証情報または保存領域を利用できませんでした。時間をおいて同じ操作を再実行してください。",
    ]


REJECTIONS = [
    (PocketHTTPError(401, "UNAUTHORIZED", URL), "upload token が無効です。音源ポケットで token を再発行した場合は接続し直してください。"),
    (PocketHTTPError(403, "ORIGIN_MISMATCH", URL), "音源ポケットが Origin の不一致として拒否しました。接続先 URL と音源ポケットの設定を確認してください。"),
    (PocketHTTPError(403, None, URL), "音源ポケットの手前で要求が拒否されました(HTTP 403)。接続先の保護設定を確認してください。"),
    (PocketHTTPError(404, "NOT_FOUND", URL), "音源ポケットに再設定の経路がありません。音源ポケットを 0.4.0 以降へ更新するか、接続先 URL を確認してください。"),
    (PocketHTTPError(422, "UNSUPPORTED_KDF", URL), "この Bunri の鍵導出方式を音源ポケットが受け付けません。Bunri を更新してください。"),
    (PocketHTTPError(422, "INVALID_REQUEST", URL), "音源ポケットが要求の形式を拒否しました(HTTP 422 INVALID_REQUEST)。Bunri と音源ポケットの版の組み合わせを確認してください。"),
    (PocketHTTPError(400, "INVALID_JSON", URL), "音源ポケットが要求の形式を拒否しました(HTTP 400 INVALID_JSON)。Bunri と音源ポケットの版の組み合わせを確認してください。"),
    (PocketHTTPError(405, None, URL), "音源ポケットが要求の形式を拒否しました(HTTP 405)。Bunri と音源ポケットの版の組み合わせを確認してください。"),
    (PocketHTTPError(411, None, URL), "音源ポケットが要求の形式を拒否しました(HTTP 411)。Bunri と音源ポケットの版の組み合わせを確認してください。"),
    (PocketHTTPError(413, "PAYLOAD_TOO_LARGE", URL), "音源ポケットが要求の形式を拒否しました(HTTP 413 PAYLOAD_TOO_LARGE)。Bunri と音源ポケットの版の組み合わせを確認してください。"),
    (PocketHTTPError(415, "UNSUPPORTED_MEDIA_TYPE", URL), "音源ポケットが要求の形式を拒否しました(HTTP 415 UNSUPPORTED_MEDIA_TYPE)。Bunri と音源ポケットの版の組み合わせを確認してください。"),
    (PocketHTTPError(429, "RATE_LIMITED", URL, retry_after="12"), "音源ポケットが要求を制限しました。しばらく待って再実行してください。(目安: 12 秒後)"),
    (PocketHTTPError(429, "RATE_LIMITED", URL), "音源ポケットが要求を制限しました。しばらく待って再実行してください。"),
    (PocketHTTPError(409, "CONFLICT", URL), "音源ポケットが要求を拒否しました(HTTP 409 CONFLICT)。"),
    (PocketHTTPError(418, "<script>alert(1)</script>", URL), "音源ポケットが要求を拒否しました(HTTP 418)。"),
]


@pytest.mark.parametrize(("exc", "expected"), REJECTIONS)
def test_rejection_wording_follows_status_and_code(exc, expected):
    assert outcome_messages(ResetOutcome("not_applied", "response", exc)) == [expected, UNCHANGED]


def test_no_result_says_both_changed_and_unchanged():
    outcomes = [
        ResetOutcome("completed", "response"),
        ResetOutcome("completed", "auth_params", OSError("x")),
        ResetOutcome("not_applied", "auth_params", OSError("x")),
        ResetOutcome("unknown", "auth_params", OSError("x")),
        ResetOutcome("retry_later", "response", PocketHTTPError(503, None, URL)),
        *[ResetOutcome("not_applied", "response", exc) for exc, _ in REJECTIONS],
    ]
    for outcome in outcomes:
        text = "".join(outcome_messages(outcome))
        assert not ("変わっていません" in text and "決め直しました" in text)
        assert "pocket.invalid" not in text
        if outcome.state in ("retry_later", "unknown"):
            assert "変わっていません" not in text and "決め直しました" not in text
