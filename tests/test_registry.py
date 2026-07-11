import pytest

from stemlab.registry import REGISTRY, TargetSpec, get_target


def test_guitar_spec_contents():
    spec = get_target("guitar")
    assert isinstance(spec, TargetSpec)
    assert spec.target == "guitar"
    assert spec.stem_name == "Guitar"
    assert spec.default_model == "mel_band_roformer_guitar_becruily.ckpt"
    assert spec.fallback_model == "htdemucs_6s.yaml"
    assert spec.label_ja == "ギター"


def test_guitar_is_registered_under_its_own_key():
    assert REGISTRY["guitar"].target == "guitar"


def test_unknown_target_raises_value_error_listing_valid_keys():
    with pytest.raises(ValueError, match="guitar"):
        get_target("nope")
