"""Bunri Pocket protocol v1 validation and deterministic JSON merging."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from bunri.registry import REGISTRY

SONG_ID = re.compile(r"[0-9a-f]{12}\Z")
SHA1 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
TARGET = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\Z")


class ProtocolError(ValueError):
    def __init__(self, message: str, *, code: str = "INVALID_DOCUMENT") -> None:
        super().__init__(message)
        self.code = code


def parse_json(data: bytes) -> Any:
    if data.startswith(b"\xef\xbb\xbf"):
        raise ProtocolError("JSON must not contain a BOM")
    try:
        # JSON.parse stores every number as an IEEE-754 Number. Converting both
        # integer and fractional tokens here is required before stable output;
        # otherwise an unknown field such as 9007199254740993 would retain a
        # precision JavaScript has already discarded.
        return json.loads(
            data.decode("utf-8"),
            parse_int=float,
            parse_float=float,
            parse_constant=_reject_non_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON document") from exc


def _reject_non_json_constant(_value: str) -> Any:
    raise ProtocolError("invalid JSON document")


def _array_index(key: str) -> int | None:
    if not re.fullmatch(r"0|[1-9]\d*", key):
        return None
    value = int(key)
    return value if value <= 4_294_967_294 else None


def _ordered_keys(value: dict[str, Any]) -> list[str]:
    # Object.fromEntries reorders ECMAScript array-index properties numerically.
    indexes = sorted(((_array_index(k), k) for k in value if _array_index(k) is not None))
    others = sorted((k for k in value if _array_index(k) is None), key=lambda s: tuple(map(ord, s)))
    return [k for _, k in indexes] + others


def _number(value: int | float) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        return "null"
    if value == 0:
        return "0"
    text = repr(value).lower()
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    if "e" in text:
        mantissa, exponent_text = text.split("e")
        exponent = int(exponent_text)
        digits = mantissa.lstrip("-").replace(".", "")
        digits = digits.rstrip("0") or "0"
        if 1e-6 <= absolute < 1e21:
            point = exponent + 1
            if point <= 0:
                return sign + "0." + "0" * (-point) + digits
            if point >= len(digits):
                return sign + digits + "0" * (point - len(digits))
            return sign + digits[:point] + "." + digits[point:]
        mantissa_out = digits[0] + (("." + digits[1:]) if len(digits) > 1 else "")
        return sign + mantissa_out + "e" + ("+" if exponent >= 0 else "") + str(exponent)
    if text.endswith(".0"):
        text = text[:-2]
    if absolute >= 1e21 or absolute < 1e-6:
        # repr normally chose decimal only near the lower JSON.stringify boundary.
        raw = format(absolute, ".15e")
        mantissa, exponent_text = raw.split("e")
        mantissa = mantissa.rstrip("0").rstrip(".")
        exponent = int(exponent_text)
        return sign + mantissa + "e" + ("+" if exponent >= 0 else "") + str(exponent)
    return text


def _serialize(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, str):
        normalized: list[str] = []
        index = 0
        while index < len(value):
            code_point = ord(value[index])
            if 0xD800 <= code_point <= 0xDBFF and index + 1 < len(value):
                low = ord(value[index + 1])
                if 0xDC00 <= low <= 0xDFFF:
                    normalized.append(chr(0x10000 + (code_point - 0xD800) * 0x400 + low - 0xDC00))
                    index += 2
                    continue
            normalized.append(value[index])
            index += 1
        text = json.dumps("".join(normalized), ensure_ascii=False, separators=(",", ":"))
        return re.sub(r"[\ud800-\udfff]", lambda match: f"\\u{ord(match.group()):04x}", text)
    if isinstance(value, list):
        return "[" + ",".join(_serialize(x) for x in value) + "]"
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return "{" + ",".join(_serialize(k) + ":" + _serialize(value[k]) for k in _ordered_keys(value)) + "}"
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def stable_json(value: Any) -> bytes:
    return (_serialize(value) + "\n").encode("utf-8")


def _version(value: Any) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+", value):
        raise ProtocolError("malformed schema_version")
    if not value.startswith("1."):
        raise ProtocolError("unsupported schema major", code="UNSUPPORTED_SCHEMA_MAJOR")


def _nonempty(value: Any, label: str, issues: list[str]) -> None:
    if not isinstance(value, str) or not value:
        issues.append(f"{label} must be a non-empty string")


def _utc(value: Any, label: str, issues: list[str]) -> None:
    if not isinstance(value, str) or not UTC.fullmatch(value):
        issues.append(f"{label} must be RFC3339 UTC")
        return
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        issues.append(f"{label} must be RFC3339 UTC")


def _asset(value: Any, path: str, label: str, issues: list[str]) -> None:
    if not isinstance(value, dict):
        issues.append(f"{label} must be an object")
        return
    if value.get("path") != path or "/" in str(value.get("path", "")) or "\\" in str(value.get("path", "")):
        issues.append(f"{label}.path is invalid")
    if value.get("content_type") != "audio/mpeg":
        issues.append(f"{label}.content_type is invalid")
    size = value.get("bytes")
    if (
        isinstance(size, bool)
        or not isinstance(size, (int, float))
        or not math.isfinite(size)
        or not float(size).is_integer()
        or size <= 0
    ):
        issues.append(f"{label}.bytes must be positive")
    checksum = value.get("sha256")
    if not isinstance(checksum, str) or not SHA256.fullmatch(checksum):
        issues.append(f"{label}.sha256 is invalid")


def validate_manifest(value: Any, route_song_id: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("manifest must be an object")
    _version(value.get("schema_version"))
    issues: list[str] = []
    song = value.get("song_id")
    if not isinstance(song, str) or not SONG_ID.fullmatch(song): issues.append("song_id is invalid")
    if route_song_id is not None and song != route_song_id: issues.append("route song id differs from document")
    _nonempty(value.get("title"), "title", issues); _utc(value.get("updated_at"), "updated_at", issues)
    source = value.get("source")
    if not isinstance(source, dict): issues.append("source must be an object")
    else:
        digest, key = source.get("digest"), source.get("cache_key")
        if source.get("algorithm") != "sha1": issues.append("source.algorithm must be sha1")
        if not isinstance(digest, str) or not SHA1.fullmatch(digest): issues.append("source.digest is invalid")
        if not isinstance(key, str) or not SONG_ID.fullmatch(key): issues.append("source.cache_key is invalid")
        if isinstance(song, str) and isinstance(digest, str) and isinstance(key, str) and (key != song or digest[:12] != song): issues.append("source identity fields disagree")
    if "original" not in value:
        issues.append("original is required")
    elif value["original"] is not None:
        _asset(value["original"], "original.mp3", "original", issues)
    instruments = value.get("instruments")
    if not isinstance(instruments, list): issues.append("instruments must be an array")
    else:
        seen: set[str] = set()
        for index, instrument in enumerate(instruments):
            if not isinstance(instrument, dict): issues.append(f"instruments[{index}] must be an object"); continue
            target = instrument.get("target")
            if not isinstance(target, str) or not TARGET.fullmatch(target) or target == "original": issues.append(f"instruments[{index}].target is invalid")
            elif target in seen: issues.append(f"target {target} is duplicated")
            else: seen.add(target)
            _nonempty(instrument.get("label"), f"instruments[{index}].label", issues)
            stems = instrument.get("stems")
            if not isinstance(stems, list) or len(stems) != 2: issues.append(f"instruments[{index}].stems must contain target and backing"); continue
            roles: set[str] = set()
            for stem in stems:
                if not isinstance(stem, dict) or stem.get("role") not in ("target", "backing"): issues.append(f"instruments[{index}] has an invalid stem role"); continue
                role = stem["role"]; roles.add(role)
                _asset(stem, f"{target}.mp3" if role == "target" else f"{target}.backing.mp3", f"instruments[{index}].{role}", issues)
            if roles != {"target", "backing"}: issues.append(f"instruments[{index}] must have each stem role exactly once")
    if issues: raise ProtocolError("; ".join(issues))
    return value


def validate_library(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict): raise ProtocolError("library must be an object")
    _version(value.get("schema_version")); issues: list[str] = []; _utc(value.get("updated_at"), "updated_at", issues)
    songs = value.get("songs")
    if not isinstance(songs, list): issues.append("songs must be an array")
    else:
        seen: set[str] = set()
        for i, song in enumerate(songs):
            if not isinstance(song, dict): issues.append(f"songs[{i}] must be an object"); continue
            sid = song.get("song_id")
            if not isinstance(sid, str) or not SONG_ID.fullmatch(sid): issues.append(f"songs[{i}].song_id is invalid")
            elif sid in seen: issues.append(f"song {sid} is duplicated")
            else: seen.add(sid)
            if isinstance(sid, str) and song.get("manifest") != f"tracks/{sid}/manifest.json": issues.append(f"songs[{i}].manifest is inconsistent")
            _nonempty(song.get("title"), f"songs[{i}].title", issues); _utc(song.get("updated_at"), f"songs[{i}].updated_at", issues)
            if not isinstance(song.get("has_original"), bool): issues.append(f"songs[{i}].has_original must be boolean")
            instruments = song.get("instruments")
            if not isinstance(instruments, list): issues.append(f"songs[{i}].instruments must be an array"); continue
            targets: set[str] = set()
            for instrument in instruments:
                if not isinstance(instrument, dict) or not isinstance(instrument.get("target"), str) or not TARGET.fullmatch(instrument["target"]) or instrument["target"] == "original": issues.append(f"songs[{i}] has invalid instrument"); continue
                if instrument["target"] in targets: issues.append(f"songs[{i}] has duplicate target")
                targets.add(instrument["target"]); _nonempty(instrument.get("label"), f"songs[{i}].instrument.label", issues)
    if issues: raise ProtocolError("; ".join(issues))
    return value


@dataclass(frozen=True)
class AssetInfo:
    remote_name: str
    bytes: int
    sha256: str
    target: str | None = None
    role: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def merge_manifest(*, metadata: Any, assets: list[AssetInfo], remote: dict[str, Any] | None, include_original: bool, clock: Callable[[], str] = _now) -> tuple[dict[str, Any], bool]:
    if remote is not None:
        validate_manifest(remote, metadata.source.cache_key)
        if remote["source"]["digest"] != metadata.source.digest: raise ProtocolError("source digest collision", code="DIGEST_COLLISION")
    result = deepcopy(remote) if remote is not None else {"schema_version": "1.0", "song_id": metadata.source.cache_key, "title": metadata.title, "source": {}, "original": None, "instruments": [], "updated_at": ""}
    result.update(song_id=metadata.source.cache_key, title=metadata.title)
    source = deepcopy(result.get("source", {})); source.update(algorithm="sha1", digest=metadata.source.digest, cache_key=metadata.source.cache_key); result["source"] = source
    by_name = {a.remote_name: a for a in assets}
    def asset_value(info: AssetInfo, old: Any = None) -> dict[str, Any]:
        value = deepcopy(old) if isinstance(old, dict) else {}; value.update(path=info.remote_name, content_type="audio/mpeg", bytes=info.bytes, sha256=info.sha256); return value
    if include_original: result["original"] = asset_value(by_name["original.mp3"], result.get("original"))
    old_inst = {x.get("target"): x for x in result.get("instruments", []) if isinstance(x, dict)}
    for entry in metadata.targets:
        old = deepcopy(old_inst.get(entry.target, {})); old.update(target=entry.target, label=REGISTRY[entry.target].label_ja)
        old_stems = {x.get("role"): x for x in old.get("stems", []) if isinstance(x, dict)}
        stems = []
        for role, name in (("target", f"{entry.target}.mp3"), ("backing", f"{entry.target}.backing.mp3")):
            stem = asset_value(by_name[name], old_stems.get(role)); stem["role"] = role; stems.append(stem)
        old["stems"] = stems; old_inst[entry.target] = old
    result["instruments"] = [old_inst[k] for k in sorted(old_inst)]
    previous_time = remote.get("updated_at") if remote else ""
    result["updated_at"] = previous_time
    changed = remote is None or stable_json(result) != stable_json(remote)
    if changed: result["updated_at"] = clock()
    validate_manifest(result, metadata.source.cache_key)
    return result, changed


def merge_library(*, manifest: dict[str, Any], remote: dict[str, Any] | None, clock: Callable[[], str] = _now) -> tuple[dict[str, Any], bool]:
    if remote is not None: validate_library(remote)
    result = deepcopy(remote) if remote is not None else {"schema_version": "1.0", "updated_at": "", "songs": []}
    songs = {x["song_id"]: x for x in result.get("songs", [])}
    old = deepcopy(songs.get(manifest["song_id"], {})); old_inst = {x.get("target"): x for x in old.get("instruments", []) if isinstance(x, dict)}
    for item in manifest["instruments"]:
        inst = deepcopy(old_inst.get(item["target"], {})); inst.update(target=item["target"], label=item["label"]); old_inst[item["target"]] = inst
    old.update(song_id=manifest["song_id"], title=manifest["title"], manifest=f"tracks/{manifest['song_id']}/manifest.json", has_original=manifest["original"] is not None, instruments=[old_inst[k] for k in sorted(old_inst)], updated_at=manifest["updated_at"])
    songs[manifest["song_id"]] = old; result["songs"] = [songs[k] for k in sorted(songs)]
    previous_time = remote.get("updated_at") if remote else ""; result["updated_at"] = previous_time
    changed = remote is None or stable_json(result) != stable_json(remote)
    if changed: result["updated_at"] = clock()
    validate_library(result); return result, changed
