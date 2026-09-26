from __future__ import annotations

import json
from pathlib import Path

import pytest

from bunri import cache
from bunri.package_metadata import PackageMetadata, SourceIdentity, TargetMetadata, begin_target, complete_target, read_package_metadata, write_package_metadata


def metadata() -> PackageMetadata:
    return PackageMetadata("Song", "Song", SourceIdentity("sha1", "a" * 40, "a" * 12), (TargetMetadata("guitar", ("mp3", "wav")),))


def test_sidecar_roundtrip_invalidation_and_merge(tmp_path):
    directory = tmp_path / "Song"; directory.mkdir(); path = directory / ".bunri-package.json"
    write_package_metadata(path, metadata())
    pending = begin_target(path, title="Song", safe_name="Song", digest="a" * 40, cache_key="a" * 12, target="guitar")
    assert pending.targets == () and read_package_metadata(path).targets == ()
    complete_target(path, expected=pending, target="guitar", formats=("wav",))
    assert read_package_metadata(path).targets[0].formats == ("wav",)


@pytest.mark.parametrize("version", [True, "1", 2])
def test_sidecar_rejects_non_integer_v1(tmp_path, version):
    directory = tmp_path / "Song"; directory.mkdir(); path = directory / ".bunri-package.json"
    value = json.loads(json.dumps({"schema_version": 1, "title": "Song", "safe_name": "Song", "source": {"algorithm": "sha1", "digest": "a" * 40, "cache_key": "a" * 12}, "targets": []}))
    value["schema_version"] = version; path.write_text(json.dumps(value))
    with pytest.raises(ValueError): read_package_metadata(path)


def test_sidecar_refuses_symlink_and_digest_collision(tmp_path):
    directory = tmp_path / "Song"; directory.mkdir(); victim = tmp_path / "victim"; victim.write_text("x")
    path = directory / ".bunri-package.json"; path.symlink_to(victim)
    with pytest.raises(ValueError): read_package_metadata(path)
    path.unlink(); write_package_metadata(path, metadata())
    with pytest.raises(ValueError, match="different input"): begin_target(path, title="Song", safe_name="Song", digest="b" * 40, cache_key="b" * 12, target="guitar")


def test_input_digest_and_cache_identity_keep_short_directory(tmp_path):
    source = tmp_path / "input"; source.write_bytes(b"content")
    digest = cache.input_digest(source)
    assert len(digest.full_sha1) == 40 and digest.cache_key == digest.full_sha1[:12]
    assert cache.file_digest(source) == digest.cache_key
    cache_dir = tmp_path / digest.cache_key; cache_dir.mkdir()
    cache.ensure_input_identity(cache_dir, digest)
    cache.ensure_input_identity(cache_dir, digest)
    with pytest.raises(ValueError, match="collision"):
        cache.ensure_input_identity(cache_dir, cache.InputDigest("b" * 40, digest.cache_key))


# ---------------------------------------------------------------------------
# pan_split field, set_target_pan_split, and the sidecar lock
# ---------------------------------------------------------------------------
def _sidecar(tmp_path: Path, *targets: TargetMetadata) -> Path:
    directory = tmp_path / "Song"
    directory.mkdir()
    path = directory / ".bunri-package.json"
    write_package_metadata(
        path,
        PackageMetadata("Song", "Song", SourceIdentity("sha1", "a" * 40, "a" * 12), targets),
    )
    return path


def _begin(path: Path, target: str) -> PackageMetadata:
    return begin_target(
        path, title="Song", safe_name="Song", digest="a" * 40, cache_key="a" * 12, target=target
    )


def test_pan_split_round_trips_and_is_omitted_when_unset(tmp_path):
    path = _sidecar(
        tmp_path,
        TargetMetadata("bass", ("mp3",)),
        TargetMetadata("guitar", ("mp3", "wav"), "left_right"),
    )
    value = json.loads(path.read_text())
    assert value["targets"] == [
        {"target": "bass", "formats": ["mp3"]},
        {"target": "guitar", "formats": ["mp3", "wav"], "pan_split": "left_right"},
    ]
    assert value["schema_version"] == 1
    assert read_package_metadata(path).targets[1].pan_split == "left_right"
    assert read_package_metadata(path).targets[0].pan_split is None


@pytest.mark.parametrize("value", ["both", "", None, 1, True, ["left_right"]])
def test_invalid_pan_split_is_rejected(tmp_path, value):
    path = _sidecar(tmp_path, TargetMetadata("guitar", ("mp3",)))
    raw = json.loads(path.read_text())
    raw["targets"][0]["pan_split"] = value
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="pan_split"):
        read_package_metadata(path)


