from __future__ import annotations

from pathlib import Path
import hashlib

import pytest

from bunri.package_metadata import PackageMetadata, SourceIdentity, TargetMetadata
from bunri.pocket.protocol import AssetInfo, ProtocolError, merge_library, merge_manifest, parse_json, stable_json, validate_library, validate_manifest

FIXTURES = Path(__file__).parent / "fixtures" / "bunri_pocket_protocol_v1"


def test_upstream_sample_media_matches_contract():
    data = (FIXTURES / "media/sample.mp3").read_bytes()
    assert len(data) == 24
    assert hashlib.sha256(data).hexdigest() == "01316c8ec960ebe91747508e865d42eef794073d8d6c17eeb87d6f495bcb760b"


def test_upstream_valid_and_invalid_fixtures():
    for path in (FIXTURES / "valid").glob("manifest*.json"):
        validate_manifest(parse_json(path.read_bytes()))
    validate_library(parse_json((FIXTURES / "valid/library-v1.json").read_bytes()))
    for path in (FIXTURES / "invalid").glob("*.json"):
        validator = validate_library if path.name.startswith("library") else validate_manifest
        with pytest.raises(ProtocolError): validator(parse_json(path.read_bytes()))


@pytest.mark.parametrize("name", ["manifest-v1", "library-v1"])
def test_upstream_stable_golden(name):
    value = parse_json((FIXTURES / "valid" / f"{name}.json").read_bytes())
    assert stable_json(value) == (FIXTURES / "stable" / f"{name}.stable.json").read_bytes()


@pytest.mark.parametrize("name", ["number-forms", "unicode-keys-nested", "lone-surrogates", "non-finite"])
def test_stable_json_boundary_golden(name):
    golden = (FIXTURES / "stable" / f"{name}.stable.json").read_bytes()
    assert stable_json(parse_json(golden)) == golden
    assert golden.endswith(b"\n") and not golden.endswith(b"\n\n") and not golden.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("name", ["number-forms", "unicode-keys-nested", "lone-surrogates"])
def test_stable_json_boundary_inputs_reproduce_golden(name):
    value = parse_json((FIXTURES / "stable" / f"{name}.input.json").read_bytes())
    assert stable_json(value) == (FIXTURES / "stable" / f"{name}.stable.json").read_bytes()


def test_non_finite_literal_reproduces_golden():
    value = {"nan": float("nan"), "negative": float("-inf"), "positive": float("inf")}
    assert stable_json(value) == (FIXTURES / "stable/non-finite.stable.json").read_bytes()


def test_generator_golden_and_noop_preserve_unknown_fields():
    metadata = PackageMetadata("Sample Song", "Sample Song", SourceIdentity("sha1", "0123456789abcdef0123456789abcdef01234567", "0123456789ab"), (TargetMetadata("guitar", ("mp3", "wav")),))
    sha = "01316c8ec960ebe91747508e865d42eef794073d8d6c17eeb87d6f495bcb760b"
    assets = [AssetInfo("original.mp3", 24, sha), AssetInfo("guitar.mp3", 24, sha, "guitar", "target"), AssetInfo("guitar.backing.mp3", 24, sha, "guitar", "backing")]
    manifest, changed = merge_manifest(metadata=metadata, assets=assets, remote=None, include_original=True, clock=lambda: "2026-08-30T00:00:00Z")
    assert changed and stable_json(manifest) == (FIXTURES / "generated/manifest-v1-guitar-ja.json").read_bytes()
    manifest["future"] = {"kept": True}
    same, changed = merge_manifest(metadata=metadata, assets=assets, remote=manifest, include_original=True, clock=lambda: pytest.fail("clock called"))
    assert not changed and same["future"] == {"kept": True}
    library, changed = merge_library(manifest=manifest, remote=None, clock=lambda: "2026-08-30T00:00:00Z")
    library.pop("future", None)
    manifest.pop("future")
    library, _ = merge_library(manifest=manifest, remote=None, clock=lambda: "2026-08-30T00:00:00Z")
    assert stable_json(library) == (FIXTURES / "generated/library-v1-guitar-ja.json").read_bytes()


def test_protocol_accepts_v1_minor_but_distinguishes_version_errors():
    value = parse_json((FIXTURES / "valid/manifest-v1.json").read_bytes())
    value["schema_version"] = "1.9"; validate_manifest(value)
    value["schema_version"] = "2.0"
    with pytest.raises(ProtocolError) as exc: validate_manifest(value)
    assert exc.value.code == "UNSUPPORTED_SCHEMA_MAJOR"
    value["schema_version"] = "one"
    with pytest.raises(ProtocolError) as exc: validate_manifest(value)
    assert exc.value.code == "INVALID_DOCUMENT"


def test_manifest_requires_original_key_but_accepts_null():
    value = parse_json((FIXTURES / "valid/manifest-v1-no-original.json").read_bytes())
    assert value["original"] is None
    validate_manifest(value)
    del value["original"]
    with pytest.raises(ProtocolError, match="original is required"):
        validate_manifest(value)


def test_remote_numbers_are_parsed_as_ecmascript_binary64():
    value = parse_json(b'{"unknown":9007199254740993}')
    assert stable_json(value) == b'{"unknown":9007199254740992}\n'


@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_remote_non_json_constants_are_rejected(constant):
    with pytest.raises(ProtocolError, match="invalid JSON document"):
        parse_json(b'{"unknown":' + constant + b"}")
