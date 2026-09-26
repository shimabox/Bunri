from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import hashlib

import pytest

from bunri.package_metadata import PackageMetadata, SourceIdentity, TargetMetadata
from bunri.pocket.protocol import AssetInfo, ProtocolError, merge_library, merge_manifest, pan_split_supported, parse_json, stable_json, validate_library, validate_manifest

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


@pytest.mark.parametrize(
    ("capabilities", "expected"),
    [
        ({"api": {"major": 1}, "features": {"pan_split": True}}, True),
        ({"api": {"major": 1}}, False),
        ({"api": {"major": 1}, "features": {"pan_split": False}}, False),
        ({"api": {"major": 1}, "features": {"pan_split": "true"}}, False),
        ({"api": {"major": 1}, "features": {}}, False),
        ({"api": {"major": 1}, "features": None}, False),
        ({"api": {"major": 2}, "features": {"pan_split": True}}, False),
        ({"api": {"major": True}, "features": {"pan_split": True}}, False),
        ({"api": {"major": 1.0}, "features": {"pan_split": True}}, False),
        ({"features": {"pan_split": True}}, False),
        ([{"api": {"major": 1}, "features": {"pan_split": True}}], False),
        ("capabilities", False),
        (None, False),
    ],
)
def test_pan_split_supported_truth_table(capabilities, expected):
    assert pan_split_supported(capabilities) is expected


def _pan_split_fixture():
    return parse_json((FIXTURES / "valid/manifest-v1-pan-split.json").read_bytes())


def test_pan_split_fixture_covers_four_stems_single_and_two_stem_left_right():
    value = _pan_split_fixture()
    assert validate_manifest(value) is value
    by_target = {item["target"]: item for item in value["instruments"]}
    assert len(by_target["guitar"]["stems"]) == 4
    assert by_target["bass"]["pan_split"] == "single"
    assert by_target["drums"]["pan_split"] == "left_right" and len(by_target["drums"]["stems"]) == 2


def _guitar(value):
    return next(item for item in value["instruments"] if item["target"] == "guitar")


def _left_only(value):
    guitar = _guitar(value); guitar["stems"] = guitar["stems"][:3]


def _left_only_pair(value):
    guitar = _guitar(value); guitar["stems"] = [guitar["stems"][0], guitar["stems"][2]]


def _no_pan_split(value):
    del _guitar(value)["pan_split"]


def _single(value):
    _guitar(value)["pan_split"] = "single"


def _five(value):
    guitar = _guitar(value); guitar["stems"].append(deepcopy(guitar["stems"][3]))


def _duplicate(value):
    guitar = _guitar(value); guitar["stems"][3] = deepcopy(guitar["stems"][2])


def _both(value):
    _guitar(value)["pan_split"] = "both"


def _bass_with_guitar_left(value):
    bass = next(item for item in value["instruments"] if item["target"] == "bass")
    bass["pan_split"] = "left_right"
    bass["stems"] += deepcopy(_guitar(value)["stems"][2:])


def _swapped_paths(value):
    stems = _guitar(value)["stems"]
    stems[2]["path"], stems[3]["path"] = stems[3]["path"], stems[2]["path"]


@pytest.mark.parametrize(
    "mutate",
    [_left_only, _left_only_pair, _no_pan_split, _single, _five, _duplicate, _both, _bass_with_guitar_left, _swapped_paths],
)
def test_manifest_rejects_inconsistent_left_right(mutate):
    value = _pan_split_fixture()
    mutate(value)
    with pytest.raises(ProtocolError):
        validate_manifest(value)


SHA = "01316c8ec960ebe91747508e865d42eef794073d8d6c17eeb87d6f495bcb760b"
DIGEST = "0123456789abcdef0123456789abcdef01234567"


def _metadata(pan_split):
    return PackageMetadata("Sample Song", "Sample Song", SourceIdentity("sha1", DIGEST, DIGEST[:12]), (TargetMetadata("guitar", ("mp3",), pan_split),))


def _assets(*, lr: bool):
    names = [("original.mp3", None, None), ("guitar.mp3", "guitar", "target"), ("guitar.backing.mp3", "guitar", "backing")]
    if lr:
        names += [("guitar.left.mp3", "guitar", "left"), ("guitar.right.mp3", "guitar", "right")]
    return [AssetInfo(name, 24, SHA, target, role) for name, target, role in names]


