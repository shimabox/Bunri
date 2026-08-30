"""Secret-safe, redirect-refusing standard-library HTTP transport."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bunri.pocket.protocol import parse_json

METADATA_TIMEOUT_SECONDS = 30
MEDIA_TIMEOUT_SECONDS = 300
JSON_LIMIT_BYTES = 1_048_576


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class PocketHTTPError(RuntimeError):
    def __init__(self, status: int, code: str | None, url: str, *, retry_after: str | None = None, supported_major: Any = None) -> None:
        self.status, self.code, self.url = status, code, url
        self.retry_after, self.supported_major = retry_after, supported_major
        super().__init__(self.user_message())

    def user_message(self) -> str:
        messages = {
            401: "upload token が無効です。connect を再実行してください。",
            409: "Pocket と protocol の互換性がありません。client/server を更新してください。",
            413: "送信するデータが Pocket の上限を超えています。",
            422: "Pocket が文書または media の内容不一致を拒否しました。",
            428: "条件付き更新の契約が一致しません。client/server を更新してください。",
            503: "Pocket の認証または storage を利用できません。後で再実行してください。",
        }
        if self.status == 429:
            return "Pocket が要求を制限しました。" + (f" Retry-After: {self.retry_after}" if self.retry_after else "")
        return messages.get(self.status, f"Pocket HTTP error: status={self.status} code={self.code or 'unknown'} url={self.url}")


@dataclass(frozen=True)
class JSONDocument:
    value: dict[str, Any]
    etag: str


class PocketHTTPClient:
    def __init__(self, base_url: str, token: str, *, opener: Any = None, metadata_timeout: float = METADATA_TIMEOUT_SECONDS, media_timeout: float = MEDIA_TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/"); self._token = token
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self.metadata_timeout, self.media_timeout = metadata_timeout, media_timeout

    def _url(self, path: str) -> str: return self.base_url + "/api/v1/upload/" + path.lstrip("/")
    def _request(self, method: str, path: str, *, data: Any = None, headers: dict[str, str] | None = None, timeout: float | None = None, limit: int = JSON_LIMIT_BYTES) -> tuple[int, Any, bytes]:
        url = self._url(path); request_headers = {"Authorization": f"Bearer {self._token}", **(headers or {})}
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try: response = self._opener.open(request, timeout=timeout or self.metadata_timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read(JSON_LIMIT_BYTES + 1); code = None; supported = None
            try:
                envelope = json.loads(body[:limit]); error = envelope.get("error", {}); code = error.get("code"); supported = error.get("supported_schema_major")
            except Exception: pass
            raise PocketHTTPError(exc.code, code, url, retry_after=exc.headers.get("Retry-After"), supported_major=supported) from None
        with response:
            body = response.read(limit + 1)
            if len(body) > limit: raise PocketHTTPError(413, "RESPONSE_TOO_LARGE", url)
            return response.status, response.headers, body

    def capabilities(self) -> dict[str, Any]:
        status, _, body = self._request("GET", "capabilities")
        if status != 200: raise PocketHTTPError(status, "UNEXPECTED_STATUS", self._url("capabilities"))
        try: value = json.loads(body)
        except json.JSONDecodeError as exc: raise PocketHTTPError(422, "INVALID_DOCUMENT", self._url("capabilities")) from exc
        return value

    @staticmethod
    def _strong_etag(headers: Any, url: str) -> str:
        etag = headers.get("ETag")
        if not isinstance(etag, str) or etag.startswith("W/") or not re.fullmatch(r'"[^"\r\n]+"', etag):
            raise PocketHTTPError(422, "INVALID_ETAG", url)
        return etag

    def get_json(self, path: str) -> JSONDocument | None:
        try: status, headers, body = self._request("GET", path)
        except PocketHTTPError as exc:
            if exc.status == 404: return None
            raise
        if status != 200: raise PocketHTTPError(status, "UNEXPECTED_STATUS", self._url(path))
        try: value = parse_json(body)
        except ValueError as exc: raise PocketHTTPError(422, "INVALID_DOCUMENT", self._url(path)) from exc
        return JSONDocument(value, self._strong_etag(headers, self._url(path)))

    def put_json(self, path: str, data: bytes, condition: str) -> str:
        if len(data) > JSON_LIMIT_BYTES: raise PocketHTTPError(413, "PAYLOAD_TOO_LARGE", self._url(path))
        header = "If-None-Match" if condition == "*" else "If-Match"
        status, headers, _ = self._request("PUT", path, data=data, headers={header: condition, "Content-Type": "application/json", "Content-Length": str(len(data))})
        if status not in (200, 201): raise PocketHTTPError(status, "UNEXPECTED_STATUS", self._url(path))
        return self._strong_etag(headers, self._url(path))

    def head_media(self, song_id: str, name: str) -> tuple[str | None, int | None] | None:
        path = f"media/{song_id}/{name}"
        try: status, headers, _ = self._request("HEAD", path, limit=0)
        except PocketHTTPError as exc:
            if exc.status == 404: return None
            raise
        if status != 200: raise PocketHTTPError(status, "UNEXPECTED_STATUS", self._url(path))
        try: size = int(headers.get("Content-Length"))
        except (TypeError, ValueError): size = None
        return headers.get("X-Bunri-Content-SHA256"), size

    def put_media(self, song_id: str, name: str, path: Path, size: int, checksum: str) -> str | None:
        """Upload media and return the server-verified SHA-256 checksum.

        The empty PUT response body has a transport-level Content-Length of zero,
        so the response length is deliberately not treated as stored media size.
        """
        remote = f"media/{song_id}/{name}"
        with path.open("rb") as stream:
            status, headers, _ = self._request("PUT", remote, data=stream, headers={"Content-Type": "audio/mpeg", "Content-Length": str(size), "X-Bunri-Content-SHA256": checksum}, timeout=self.media_timeout, limit=0)
        if status not in (200, 201): raise PocketHTTPError(status, "UNEXPECTED_STATUS", self._url(remote))
        return headers.get("X-Bunri-Content-SHA256")
