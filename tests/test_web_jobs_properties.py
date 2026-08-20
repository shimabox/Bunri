"""Property-based tests for the job store's load/save invariants.

The example-based tests in test_web_jobs.py each pin one failure that
actually happened: a truncated write, invalid UTF-8, a 10,000-level nested
array, a lone surrogate, a record too big to save back. Every one of those
was found in production rather than in the suite, and the list only ever
grew -- which is the signal that the *cases* were never the point. What
matters is the handful of properties they were all violating.

So this module states those properties directly and lets Hypothesis look for
the next case:

* P1  loading never raises, whatever is in the directory
* P2  read -> write -> read round-trips
* P3  nothing this app writes exceeds the record limit
* P4  a broken file is quarantined no matter which exception it provokes
* P5  a job is never re-run while its previous process is unaccounted for

The example tests stay where they are: they document the specific bugs and
they fail fast and legibly. These are the net underneath them.

Hypothesis runs derandomized here -- see conftest.py.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from stemlab.web.jobs import (
    MAX_ERROR_CHARS,
    MAX_JOB_FILE_BYTES,
    MAX_TITLE_CHARS,
    Job,
    JobStore,
    TerminationOutcome,
    _job_record_bytes,
    _serialize_job_within_limit,
    _validate_job_record,
    default_runner,
    safe_filename,
)

from test_web_jobs import FakeRunner, _make_upload, _wait_until

# ---------------------------------------------------------------------------
# strategies
# ---------------------------------------------------------------------------
# The full Unicode range, surrogates included: a lone surrogate is exactly
# the kind of value that decodes fine and then refuses to encode, and it took
# a production incident to notice. Anything that generates "text" here has to
# be able to produce one.
_ANY_TEXT = st.text(
    alphabet=st.characters(codec=None, min_codepoint=0, max_codepoint=0x10FFFF),
    max_size=40,
)

# Characters that have historically broken *something* somewhere, kept as an
# explicit menu so Hypothesis doesn't have to rediscover them by luck.
_NASTY = st.sampled_from(
    [
        "",
        "\ud800",  # lone surrogate: decodes, won't encode
        "\udfff",
        "\x00",  # NUL
        "\x1b[31m",  # control characters / ANSI
        "\r\n",
        "\N{ZERO WIDTH SPACE}",
        "�",
        "曲名",
        "🎸",
        "é",  # combining accent
        "../../etc/passwd",
        "%2e%2e",
        "T" * 300,  # past MAX_TITLE_CHARS
    ]
)

_TEXT = st.one_of(_ANY_TEXT, _NASTY, st.text(max_size=5))

# Values for the fields resolved against out_dir (log / upload / package).
# Path-shaped rather than merely textual, because what breaks on these is
# path *semantics*: "/" and "." have no name component and blow up
# `Path.with_suffix` when the pid sidecar is derived from them; an absolute
# path or one with ".." escapes the output directory; a value naming an
# existing directory is replaced at run time by a longer canonical one, which
# grows the record after it was admitted; and multi-byte names are shorter in
# characters than in bytes, which is the unit the record limit is in.
_PATHISH = st.sampled_from(
    [
        "/",
        ".",
        "..",
        "../../escape",
        "/etc/passwd",
        "web",  # a directory that always exists
        "web/",
        "web/logs",
        "web/logs/j-prop-0001.log",
        "web/uploads/song.mp3",
        "曲名/曲名.guitar.player.html",
        "🎸/🎸.guitar.player.html",
        "a/",
        "./x",
        "nested/deeply/enough/file.log",
    ]
)

_PATH_FIELD = st.one_of(st.none(), _TEXT, _PATHISH)

# Paths aimed squarely at the sentinels _plant_sentinels lays down, including
# the two routes out of the tree (a symlinked directory inside web/logs, and a
# symlink sitting in out_dir itself). Fuzzing alone would take a very long
# time to guess "Song/Song.original.mp3"; naming the targets is what turns
# P6 from a lottery into a statement.
_BLAST_TARGETS = st.sampled_from(
    [
        "Song/Song.original.mp3",  # the user's own audio, truncated by open("wb")
        "Song/Song.guitar.player.html",
        "web/uploads/song.mp3",
        "web/jobs/keep.txt",
        ".cache/stem.wav",
        "web/logs/escape/secret.txt",  # web/logs/escape is a symlink out of the tree
        "escape-hatch/secret.txt",  # so is out_dir/escape-hatch
        "../outside/secret.txt",
        "web/logs/../../outside/secret.txt",
    ]
)


def _plant_sentinels(out_dir: Path, outside: Path) -> dict[Path, Path]:
    """Lay a known file in every directory a job record could name, plus two
    symlinks pointing clean out of the tree, and return the set of files whose
    bytes must not change."""
    outside.mkdir(parents=True, exist_ok=True)
    (outside / "secret.txt").write_text("outside, untouchable", encoding="utf-8")

    for rel, content in [
        ("Song/Song.original.mp3", "the user's source audio"),
        ("Song/Song.guitar.player.html", "<html>a finished package</html>"),
        ("web/uploads/song.mp3", "an uploaded file"),
        ("web/jobs/keep.txt", "not a job record, but still ours"),
        (".cache/stem.wav", "an expensive intermediate"),
    ]:
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    (out_dir / "web" / "logs").mkdir(parents=True, exist_ok=True)
    # Two ways out of the tree that no amount of string checking can see.
    for link in (out_dir / "web" / "logs" / "escape", out_dir / "escape-hatch"):
        if not link.exists():
            link.symlink_to(outside, target_is_directory=True)

    watched = [out_dir / rel for rel, _ in [
        ("Song/Song.original.mp3", None), ("Song/Song.guitar.player.html", None),
        ("web/uploads/song.mp3", None), ("web/jobs/keep.txt", None),
        (".cache/stem.wav", None),
    ]]
    watched.append(outside / "secret.txt")
    return {p: p for p in watched}


def _neuter_spawn(monkeypatch) -> None:
    """Let `default_runner` run its real file handling without launching the
    separation CLI (and torch) behind it."""
    from stemlab.web import jobs as jobs_module

    class _InstantProc:
        pid = 4242

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(jobs_module.subprocess, "Popen", lambda *a, **kw: _InstantProc())


def _fingerprint(paths: dict[Path, Path]) -> dict[Path, object]:
    """Exact bytes of each watched file, or a marker if it is gone. Bytes,
    not mtimes: the question is whether the content changed."""
    out: dict[Path, object] = {}
    for path in paths:
        try:
            out[path] = path.read_bytes()
        except OSError:
            out[path] = "<missing>"
    return out

_STATUS = st.sampled_from(["queued", "running", "done", "error"])

_ISO = st.sampled_from(
    [
        "2026-01-01T00:00:00+00:00",
        "2026-07-12T09:30:15.123456+00:00",
        "2026-12-31T23:59:59-05:00",
    ]
)


@st.composite
def _job_payloads(draw, job_id: str = "j-prop-0001") -> dict:
    """Job-shaped dicts spanning every status, optional-field presence, and
    the nastier corners of Unicode and of path syntax.

    The path fields are drawn from a mix that deliberately includes the
    *derived* values -- the log path this id implies, the package this title
    and target imply -- alongside the hostile ones. Without them the loader
    would reject nearly every draw, and a property whose examples are almost
    all filtered out is a property that tests almost nothing: P1/P4/P6 want
    the hostile records, but P2 needs records that get accepted.
    """
    title = draw(_TEXT)
    target = draw(st.sampled_from(["guitar", "vocals", "bass", draw(_TEXT)]))
    safe = safe_filename(title)
    return {
        "id": job_id,
        "digest": draw(_TEXT),
        "title": title,
        "target": target,
        "status": draw(_STATUS),
        "created_at": draw(_ISO),
        "started_at": draw(st.one_of(st.none(), _ISO)),
        "finished_at": draw(st.one_of(st.none(), _ISO)),
        "error": draw(st.one_of(st.none(), _TEXT)),
        "package": draw(
            st.one_of(
                st.just(f"{safe}/{safe}.{target}.player.html"), st.none(), _PATH_FIELD
            )
        ),
        "log": draw(st.one_of(st.just(f"web/logs/{job_id}.log"), st.none(), _PATH_FIELD)),
        "upload": draw(
            st.one_of(st.just("web/uploads/song.mp3"), st.none(), _PATH_FIELD)
        ),
    }


def _encode_payload(payload: dict, style: str) -> bytes:
    """The same record in one of the several JSON spellings a file on disk
    might legitimately use -- compact, indented, or fully \\u-escaped. A
    loader that accepts a value in one spelling must accept it in all of
    them, and `ensure_ascii` is exactly the knob that decides whether a lone
    surrogate reaches the decoder as an escape or as a raw character."""
    if style == "compact":
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    elif style == "indented":
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    else:  # "escaped"
        text = json.dumps(payload, ensure_ascii=True, indent=1)
    return text.encode("utf-8", errors="surrogatepass")


_STYLE = st.sampled_from(["compact", "indented", "escaped"])


# Arbitrary file content: raw bytes on one side, JSON-ish text on the other,
# plus the shapes that have actually broken the loader (deep nesting, huge
# files, unterminated documents).
_PATHOLOGICAL_TEXT = st.sampled_from(
    [
        "",
        "{not json",
        "null",
        "[]",
        '"just a string"',
        "1e400",
        '{"id": "j-prop-0001"}',
        "[" * 12_000 + "]" * 12_000,  # RecursionError out of json.loads
        '{"id": "j-prop-0001", "title": "' + "x" * (MAX_JOB_FILE_BYTES + 10) + '"}',
        '{"id": "j-prop-0001", "title": "\\ud800"}',  # lone surrogate escape
        '{"id": "j-prop-0001", "title": "\x00"}',
    ]
)

_FILE_CONTENT = st.one_of(
    st.binary(max_size=200),
    _PATHOLOGICAL_TEXT.map(lambda s: s.encode("utf-8", errors="surrogatepass")),
    _job_payloads().flatmap(
        lambda p: _STYLE.map(lambda style: _encode_payload(p, style))
    ),
)


def _jobs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "web" / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _quarantined(jobs_dir: Path, stem: str) -> list[Path]:
    return [p for p in jobs_dir.iterdir() if p.name.startswith(f"{stem}.json.bad-")]


# ---------------------------------------------------------------------------
# P1: loading never raises, and every file is either loaded or quarantined
# ---------------------------------------------------------------------------
@given(content=_FILE_CONTENT)
@settings(max_examples=300)
def test_p1_loading_any_file_content_never_raises(tmp_path_factory, content):
    """Whatever a job file contains -- arbitrary bytes, a truncated write, a
    hand-edit, something adversarial -- constructing a JobStore must not
    raise. One unreadable file may cost its own record; it may never cost the
    server its startup.

    And the outcome is always one of exactly two things: the record loaded,
    or the file was quarantined. "Silently left in place, retried and failing
    forever on every future start" is the third outcome this rules out.
    """
    tmp_path = tmp_path_factory.mktemp("p1")
    jobs_dir = _jobs_dir(tmp_path)
    path = jobs_dir / "j-prop-0001.json"
    path.write_bytes(content)

    store = JobStore(tmp_path, runner=FakeRunner())  # must not raise
    try:
        loaded = store.get_job("j-prop-0001") is not None
        quarantined = bool(_quarantined(jobs_dir, "j-prop-0001"))

        assert loaded != quarantined, (
            "a job file must end up either loaded or quarantined, never both "
            "and never neither"
        )
        if quarantined:
            assert not path.exists(), "a quarantined file must not be left in place"
    finally:
        store.shutdown(join_timeout=5.0)


@given(payload=_job_payloads(), style=_STYLE)
def test_p1_a_bad_record_never_takes_a_good_one_down_with_it(tmp_path_factory, payload, style):
    """The blast radius of one bad file is that file. A healthy record sitting
    beside anything at all still loads."""
    tmp_path = tmp_path_factory.mktemp("p1b")
    jobs_dir = _jobs_dir(tmp_path)
    (jobs_dir / "j-prop-0001.json").write_bytes(_encode_payload(payload, style))
    (jobs_dir / "j-healthy.json").write_text(
        json.dumps(
            {
                "id": "j-healthy", "digest": "d", "title": "Song", "target": "guitar",
                "status": "done", "created_at": "2026-01-01T00:00:00+00:00",
                "package": "Song/Song.guitar.player.html",
            }
        ),
        encoding="utf-8",
    )

    store = JobStore(tmp_path, runner=FakeRunner())
    try:
        assert store.get_job("j-healthy") is not None
    finally:
        store.shutdown(join_timeout=5.0)


# ---------------------------------------------------------------------------
# P2: read -> write -> read round-trips
# ---------------------------------------------------------------------------
@given(payload=_job_payloads(), style=_STYLE)
def test_p2_an_accepted_record_round_trips_through_the_writer(tmp_path_factory, payload, style):
    """Whatever the loader accepts, the writer can save, and the loader
    accepts the result again -- unchanged apart from the one documented
    transformation (a long `error` being trimmed).

    This is the property the surrogate and the re-serialization-overflow bugs
    both broke: both were records the loader happily admitted and then could
    not write back, one raising UnicodeEncodeError and one silently producing
    a file the *next* start would quarantine. Neither was a bad case to
    handle; both were this invariant not holding.
    """
    tmp_path = tmp_path_factory.mktemp("p2")
    jobs_dir = _jobs_dir(tmp_path)
    (jobs_dir / "j-prop-0001.json").write_bytes(_encode_payload(payload, style))

    store = JobStore(tmp_path, runner=FakeRunner())
    # Stop the worker first: a queued/running record gets picked up on
    # construction, and this property is about one *fixed* record, not about
    # racing a thread that is legitimately rewriting it as we look.
    store.shutdown(join_timeout=5.0)
    try:
        job = store.get_job("j-prop-0001")
        assume(job is not None)  # only accepted records make a claim here

        encoded = _serialize_job_within_limit(job)

        # (a) it encodes...
        raw = encoded.encode("utf-8")
        # (b) ...within the limit...
        assert len(raw) <= MAX_JOB_FILE_BYTES

        # (c) ...and the loader accepts what came out.
        reparsed = json.loads(encoded)
        assert _validate_job_record(reparsed, "j-prop-0001") is None, (
            f"the writer produced a record the loader rejects: {reparsed!r}"
        )

        # (d) and the job rebuilt from it is the one we started with.
        rebuilt = Job.from_dict(reparsed)
        for field in ("id", "digest", "title", "target", "status", "created_at",
                      "started_at", "finished_at", "package", "log", "upload"):
            assert getattr(rebuilt, field) == getattr(job, field), field
        if job.error is None:
            assert rebuilt.error is None
        else:
            # The only permitted difference: a long error is clamped, keeping
            # its informative end.
            assert rebuilt.error is not None
            assert len(rebuilt.error) <= max(len(job.error), MAX_ERROR_CHARS)
            assert job.error.endswith(rebuilt.error[-40:] or rebuilt.error)
    finally:
        store.shutdown(join_timeout=5.0)  # idempotent; the worker is already gone


@given(
    slack=st.integers(min_value=-400, max_value=200),
    style=st.sampled_from(["compact", "indented"]),
)
@settings(max_examples=80)
def test_p2_a_record_near_the_size_limit_is_accepted_only_if_it_can_be_saved(
    tmp_path_factory, slack, style
):
    """The same round-trip property, swept across the size boundary rather
    than around Unicode -- because the two ways to violate it are unrelated
    and the string strategies above stay small on purpose.

    The trap this generalizes: the file on disk and the record written back
    are not the same size. A compact record passes the read-side check at
    exactly the limit and re-serializes larger, because the canonical form
    is indented. Put the bulk somewhere the writer cannot trim (`error` is
    the only field it can) and the record was accepted, re-saved oversized,
    and quarantined on the *next* start.

    The padding goes in `digest`, deliberately: `title` has a length cap of
    its own and would be refused by *that* rule before ever reaching the
    size branch, leaving this property testing nothing. `digest` is
    uncapped, untrimmable, and therefore the field that actually exercises
    the boundary.

    Whichever side of the boundary a given draw lands on, only one thing has
    to hold: whatever is accepted can be saved.
    """
    tmp_path = tmp_path_factory.mktemp("p2c")
    jobs_dir = _jobs_dir(tmp_path)

    base = {
        "id": "j-prop-0001", "digest": "", "title": "Song", "target": "guitar",
        "status": "queued", "created_at": "2026-01-01T00:00:00+00:00",
        "started_at": None, "finished_at": None, "error": None,
        "package": None, "log": None, "upload": None,
    }
    dump = (
        (lambda p: json.dumps(p, separators=(",", ":")))
        if style == "compact"
        else (lambda p: json.dumps(p, indent=2))
    )
    filler = MAX_JOB_FILE_BYTES + slack - len(dump(base))
    assume(filler > 0)
    base["digest"] = "d" * filler
    text = dump(base)
    (jobs_dir / "j-prop-0001.json").write_text(text, encoding="utf-8")

    store = JobStore(tmp_path, runner=FakeRunner())
    store.shutdown(join_timeout=5.0)

    job = store.get_job("j-prop-0001")
    if job is None:
        assert _quarantined(jobs_dir, "j-prop-0001"), (
            "a record that can't be used must be quarantined, not dropped silently"
        )
    else:
        assert _job_record_bytes(job) <= MAX_JOB_FILE_BYTES, (
            "accepted a record of "
            f"{len(text.encode('utf-8'))} bytes that re-serializes to "
            f"{_job_record_bytes(job)} -- it will be quarantined on the next start, "
            "and the job will vanish then rather than now"
        )


@given(
    slack=st.integers(min_value=-600, max_value=100),
    succeeds=st.booleans(),
    start_status=st.sampled_from(["queued", "running"]),
    # Multi-byte titles matter here specifically: `package` is derived from
    # the title, and a value that is shorter in characters than the one
    # replacing it can still be longer in bytes -- the unit the limit is in.
    title=st.sampled_from(["Song", "曲名", "🎸🎸🎸", "曲" * 60, "Ünïcödé Tïtlé"]),
    # `package` is either absent or the value this title and target derive --
    # the only two a record may carry. Whichever it is, succeeding fills it
    # in, and that is the growth being measured.
    stores_package=st.booleans(),
)
@settings(max_examples=60)
def test_p2_an_accepted_record_still_fits_after_the_job_actually_runs(
    tmp_path_factory, slack, succeeds, start_status, title, stores_package
):
    """Admission is a promise about the job's whole life, not about the
    moment it was read.

    A record is only ever *smallest* when it arrives: running the job fills
    in `started_at`, then `finished_at`, then (on success) `package`, which
    is derived from the title at roughly twice its length. So a queued record
    admitted just under the limit could be pushed over it by nothing more
    than succeeding -- written oversized with only a warning, and
    quarantined on the next start. The job would disappear one restart after
    the point where anything could still have been said about it.

    This drives the transition for real, through the runner, and checks the
    promise where it has to hold: on disk, afterwards, and on reload.
    """
    tmp_path = tmp_path_factory.mktemp("p2e")
    jobs_dir = _jobs_dir(tmp_path)
    upload = _make_upload(tmp_path)

    base = {
        "id": "j-prop-0001", "digest": "", "title": title, "target": "guitar",
        "status": start_status, "created_at": "2026-01-01T00:00:00+00:00",
        # A stored timestamp without microseconds is seven characters shorter
        # than the one a re-run writes over it -- more growth to reserve for.
        "started_at": "2026-01-01T00:00:01+00:00" if start_status == "running" else None,
        "finished_at": None, "error": None,
        "package": (
            f"{safe_filename(title)}/{safe_filename(title)}.guitar.player.html"
            if stores_package
            else None
        ),
        "log": "web/logs/j-prop-0001.log",
        "upload": str(upload.relative_to(tmp_path)),
    }
    # Padding in `digest`: uncapped and untrimmable, so it puts the record
    # near the boundary without tripping a different rule first. Measured in
    # bytes, since that is what the limit counts.
    encoded_len = len(json.dumps(base, separators=(",", ":")).encode("utf-8"))
    filler = MAX_JOB_FILE_BYTES + slack - encoded_len
    assume(filler > 0)
    base["digest"] = "d" * filler
    text = json.dumps(base, separators=(",", ":"))
    (jobs_dir / "j-prop-0001.json").write_text(text, encoding="utf-8")

    runner = FakeRunner(returncode=0 if succeeds else 1, write_player=succeeds)
    store = JobStore(tmp_path, runner=runner)
    try:
        job = store.get_job("j-prop-0001")
        assume(job is not None)  # only accepted records make a promise
        _wait_until(
            lambda: store.get_job("j-prop-0001").status in ("done", "error"), timeout=10.0
        )
        final = store.get_job("j-prop-0001")
        # The transition really did fill in the fields that grow the record.
        assert final.started_at is not None and final.finished_at is not None
        if final.status == "done":
            assert final.package is not None

        written = (jobs_dir / "j-prop-0001.json").stat().st_size
        assert written <= MAX_JOB_FILE_BYTES, (
            f"a record accepted at {len(text.encode('utf-8'))} bytes grew to {written} "
            "by running -- over the limit, so the job will be quarantined on the next start"
        )
    finally:
        store.shutdown(join_timeout=5.0)

    reloaded = JobStore(tmp_path, runner=FakeRunner())
    try:
        assert reloaded.get_job("j-prop-0001") is not None, (
            "the grown record must still be readable"
        )
        assert _quarantined(jobs_dir, "j-prop-0001") == []
    finally:
        reloaded.shutdown(join_timeout=5.0)


@given(
    over=st.integers(min_value=0, max_value=40),
    collisions=st.integers(min_value=2, max_value=12),
)
@settings(max_examples=40)
def test_p2_title_collision_suffixes_never_break_the_title_cap(
    tmp_path_factory, over, collisions
):
    """The title cap has to hold on the value actually stored, suffix and
    all.

    `create_job` truncated the requested title and *then* handed it to the
    collision resolver, which appended "-2" on top -- so a title at exactly
    the cap that collided came back one character over it. The app wrote a
    record its own loader would reject, and the job vanished on the next
    start. Uploading the same song title twice was enough.
    """
    tmp_path = tmp_path_factory.mktemp("p2f")
    store = JobStore(tmp_path, runner=FakeRunner())
    requested = "T" * (MAX_TITLE_CHARS + over)
    job_ids = []
    try:
        for i in range(collisions):
            upload = _make_upload(tmp_path, f"song{i}.mp3", content=f"audio-{i}".encode())
            job, created = store.create_job(upload, digest=f"d{i}", requested_title=requested)
            assert created, "distinct digests must not dedup onto each other"
            job_ids.append(job.id)
            assert len(job.title) <= MAX_TITLE_CHARS, (
                f"collision #{i} produced a {len(job.title)}-character title: "
                f"{job.title[-10:]!r}"
            )
        assert len({store.get_job(j).title for j in job_ids}) == collisions, (
            "each colliding job must still get a distinct title"
        )
        _wait_until(
            lambda: all(store.get_job(j).status in ("done", "error") for j in job_ids),
            timeout=15.0,
        )
    finally:
        store.shutdown(join_timeout=5.0)

    # And the records the app just wrote must be ones it can read back.
    reloaded = JobStore(tmp_path, runner=FakeRunner())
    try:
        for job_id in job_ids:
            assert reloaded.get_job(job_id) is not None, (
                f"{job_id} did not survive a restart -- the app wrote a record its own "
                "loader rejects"
            )
    finally:
        reloaded.shutdown(join_timeout=5.0)


@given(payload=_job_payloads(), style=_STYLE)
@settings(max_examples=50)  # two JobStores per example -- the priciest draw here
def test_p2_a_record_survives_an_actual_restart(tmp_path_factory, payload, style):
    """The same property end to end, through the real filesystem: a record
    the loader accepts is still there after the store writes it out and a
    fresh store reads it back. Terminal statuses are used so recovery doesn't
    re-run the job and change it for legitimate reasons."""
    tmp_path = tmp_path_factory.mktemp("p2b")
    payload = dict(payload, status="done")
    jobs_dir = _jobs_dir(tmp_path)
    (jobs_dir / "j-prop-0001.json").write_bytes(_encode_payload(payload, style))

    store = JobStore(tmp_path, runner=FakeRunner())
    try:
        job = store.get_job("j-prop-0001")
        assume(job is not None)
        store._write_job(job)  # what recovery/the worker would do
    finally:
        store.shutdown(join_timeout=5.0)

    reloaded = JobStore(tmp_path, runner=FakeRunner())
    try:
        survivor = reloaded.get_job("j-prop-0001")
        assert survivor is not None, "an accepted record must survive being written back"
        assert _quarantined(jobs_dir, "j-prop-0001") == []
        assert survivor.title == job.title
        assert survivor.status == job.status
    finally:
        reloaded.shutdown(join_timeout=5.0)


# ---------------------------------------------------------------------------
# P3: the writer never produces an oversized or unencodable record
# ---------------------------------------------------------------------------
@given(
    title=_TEXT,
    error=st.one_of(st.none(), _TEXT, st.integers(1, 3).map(lambda n: "E" * (10**n))),
    status=_STATUS,
)
def test_p3_the_serializer_always_returns_encodable_text(title, error, status):
    """Half of `_serialize_job_within_limit`'s contract holds unconditionally:
    whatever it is handed, what it returns can be written as UTF-8. (The size
    half is conditional -- see the next test -- because a title too large to
    save can't be trimmed away, and is refused at load instead.)"""
    job = Job(
        id="j-prop-0001", digest="d", title=title, target="guitar", status=status,
        created_at="2026-01-01T00:00:00+00:00", error=error,
    )

    encoded = _serialize_job_within_limit(job)

    encoded.encode("utf-8")  # must not raise
    assert json.loads(encoded)["id"] == "j-prop-0001"


@given(error=st.integers(0, 6).map(lambda n: "E" * (10**n)), status=_STATUS)
def test_p3_an_oversized_error_is_always_trimmed_into_the_limit(error, status):
    """`error` is the field with slack, so however big it gets, the record
    still fits. This is the half of the contract that is unconditional given
    a bounded title -- which create_job guarantees and the loader enforces."""
    job = Job(
        id="j-prop-0001", digest="d", title="Song", target="guitar", status=status,
        created_at="2026-01-01T00:00:00+00:00", error=error,
    )

    assert _job_record_bytes(job) <= MAX_JOB_FILE_BYTES


@given(
    title=_TEXT,
    log_text=st.one_of(
        _TEXT,
        st.integers(1, 6).map(lambda n: "boom " * (10**n)),  # up to ~5 MB, no newlines
        st.integers(1, 4).map(lambda n: "line\n" * (10**n)),
    ),
    fails=st.booleans(),
)
@settings(max_examples=120)
def test_p3_nothing_reachable_through_the_public_api_writes_an_oversized_record(
    tmp_path_factory, title, log_text, fails
):
    """The property stated over the paths a user can actually drive: any
    title, and any log a runner cares to emit. Whatever comes of it, the
    record on disk is within the limit and a restart can still read it.

    The 1.25 MB single-line log that started all this is one draw from this
    strategy rather than a case someone had to think of."""
    tmp_path = tmp_path_factory.mktemp("p3")
    upload = _make_upload(tmp_path)
    runner = FakeRunner(
        returncode=1 if fails else 0, write_player=not fails, log_text=log_text
    )
    store = JobStore(tmp_path, runner=runner)
    try:
        job, _ = store.create_job(upload, digest="d1", requested_title=title)
        _wait_until(lambda: store.get_job(job.id).status in ("done", "error"), timeout=10.0)

        assert len(job.title) <= MAX_TITLE_CHARS
        job.title.encode("utf-8")  # the stored title must be writable

        record = tmp_path / "web" / "jobs" / f"{job.id}.json"
        assert record.stat().st_size <= MAX_JOB_FILE_BYTES
    finally:
        store.shutdown(join_timeout=5.0)

    reloaded = JobStore(tmp_path, runner=FakeRunner())
    try:
        assert reloaded.get_job(job.id) is not None, "the job must survive a restart"
        assert _quarantined(tmp_path / "web" / "jobs", job.id) == []
    finally:
        reloaded.shutdown(join_timeout=5.0)


# ---------------------------------------------------------------------------
# P4: whichever exception a broken file provokes, startup continues
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "exc",
    [
        OSError("simulated: I/O error"),
        ValueError("simulated: not JSON"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "simulated: invalid start byte"),
        UnicodeEncodeError("utf-8", "\ud800", 0, 1, "simulated: surrogates not allowed"),
        RecursionError("simulated: maximum recursion depth exceeded"),
        TypeError("simulated: unexpected type"),
        MemoryError("simulated: out of memory"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_p4_any_exception_from_reading_one_file_is_contained(tmp_path, monkeypatch, exc):
    """P1's fuzzing is what finds the exception class nobody listed; this is
    the companion that says what must happen once one shows up. Reading a
    single job file may fail in any way at all -- the file goes to
    quarantine, the store comes up, and the healthy job beside it loads.

    Together the two are the point: forget to enumerate an exception class
    and the fuzz test catches you; enumerate one and this pins the behaviour.
    """
    from stemlab.web import jobs as jobs_module

    jobs_dir = _jobs_dir(tmp_path)
    (jobs_dir / "j-prop-0001.json").write_text("{}", encoding="utf-8")
    (jobs_dir / "j-healthy.json").write_text(
        json.dumps(
            {
                "id": "j-healthy", "digest": "d", "title": "Song", "target": "guitar",
                "status": "done", "created_at": "2026-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    real_read_text = jobs_module.Path.read_text

    def exploding_read_text(self, *args, **kwargs):
        if self.name == "j-prop-0001.json":
            raise exc
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(jobs_module.Path, "read_text", exploding_read_text)

    store = JobStore(tmp_path, runner=FakeRunner())
    try:
        assert store.get_job("j-prop-0001") is None
        assert _quarantined(jobs_dir, "j-prop-0001"), (
            f"{type(exc).__name__} must send the file to quarantine, not escape"
        )
        assert store.get_job("j-healthy") is not None
    finally:
        store.shutdown(join_timeout=5.0)


# ---------------------------------------------------------------------------
# P5: a job is never re-run while its previous process is unaccounted for
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("outcome", list(TerminationOutcome), ids=lambda o: o.name)
@pytest.mark.parametrize("status", ["queued", "running", "done", "error"])
def test_p5_a_job_is_re_run_exactly_when_its_old_process_is_accounted_for(
    tmp_path, monkeypatch, outcome, status
):
    """The whole state table at once, rather than one row per remembered
    incident: for every startup status crossed with every verdict the reaper
    can return, is the job handed to the runner again?

    It must be, and only be, when there is work outstanding (queued or
    running) *and* the old process is accounted for -- confirmed stopped, or
    confirmed never there. A FAILED verdict means the previous run may still
    be writing the same cache and package files, so re-running is precisely
    the thing that must not happen; a terminal status means there is nothing
    to re-run at all.
    """
    from stemlab.web import jobs as jobs_module

    upload = _make_upload(tmp_path)
    jobs_dir = _jobs_dir(tmp_path)
    (tmp_path / "web" / "logs").mkdir(parents=True, exist_ok=True)
    (jobs_dir / "j-prop-0001.json").write_text(
        json.dumps(
            {
                "id": "j-prop-0001", "digest": "d", "title": "Song", "target": "guitar",
                "status": status, "created_at": "2026-01-01T00:00:00+00:00",
                "started_at": "2026-01-01T00:00:01+00:00" if status != "queued" else None,
                "finished_at": "2026-01-01T00:00:02+00:00" if status in ("done", "error") else None,
                "package": "Song/Song.guitar.player.html" if status == "done" else None,
                "log": "web/logs/j-prop-0001.log",
                "upload": str(upload.relative_to(tmp_path)),
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        jobs_module, "terminate_pid_from_sidecar", lambda *a, **kw: outcome
    )

    runner = FakeRunner()
    store = JobStore(tmp_path, runner=runner)
    try:
        # Long enough for the worker to have picked the job up if it was
        # going to; the assertion below is about whether it ever was.
        time.sleep(0.4)

        old_process_accounted_for = outcome is not TerminationOutcome.FAILED
        has_work_outstanding = status in ("queued", "running")
        # A queued job was never started, so there is no old process for the
        # reaper to have an opinion about -- it's re-run regardless.
        should_re_run = has_work_outstanding and (
            status == "queued" or old_process_accounted_for
        )

        was_re_run = bool(runner.calls)
        assert was_re_run == should_re_run, (
            f"status={status} outcome={outcome.name}: "
            f"{'re-ran' if was_re_run else 'did not re-run'}, expected the opposite"
        )

        final = store.get_job("j-prop-0001")
        if status == "running" and not old_process_accounted_for:
            assert final.status == "running", (
                "a job whose old process is unaccounted for must be held, not re-queued"
            )
            assert runner.calls == [], "and above all, must not be re-run"
        if status in ("done", "error"):
            assert final.status == status, "a terminal job must be left alone"
    finally:
        store.shutdown(join_timeout=5.0)


@given(
    log=st.one_of(st.none(), _PATH_FIELD, _BLAST_TARGETS),
    upload=st.one_of(st.none(), _PATH_FIELD, _BLAST_TARGETS),
    package=st.one_of(st.none(), _PATH_FIELD, _BLAST_TARGETS),
    status=st.sampled_from(["queued", "running"]),
    # What is standing at the job's own canonical paths before it starts.
    # Deriving the log path defends against a record *naming* someone else's
    # file; it does nothing about a symlink planted at the derived name
    # itself, which is the more obvious attack precisely because the name is
    # predictable.
    plant=st.sampled_from(
        [None, "log->inside", "log->outside", "pid->inside", "pid->outside"]
    ),
    # The pid sidecar is only written by the real runner, so the fake one
    # cannot exercise that path at all.
    real_runner=st.booleans(),
)
@settings(max_examples=100)
def test_p6_a_recovered_job_can_only_ever_touch_its_own_log(
    tmp_path_factory, monkeypatch, log, upload, package, status, plant, real_runner
):
    """A job record says what a job *is*, not what files the server may
    write. Nothing a record can say may cause anything outside that job's own
    log to change.

    P1-P5 pinned that records can be read, written and safely re-run; none of
    them said anything about *reach*. So `log: "Song/Song.original.mp3"` was
    honoured, and since `default_runner` opens the log with "wb", merely
    restarting the server truncated the user's own audio -- with a symlinked
    directory under web/logs, files outside out_dir entirely. Path-string
    validation alone cannot state this property, because the damage is done
    through filesystem structure (symlinks) that strings don't describe. So
    the property is stated over the actual bytes on disk.

    Every file in the tree is fingerprinted before the store runs and after
    it finishes; only `web/logs/<id>.log` is allowed to differ.
    """
    tmp_path = tmp_path_factory.mktemp("p6")
    out_dir = tmp_path / "out"
    outside = tmp_path / "outside"
    sentinels = _plant_sentinels(out_dir, outside)

    upload_file = _make_upload(out_dir)
    jobs_dir = out_dir / "web" / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": "j-prop-0001", "digest": "d", "title": "Song", "target": "guitar",
        "status": status, "created_at": "2026-01-01T00:00:00+00:00",
        "started_at": "2026-01-01T00:00:01+00:00" if status == "running" else None,
        "finished_at": None, "error": None,
        "package": package, "log": log,
        "upload": upload if upload is not None else str(upload_file.relative_to(out_dir)),
    }
    (jobs_dir / "j-prop-0001.json").write_text(json.dumps(record), encoding="utf-8")
    logs_dir = out_dir / "web" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    own_log = logs_dir / "j-prop-0001.log"

    if plant is not None:
        which, _, where = plant.partition("->")
        victim = (
            out_dir / "Song" / "Song.original.mp3" if where == "inside"
            else outside / "secret.txt"
        )
        (logs_dir / f"j-prop-0001.{which}").symlink_to(victim)

    before = _fingerprint(sentinels)
    if real_runner:
        # default_runner for real, minus the separation subprocess: its file
        # operations (the truncating log open, the sidecar write) are the
        # dangerous ones and they run exactly as they do in production.
        _neuter_spawn(monkeypatch)
        runner = default_runner
    else:
        # A runner that writes no player, so the *only* write this job is
        # entitled to make is its own log. (A successful job legitimately
        # creates its package; allowing that would blunt the assertion.)
        runner = FakeRunner(returncode=1, write_player=False)
    store = JobStore(out_dir, runner=runner)
    try:
        # Let the worker get as far as it is ever going to.
        _wait_until(
            lambda: store.get_job("j-prop-0001") is None
            or store.get_job("j-prop-0001").status in ("done", "error", "running"),
            timeout=10.0,
        )
        time.sleep(0.3)
    finally:
        store.shutdown(join_timeout=5.0)

    after = _fingerprint(sentinels)
    changed = {p for p in before if before[p] != after.get(p)}
    assert changed == set(), (
        f"a job record reached files it has no business touching: {sorted(changed)}. "
        f"Only {own_log} may ever be written."
    )
    assert set(after) == set(before), "no sentinel may be deleted either"


@given(outcome=st.sampled_from(list(TerminationOutcome)))
def test_p5_a_failed_verdict_never_reaches_the_runner(tmp_path_factory, monkeypatch, outcome):
    """The one-line version of the table above, as a property: FAILED implies
    the runner is never called. Stated separately because it is the part
    whose violation costs real data -- two processes writing one cache."""
    from stemlab.web import jobs as jobs_module

    tmp_path = tmp_path_factory.mktemp("p5b")
    upload = _make_upload(tmp_path)
    jobs_dir = _jobs_dir(tmp_path)
    (tmp_path / "web" / "logs").mkdir(parents=True, exist_ok=True)
    (jobs_dir / "j-prop-0001.json").write_text(
        json.dumps(
            {
                "id": "j-prop-0001", "digest": "d", "title": "Song", "target": "guitar",
                "status": "running", "created_at": "2026-01-01T00:00:00+00:00",
                "started_at": "2026-01-01T00:00:01+00:00",
                "log": "web/logs/j-prop-0001.log",
                "upload": str(upload.relative_to(tmp_path)),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(jobs_module, "terminate_pid_from_sidecar", lambda *a, **kw: outcome)

    runner = FakeRunner()
    store = JobStore(tmp_path, runner=runner)
    try:
        time.sleep(0.3)
        if outcome is TerminationOutcome.FAILED:
            assert runner.calls == []
            assert store.get_job("j-prop-0001").status == "running"
        else:
            _wait_until(lambda: bool(runner.calls), timeout=5.0)
    finally:
        store.shutdown(join_timeout=5.0)
