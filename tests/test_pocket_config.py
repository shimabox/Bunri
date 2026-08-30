from __future__ import annotations

import base64
import stat

import pytest

from bunri.pocket.config import PocketConfig, read_config, save_config, validate_base_url, validate_capabilities, validate_token


TOKEN = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
CAPABILITIES = {"api": {"major": 1}, "schemas": {"manifest": {"major": 1, "latest": "1.0"}, "library": {"major": 1, "latest": "1.0"}}, "limits": {"media_bytes": 94371840, "json_bytes": 1048576}, "media": {"content_types": ["audio/mpeg"], "hash": "SHA-256", "conditional_json_put": True}}


@pytest.mark.parametrize(("raw", "expected"), [("https://example.invalid/", "https://example.invalid"), ("https://example.invalid/pocket/", "https://example.invalid/pocket"), ("http://localhost:8787/", "http://localhost:8787"), ("http://127.0.0.1/x", "http://127.0.0.1/x"), ("http://[::1]/", "http://[::1]")])
def test_valid_base_urls(raw, expected): assert validate_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["http://example.invalid", "https://u:p@example.invalid", "https://example.invalid?q=1", "https://example.invalid/#x", "noscheme"])
def test_invalid_base_urls(raw):
    with pytest.raises(ValueError): validate_base_url(raw)


def test_token_config_roundtrip_modes_and_repr(tmp_path):
    assert validate_token("  " + TOKEN + "\n") == TOKEN
    config = PocketConfig("https://example.invalid", TOKEN)
    assert TOKEN not in repr(config)
    assert save_config(tmp_path, config) == []
    assert read_config(tmp_path) == config
    assert stat.S_IMODE((tmp_path / ".pocket").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / ".pocket/config.json").stat().st_mode) == 0o600


def test_rejects_short_token_and_capability_mismatch():
    with pytest.raises(ValueError): validate_token(base64.urlsafe_b64encode(b"short").decode().rstrip("="))
    validate_capabilities(CAPABILITIES)
    bad = {**CAPABILITIES, "api": {"major": 2}}
    with pytest.raises(ValueError): validate_capabilities(bad)


def test_read_rejects_symlink(tmp_path):
    (tmp_path / ".pocket").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError): read_config(tmp_path)