def _merge(pan_split, *, lr=None, remote=None, supported=True, clock=lambda: "2026-09-27T00:00:00Z"):
    return merge_manifest(
        metadata=_metadata(pan_split),
        assets=_assets(lr=pan_split == "left_right" if lr is None else lr),
        remote=remote,
        include_original=True,
        clock=clock,
        pan_split_supported=supported,
    )


def test_merge_without_support_keeps_the_previous_document():
    for pan_split in (None, "left_right", "single"):
        manifest, _ = _merge(pan_split, supported=False, clock=lambda: "2026-08-30T00:00:00Z")
        assert stable_json(manifest) == (FIXTURES / "generated/manifest-v1-guitar-ja.json").read_bytes()


def test_merge_without_support_keeps_remote_pan_split_and_version():
    remote, _ = _merge("left_right")
    remote["schema_version"] = "1.1"
    remote["instruments"][0]["stems"] = remote["instruments"][0]["stems"][:2]
    manifest, changed = _merge(None, remote=remote, supported=False, clock=lambda: pytest.fail("clock called"))
    assert not changed
    assert manifest["schema_version"] == "1.1"
    assert manifest["instruments"][0]["pan_split"] == "left_right"


def test_merge_with_support_writes_left_right_stems_in_order():
    manifest, changed = _merge("left_right")
    assert changed and manifest["schema_version"] == "1.1"
    instrument = manifest["instruments"][0]
    assert instrument["pan_split"] == "left_right"
    assert [(stem["role"], stem["path"]) for stem in instrument["stems"]] == [
        ("target", "guitar.mp3"), ("backing", "guitar.backing.mp3"),
        ("left", "guitar.left.mp3"), ("right", "guitar.right.mp3"),
    ]


def test_merge_with_support_writes_single_and_leaves_unrecorded_alone():
    single, _ = _merge("single")
    assert single["schema_version"] == "1.1"
    assert single["instruments"][0]["pan_split"] == "single"
    assert len(single["instruments"][0]["stems"]) == 2
    unrecorded, _ = _merge(None)
    assert unrecorded["schema_version"] == "1.0"
    assert "pan_split" not in unrecorded["instruments"][0]
    assert len(unrecorded["instruments"][0]["stems"]) == 2


def test_merge_with_support_needs_both_lr_assets_for_four_stems():
    manifest, _ = _merge("left_right", lr=False)
    assert manifest["instruments"][0]["pan_split"] == "left_right"
    assert len(manifest["instruments"][0]["stems"]) == 2


def test_merge_upgrades_a_three_file_sync_and_detects_the_change():
    remote, _ = _merge("left_right", supported=False, clock=lambda: "2026-09-01T00:00:00Z")
    remote["instruments"][0]["stems"][0]["future"] = "kept"
    manifest, changed = _merge("left_right", remote=remote, clock=lambda: "2026-09-27T00:00:00Z")
    assert changed
    assert manifest["schema_version"] == "1.1"
    assert manifest["updated_at"] == "2026-09-27T00:00:00Z"
    assert len(manifest["instruments"][0]["stems"]) == 4
    assert manifest["instruments"][0]["stems"][0]["future"] == "kept"
    again, changed = _merge("left_right", remote=manifest, clock=lambda: pytest.fail("clock called"))
    assert not changed and again == manifest


def test_merge_keeps_a_newer_minor_and_carries_lr_stem_fields():
    remote, _ = _merge("left_right")
    remote["schema_version"] = "1.2"
    remote["instruments"][0]["stems"][2]["future_asset"] = "kept"
    manifest, changed = _merge("left_right", remote=remote, clock=lambda: pytest.fail("clock called"))
    assert not changed
    assert manifest["schema_version"] == "1.2"
    assert manifest["instruments"][0]["stems"][2]["future_asset"] == "kept"


def test_merge_drops_remote_pan_split_when_the_sidecar_has_none():
    remote, _ = _merge("left_right")
    manifest, changed = _merge(None, remote=remote)
    assert changed
    assert "pan_split" not in manifest["instruments"][0]
    assert [stem["role"] for stem in manifest["instruments"][0]["stems"]] == ["target", "backing"]
    assert manifest["schema_version"] == "1.1"