def test_begin_and_complete_keep_other_targets_pan_split(tmp_path):
    path = _sidecar(tmp_path, TargetMetadata("guitar", ("mp3",), "single"))
    pending = _begin(path, "bass")
    assert pending.targets == (TargetMetadata("guitar", ("mp3",), "single"),)
    result = complete_target(path, expected=pending, target="bass", formats=("mp3",))
    assert result.targets == (
        TargetMetadata("bass", ("mp3",)),
        TargetMetadata("guitar", ("mp3",), "single"),
    )
    assert read_package_metadata(path) == result
    again = _begin(path, "guitar")
    complete_target(path, expected=again, target="guitar", formats=("mp3",), pan_split="left_right")
    assert read_package_metadata(path).targets == (
        TargetMetadata("bass", ("mp3",)),
        TargetMetadata("guitar", ("mp3",), "left_right"),
    )


def test_set_target_pan_split_changes_only_that_field(tmp_path):
    from bunri.package_metadata import TargetNotFoundError, set_target_pan_split

    path = _sidecar(
        tmp_path, TargetMetadata("bass", ("mp3",)), TargetMetadata("guitar", ("wav",))
    )
    before = read_package_metadata(path)

    result = set_target_pan_split(path, safe_name="Song", target="guitar", pan_split="single")

    assert result == read_package_metadata(path)
    assert result.targets == (
        TargetMetadata("bass", ("mp3",)),
        TargetMetadata("guitar", ("wav",), "single"),
    )
    assert (result.title, result.safe_name, result.source) == (
        before.title, before.safe_name, before.source
    )
    assert issubclass(TargetNotFoundError, ValueError)
    with pytest.raises(TargetNotFoundError):
        set_target_pan_split(path, safe_name="Song", target="vocals", pan_split="single")
    with pytest.raises(ValueError):
        set_target_pan_split(path, safe_name="Song", target="guitar", pan_split="both")
    assert read_package_metadata(path) == result


def test_every_update_takes_the_lock_under_out_cache(tmp_path):
    from bunri.package_metadata import set_target_pan_split

    lock = tmp_path / ".cache" / "metadata.lock"
    path = tmp_path / "Song" / ".bunri-package.json"
    path.parent.mkdir()
    pending = _begin(path, "guitar")
    assert lock.is_file() and not lock.is_symlink()
    lock.unlink()
    complete_target(path, expected=pending, target="guitar", formats=("mp3",))
    assert lock.is_file() and not lock.is_symlink()
    lock.unlink()
    set_target_pan_split(path, safe_name="Song", target="guitar", pan_split="single")
    assert lock.is_file() and not lock.is_symlink()


@pytest.mark.parametrize("paused", ["web", "cli"])
def test_concurrent_updates_are_serialized_not_lost(tmp_path, monkeypatch, paused):
    """One update stopped right after its read must hold the other off until
    it has written, and the other must then build on that write -- in either
    order both changes survive."""
    import threading

    from bunri import package_metadata
    from bunri.package_metadata import set_target_pan_split

    path = _sidecar(tmp_path, TargetMetadata("guitar", ("mp3",)))
    expected = read_package_metadata(path)
    original_read = package_metadata.read_package_metadata
    entered, proceed = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        if threading.current_thread().name == paused:
            entered.set()
            assert proceed.wait(10)
        return result

    monkeypatch.setattr(package_metadata, "read_package_metadata", read)

    def run(action):
        def body():
            try:
                action()
            except BaseException as exc:
                errors.append(exc)

        return body

    web = threading.Thread(
        name="web",
        target=run(
            lambda: complete_target(path, expected=expected, target="bass", formats=("mp3",))
        ),
    )
    cli = threading.Thread(
        name="cli",
        target=run(
            lambda: set_target_pan_split(
                path, safe_name="Song", target="guitar", pan_split="left_right"
            )
        ),
    )
    first, second = (web, cli) if paused == "web" else (cli, web)
    first.start()
    assert entered.wait(10)
    before = path.read_bytes()
    second.start()
    second.join(0.5)
    try:
        assert second.is_alive(), "the second update must wait for the lock"
        assert path.read_bytes() == before
    finally:
        proceed.set()
        first.join(10)
        second.join(10)
    assert errors == []
    assert original_read(path).targets == (
        TargetMetadata("bass", ("mp3",)),
        TargetMetadata("guitar", ("mp3",), "left_right"),
    )


def test_lock_wait_times_out_without_writing(tmp_path, monkeypatch):
    from bunri import package_metadata
    from bunri.lock import ProcessLock, ProcessLockBusy
    from bunri.package_metadata import set_target_pan_split

    path = _sidecar(tmp_path, TargetMetadata("guitar", ("mp3",)))
    (tmp_path / ".cache").mkdir()
    before = path.read_bytes()
    monkeypatch.setattr(package_metadata, "_LOCK_TIMEOUT", 0.2)
    held = ProcessLock(tmp_path / ".cache" / "metadata.lock", "held").acquire()
    try:
        with pytest.raises(ProcessLockBusy, match="身元ファイルの更新待ちがタイムアウトしました"):
            set_target_pan_split(path, safe_name="Song", target="guitar", pan_split="single")
    finally:
        held.release()
    assert path.read_bytes() == before
