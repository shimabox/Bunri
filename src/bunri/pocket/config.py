"""Per-output Pocket connection settings with protected atomic storage."""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from bunri.safepath import is_real_file_in, verified_mkdir


@dataclass(frozen=True)
class PocketConfig:
    base_url: str
    token: str = field(repr=False)


def validate_base_url(raw: str) -> str:
    try: parsed = urlsplit(raw)
    except ValueError as exc: raise ValueError("Pocket URL が不正です") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Pocket URL には https と host が必要です")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("Pocket URL に userinfo、query、fragment は指定できません")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Pocket URL の port が不正です") from exc
    if any(character.isspace() for character in parsed.netloc):
        raise ValueError("Pocket URL の host が不正です")
    if parsed.scheme == "http":
        loopback = parsed.hostname == "localhost"
        try: loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError: pass
        if not loopback: raise ValueError("http は localhost または loopback address だけで使用できます")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def validate_token(raw: str) -> str:
    token = raw.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token): raise ValueError("upload token の形式が不正です")
    try: decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except Exception as exc: raise ValueError("upload token の形式が不正です") from exc
    if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != token:
        raise ValueError("upload token の形式が不正です")
    if len(decoded) < 32: raise ValueError("upload token が短すぎます")
    return token


def validate_capabilities(value: object) -> None:
    expected = {
        ("api", "major"): 1, ("schemas", "manifest", "major"): 1,
        ("schemas", "manifest", "latest"): "1.0", ("schemas", "library", "major"): 1,
        ("schemas", "library", "latest"): "1.0", ("limits", "media_bytes"): 94_371_840,
        ("limits", "json_bytes"): 1_048_576, ("media", "hash"): "SHA-256",
        ("media", "conditional_json_put"): True,
    }
    for path, wanted in expected.items():
        current = value
        for part in path:
            if not isinstance(current, dict) or part not in current: raise ValueError(f"Pocket capabilities に {'.'.join(path)} がありません")
            current = current[part]
        if type(current) is not type(wanted) or current != wanted: raise ValueError(f"Pocket capabilities の {'.'.join(path)} が対応していません")
    media = value.get("media") if isinstance(value, dict) else None
    if not isinstance(media, dict) or not isinstance(media.get("content_types"), list) or "audio/mpeg" not in media["content_types"]:
        raise ValueError("Pocket capabilities が audio/mpeg に対応していません")


def _path(out_dir: Path) -> Path: return out_dir / ".pocket" / "config.json"


def save_config(out_dir: Path, config: PocketConfig) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    directory = verified_mkdir(out_dir, ".pocket")
    warnings: list[str] = []
    try: os.chmod(directory, 0o700)
    except OSError: warnings.append("警告: .pocket directory の権限を保証できないため、平文 token を保護できない可能性があります。")
    payload = json.dumps({"schema_version": 1, "base_url": config.base_url, "token": config.token}, ensure_ascii=False, indent=2) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=".config.tmp-", suffix=".json", dir=directory)
    temp = Path(temp_name)
    try:
        try: os.fchmod(fd, 0o600)
        except OSError: warnings.append("警告: 一時設定 file の権限を保証できないため、平文 token を保護できない可能性があります。")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, directory / "config.json")
        try:
            os.chmod(directory / "config.json", 0o600)
            if stat.S_IMODE(directory.stat().st_mode) != 0o700 or stat.S_IMODE((directory / "config.json").stat().st_mode) != 0o600: raise OSError("mode mismatch")
        except OSError: warnings.append("警告: config.json の権限を保証できないため、平文 token を保護できない可能性があります。")
        dir_fd: int | None = None
        try:
            dir_fd = os.open(directory, os.O_RDONLY); os.fsync(dir_fd)
        except OSError: pass
        finally:
            if dir_fd is not None: os.close(dir_fd)
    finally: temp.unlink(missing_ok=True)
    return warnings


def read_config(out_dir: Path) -> PocketConfig | None:
    directory, path = out_dir / ".pocket", _path(out_dir)
    if not directory.exists() and not directory.is_symlink(): return None
    if directory.is_symlink() or not directory.is_dir(): raise ValueError(f"Pocket 設定 directory が安全ではありません: {directory}")
    if not path.exists() and not path.is_symlink(): return None
    if not is_real_file_in(path, directory.resolve()): raise ValueError(f"Pocket 設定 file が安全ではありません: {path}")
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise ValueError("Pocket 設定を読み取れません") from exc
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1: raise ValueError("Pocket 設定 version が不正です")
    return PocketConfig(validate_base_url(value.get("base_url", "")), validate_token(value.get("token", "")))
