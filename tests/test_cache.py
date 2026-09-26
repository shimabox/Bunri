"""cache.stage_completed: finished at some point, whatever the params."""

from __future__ import annotations

from bunri import cache


def _completed_stage(tmp_path):
    cache_dir = tmp_path / "key"
    cache_dir.mkdir()
    stem = cache_dir / "guitar.wav"
    stem.write_bytes(b"audio")
    cache.write_stage_meta(cache_dir, "separate:guitar", 1, {"model": "a"})
    return cache_dir, stem


def test_real_meta_and_outputs_count_as_completed_for_any_params(tmp_path):
    cache_dir, stem = _completed_stage(tmp_path)
    assert cache.stage_completed(cache_dir, "separate:guitar", [stem])
    # stage_is_fresh would say no to other params; stage_completed does not ask.
    assert not cache.stage_is_fresh(cache_dir, "separate:guitar", 1, {"model": "b"}, [stem])


def test_missing_meta_is_not_completed(tmp_path):
    cache_dir, stem = _completed_stage(tmp_path)
    cache.clear_stage_meta(cache_dir, "separate:guitar")
    assert not cache.stage_completed(cache_dir, "separate:guitar", [stem])


def test_symlinked_output_is_not_completed(tmp_path):
    cache_dir, stem = _completed_stage(tmp_path)
    elsewhere = tmp_path / "elsewhere.wav"
    elsewhere.write_bytes(b"audio")
    stem.unlink()
    stem.symlink_to(elsewhere)
    assert not cache.stage_completed(cache_dir, "separate:guitar", [stem])


def test_missing_output_is_not_completed(tmp_path):
    cache_dir, stem = _completed_stage(tmp_path)
    stem.unlink()
    assert not cache.stage_completed(cache_dir, "separate:guitar", [stem])


def test_unreadable_meta_is_not_completed(tmp_path):
    cache_dir, stem = _completed_stage(tmp_path)
    (cache_dir / "separate:guitar.meta.json").write_text("{not json", encoding="utf-8")
    assert not cache.stage_completed(cache_dir, "separate:guitar", [stem])
