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


def test_vocals_spec_contents():
    spec = get_target("vocals")
    assert spec.stem_name == "Vocals"
    # Chosen by measured SDR from audio-separator's catalog (12.60, the top
    # vocals score in 0.44.3); a catalog built-in, no becruily bootstrap.
    assert spec.default_model == "vocals_mel_band_roformer.ckpt"
    assert spec.fallback_model == "htdemucs_6s.yaml"
    assert spec.label_ja == "ボーカル"


@pytest.mark.parametrize(
    ("target", "stem_name", "label"),
    [("bass", "Bass", "ベース"), ("drums", "Drums", "ドラム"), ("piano", "Piano", "ピアノ")],
)
def test_htdemucs_reuse_targets(target, stem_name, label):
    spec = get_target(target)
    assert spec.stem_name == stem_name
    assert spec.default_model == "htdemucs_6s.yaml"
    # Their default IS the shared fallback model; falling back to itself
    # would be pointless, so none is registered.
    assert spec.fallback_model is None
    assert spec.label_ja == label


def test_every_spec_key_matches_its_target_field():
    for key, spec in REGISTRY.items():
        assert spec.target == key
