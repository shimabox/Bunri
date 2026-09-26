"""Practice-player renderer tests.

Two layers, matching the html/pdf renderers' style in tab-maker:

* Pure structural checks on render_player()'s string output (labels, disabled
  "missing" tracks, title escaping, no external resources).
* Real-behaviour Playwright checks that load the player from a ``file://`` URL
  with three ffmpeg-generated silent mp3s and drive it through window.__player
  — proving the very thing that bit this project before: plain ``<audio src>``
  playback (unlike fetch/decodeAudioData) actually works under file://.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from bunri.player import render_player

_LABEL = "ギター"  # instrument_label used throughout: matches tab-maker's guitar-only wording


# --------------------------------------------------------------------------
# structural (no browser)
# --------------------------------------------------------------------------
def _script_boundary_counts(html: str) -> tuple[int, int]:
    """Count <script> open/close boundaries the way a real HTML tokenizer does
    (see tab-maker's test_html.py for the full rationale): it ends a script
    element at the literal, case-insensitive "</script" byte sequence
    regardless of quoting."""
    opens = len(re.findall(r"<script(?=[\s>])", html, flags=re.IGNORECASE))
    closes = len(re.findall(r"</script\s*>", html, flags=re.IGNORECASE))
    return opens, closes


def test_track_labels_and_buttons_are_present():
    out = render_player(
        "My Song", original="a.mp3", target="b.mp3", backing="c.mp3", instrument_label=_LABEL
    )
    assert "原曲" in out
    assert "ギター" in out
    assert "ギターなし" in out
    assert 'data-track="original"' in out
    assert 'data-track="target"' in out
    assert 'data-track="backing"' in out


def test_provided_tracks_render_audio_elements_with_relative_src():
    out = render_player(
        "s",
        original="s.original.mp3",
        target="s.guitar.mp3",
        backing="s.backing.mp3",
        instrument_label=_LABEL,
    )
    # Plain relative src attributes — the file://-safe media path, no fetch().
    assert 'src="s.original.mp3"' in out
    assert 'src="s.guitar.mp3"' in out
    assert 'src="s.backing.mp3"' in out
    assert 'id="tm-audio-original"' in out
    assert 'id="tm-audio-target"' in out
    assert 'id="tm-audio-backing"' in out


def test_none_track_renders_disabled_unavailable_button_and_no_audio_element():
    out = render_player(
        "s", original="s.original.mp3", target=None, backing=None, instrument_label=_LABEL
    )

    # The present track has its audio element; the absent ones do not.
    assert 'id="tm-audio-original"' in out
    assert 'id="tm-audio-target"' not in out
    assert 'id="tm-audio-backing"' not in out

    # Absent tracks are disabled and labelled 無し at render time (no JS needed).
    target_btn = re.search(r'<button[^>]*data-track="target"[^>]*>.*?</button>', out, re.S)
    backing_btn = re.search(r'<button[^>]*data-track="backing"[^>]*>.*?</button>', out, re.S)
    assert target_btn and "disabled" in target_btn.group(0) and "（無し）" in target_btn.group(0)
    assert backing_btn and "disabled" in backing_btn.group(0) and "（無し）" in backing_btn.group(0)

    # The present track's button is neither disabled nor labelled 無し.
    original_btn = re.search(r'<button[^>]*data-track="original"[^>]*>.*?</button>', out, re.S)
    assert original_btn and "disabled" not in original_btn.group(0)
    assert "（無し）" not in original_btn.group(0)


def test_title_is_reflected_and_html_escaped():
    out = render_player(
        "Rock & Roll <band>", original="a.mp3", target=None, backing=None, instrument_label=_LABEL
    )
    assert "Rock &amp; Roll &lt;band&gt;" in out
    assert "<band>" not in out


def test_title_containing_script_tag_cannot_inject_markup():
    out = render_player(
        '</script><script>window.__pwn=1</script>',
        original="a.mp3",
        target=None,
        backing=None,
        instrument_label=_LABEL,
    )
    # Exactly one legitimate <script> (our player code); the payload is inert,
    # autoescaped text — it never becomes a second script element.
    assert _script_boundary_counts(out) == (1, 1)
    assert "<script>window.__pwn=1</script>" not in out


def test_generated_at_defaults_and_is_used_verbatim():
    assert re.search(
        r"Generated \d{4}-\d{2}-\d{2}",
        render_player("s", original="a.mp3", target=None, backing=None, instrument_label=_LABEL),
    )
    out = render_player(
        "s",
        original="a.mp3",
        target=None,
        backing=None,
        instrument_label=_LABEL,
        generated_at="2026-07-11 09:00:00",
    )
    assert "Generated 2026-07-11 09:00:00" in out


def test_player_test_hook_is_present():
    out = render_player(
        "s", original="a.mp3", target=None, backing=None, instrument_label=_LABEL
    )
    assert "window.__player" in out
    assert "state: function" in out


def test_output_is_fully_offline_no_external_resources():
    out = render_player(
        "s", original="a.mp3", target="b.mp3", backing="c.mp3", instrument_label=_LABEL
    )
    lowered = out.lower()
    assert "http://" not in lowered
    assert "https://" not in lowered
    assert "cdn" not in lowered
    assert "//fonts." not in lowered


def test_audio_src_percent_encodes_spaces_and_hash():
    # A filename passed straight to render_player (e.g. an existing on-disk
    # file that predates the sanitizer's #/% stripping, or a title with a
    # space) must still resolve as one valid <audio src> -- an unescaped
    # space breaks the attribute value and an unescaped "#" truncates the
    # URL at a fragment.
    out = render_player(
        "s",
        original="song #1 take.mp3",
        target="song #1 take.guitar.mp3",
        backing="song #1 take.backing.mp3",
        instrument_label=_LABEL,
    )
    assert 'src="song%20%231%20take.mp3"' in out
    assert 'src="song%20%231%20take.guitar.mp3"' in out
    assert 'src="song%20%231%20take.backing.mp3"' in out
    # The raw, unencoded filename must not appear anywhere as a src value.
    assert 'src="song #1 take.mp3"' not in out


def test_instrument_label_drives_track_button_text():
    out = render_player(
        "s", original="a.mp3", target="b.mp3", backing="c.mp3", instrument_label="ボーカル"
    )
    assert "ボーカルのみ" in out
    assert "ボーカルなし" in out
    assert "ボーカル練習用プレイヤー" in out


# --------------------------------------------------------------------------
# real behaviour (Playwright + ffmpeg, file://)
# --------------------------------------------------------------------------
def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


_HAVE_BROWSER = _chromium_available()
_HAVE_FFMPEG = shutil.which("ffmpeg") is not None
_needs_runtime = pytest.mark.skipif(
    not (_HAVE_BROWSER and _HAVE_FFMPEG),
    reason="playwright chromium and/or ffmpeg not available",
)


def _make_silence(path: Path, dur: float = 3.0) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", str(dur), "-q:a", "9", "-acodec", "libmp3lame",
            str(path), "-y",
        ],
        check=True,
    )


def _render_dir(
    tmp_path: Path,
    *,
    original: str | None = "song.original.mp3",
    target: str | None = "song.guitar.mp3",
    backing: str | None = "song.backing.mp3",
    create: tuple[str, ...] = ("original", "target", "backing"),
    dur: float = 3.0,
) -> Path:
    """Write the player HTML plus the requested sibling mp3s; return the html
    path. A name listed in `create` gets a real file; a named-but-not-created
    track exercises the runtime "missing file" degrade path."""
    names = {"original": original, "target": target, "backing": backing}
    for name in create:
        if names[name]:
            _make_silence(tmp_path / names[name], dur)
    html = render_player(
        "練習曲", original=original, target=target, backing=backing, instrument_label=_LABEL
    )
    page_path = tmp_path / "player.html"
    page_path.write_text(html, encoding="utf-8")
    return page_path


@contextmanager
def _open(page_path: Path):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        # Autoplay-policy arg lets evaluate-driven play() run without a gesture,
        # so tests are deterministic (a real user's button click is a gesture).
        browser = pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        try:
            page = browser.new_page()
            page.goto(page_path.as_uri())
            page.wait_for_function(
                "window.__player && typeof window.__player.state === 'function'",
                timeout=15_000,
            )
            yield page
        finally:
            browser.close()


@_needs_runtime
def test_player_loads_all_tracks_under_file_url(tmp_path):
    with _open(_render_dir(tmp_path)) as page:
        page.wait_for_function("window.__player.state().ready === true", timeout=15_000)
        st = page.evaluate("window.__player.state()")
        assert st["ready"] is True
        assert st["activeTrack"] == "original"
        assert st["duration"] > 0
        for name in ("original", "target", "backing"):
            assert st["tracks"][name]["available"] is True, name


@_needs_runtime
def test_track_switch_preserves_playback_position(tmp_path):
    with _open(_render_dir(tmp_path)) as page:
        page.wait_for_function("window.__player.state().ready === true", timeout=15_000)
        page.evaluate("window.__player.play()")
        page.wait_for_function("window.__player.state().currentTime > 0.3", timeout=8_000)

        before = page.evaluate("window.__player.state()")
        page.evaluate("window.__player.switchTrack('target')")
        after = page.evaluate("window.__player.state()")

        assert before["activeTrack"] == "original"
        assert after["activeTrack"] == "target"
        # The audible position jump at the switch is the inter-track drift, which
        # the every-frame resync keeps well under a perceptible threshold.
        assert after["lastSwitchDrift"] < 0.05, after["lastSwitchDrift"]
        # Position is maintained: not rewound to 0, not warped away.
        assert after["currentTime"] > 0.2
        assert abs(after["currentTime"] - before["currentTime"]) < 0.5


@_needs_runtime
def test_seek_keeps_all_tracks_sample_aligned(tmp_path):
    with _open(_render_dir(tmp_path)) as page:
        page.wait_for_function("window.__player.state().ready === true", timeout=15_000)
        page.evaluate("window.__player.seek(1.5)")
        times = page.evaluate(
            "({o:document.getElementById('tm-audio-original').currentTime,"
            "  g:document.getElementById('tm-audio-target').currentTime,"
            "  b:document.getElementById('tm-audio-backing').currentTime})"
        )
        assert abs(times["o"] - 1.5) < 0.05
        assert abs(times["g"] - 1.5) < 0.05
        assert abs(times["b"] - 1.5) < 0.05


@_needs_runtime
def test_ab_loop_is_reflected_in_state_and_wraps_playback(tmp_path):
    with _open(_render_dir(tmp_path)) as page:
        page.wait_for_function("window.__player.state().ready === true", timeout=15_000)

        # (c) A/B set from explicit positions is reflected in state.
        page.evaluate("window.__player.pause(); window.__player.seek(0.2); window.__player.setA();")
        page.evaluate("window.__player.seek(0.5); window.__player.setB();")
        loop = page.evaluate("window.__player.state().loop")
        assert abs(loop["a"] - 0.2) < 0.05
        assert abs(loop["b"] - 0.5) < 0.05
        assert loop["active"] is True

        # And it actually loops: play from inside the window and watch the
        # position stay bounded and fall back near A rather than running past B.
        page.evaluate("window.__player.seek(0.4); window.__player.play();")
        samples = []
        for _ in range(16):
            samples.append(page.evaluate("window.__player.state().currentTime"))
            page.wait_for_timeout(80)
        assert max(samples) < 0.7, samples          # never escapes far past B
        assert min(samples) < 0.35, samples         # observed wrapping back near A


@_needs_runtime
def test_ab_loop_at_track_end_keeps_playing_across_multiple_wraps(tmp_path):
    page_path = _render_dir(
        tmp_path,
        target=None,
        backing=None,
        create=("original",),
        dur=1.0,
    )
    with _open(page_path) as page:
        page.wait_for_function("window.__player.state().ready === true", timeout=15_000)
        # Keep the render loop from winning the race at exactly B: playback is
        # still real, but reaching the media endpoint must exercise `ended`.
        page.evaluate("window.requestAnimationFrame=function(){return 1;}")
        page.evaluate("window.__player.seek(0); window.__player.setA();")

        # Preserve the ordinary no-loop ending behaviour before using the
        # browser-reported endpoint as B.
        page.evaluate(
            "window.__player.seek(window.__player.state().duration - 0.2);"
            "window.__player.play();"
        )
        page.wait_for_function(
            "document.getElementById('tm-audio-original').ended === true",
            timeout=8_000,
        )
        stopped = page.evaluate(
            "(()=>{var el=document.getElementById('tm-audio-original');"
            "return {paused:el.paused, playing:window.__player.state().playing,"
            "button:document.getElementById('tm-play').textContent, end:el.currentTime};})()"
        )
        assert stopped["paused"] is True
        assert stopped["playing"] is False
        assert stopped["button"] == "再生"

        page.evaluate("window.__player.setB()")
        state = page.evaluate("window.__player.state()")
        assert abs(state["loop"]["a"] - 0) < 0.05
        assert state["loop"]["active"] is True
        assert abs(state["loop"]["b"] - stopped["end"]) < 0.05
        assert abs(state["loop"]["b"] - state["duration"]) < 0.05

        page.evaluate(
            "window.__player.seek(window.__player.state().duration - 0.2);"
            "window.__testEndLoopWraps=0;"
            "document.getElementById('tm-audio-original').addEventListener('seeking',function(){"
            "if(this.currentTime < 0.05) window.__testEndLoopWraps += 1;});"
            "window.__player.play();"
        )
        page.wait_for_function(
            "window.__testEndLoopWraps >= 1 &&"
            "document.getElementById('tm-audio-original').paused === false",
            timeout=8_000,
        )
        page.wait_for_function(
            "document.getElementById('tm-audio-original').currentTime >"
            "window.__player.state().loop.a + 0.08",
            timeout=8_000,
        )
        page.wait_for_function("window.__testEndLoopWraps >= 2", timeout=8_000)

        looping = page.evaluate(
            "({paused:document.getElementById('tm-audio-original').paused,"
            "playing:window.__player.state().playing,"
            "button:document.getElementById('tm-play').textContent,"
            "wraps:window.__testEndLoopWraps})"
        )
        assert looping["paused"] is False
        assert looping["playing"] is True
        assert looping["button"] == "一時停止"
        assert looping["wraps"] >= 2


@_needs_runtime
def test_playback_rate_change_sets_preserves_pitch(tmp_path):
    with _open(_render_dir(tmp_path)) as page:
        page.wait_for_function("window.__player.state().ready === true", timeout=15_000)
        page.evaluate("window.__player.setRate(0.5)")
        st = page.evaluate("window.__player.state()")
        assert st["rate"] == 0.5
        # Fact recorded from the real engine: Chromium exposes the standard
        # unprefixed `preservesPitch`, and we set it true so slowing down keeps
        # pitch. The renderer also assigns the webkit/moz spellings when present.
        assert st["preservesPitch"]["supported"] is True
        assert st["preservesPitch"]["value"] is True
        rates = page.evaluate(
            "({o:document.getElementById('tm-audio-original').playbackRate,"
            "  pp:document.getElementById('tm-audio-original').preservesPitch})"
        )
        assert rates["o"] == 0.5
        assert rates["pp"] is True


@_needs_runtime
def test_missing_track_file_degrades_gracefully(tmp_path):
    # backing is named but never written -> the <audio> errors at load time.
    page_path = _render_dir(tmp_path, create=("original", "target"))
    with _open(page_path) as page:
        page.wait_for_function("window.__player.state().ready === true", timeout=15_000)
        page.wait_for_function(
            "window.__player.state().tracks.backing.status === 'unavailable'",
            timeout=15_000,
        )
        st = page.evaluate("window.__player.state()")
        assert st["tracks"]["original"]["available"] is True
        assert st["tracks"]["target"]["available"] is True
        assert st["tracks"]["backing"]["available"] is False

        # Its button is disabled and relabelled; switching to it is a no-op.
        btn = page.evaluate(
            "(()=>{var b=document.querySelector('button.tm-trk[data-track=\"backing\"]');"
            "return {disabled:b.disabled, text:b.textContent};})()"
        )
        assert btn["disabled"] is True
        assert "（無し）" in btn["text"]
        page.evaluate("window.__player.switchTrack('backing')")
        assert page.evaluate("window.__player.state().activeTrack") != "backing"


@_needs_runtime
def test_play_button_click_starts_playback_as_user_gesture(tmp_path):
    # Exercises the real click path (a genuine user gesture) rather than the
    # evaluate() shortcut, so the button wiring itself is covered.
    with _open(_render_dir(tmp_path)) as page:
        page.wait_for_function("window.__player.state().ready === true", timeout=15_000)
        page.click("#tm-play")
        page.wait_for_function("window.__player.state().playing === true", timeout=8_000)
        page.wait_for_function("window.__player.state().currentTime > 0.1", timeout=8_000)
        assert page.evaluate("window.__player.state().playing") is True


# --------------------------------------------------------------------------
# L/R (stereo position split) tracks
# --------------------------------------------------------------------------
def _button(html: str, track: str) -> str | None:
    match = re.search(rf'<button[^>]*data-track="{track}"[^>]*>.*?</button>', html, re.S)
    return match.group(0) if match else None


def _markup(html: str) -> str:
    """The document without its script, where TRACKS/LABELS mention every
    track name regardless of what was rendered."""
    return html.split("<script>")[0]


def test_lr_buttons_are_absent_when_the_split_does_not_apply():
    out = render_player(
        "s", original="o.mp3", target="t.mp3", backing="b.mp3", instrument_label="ベース"
    )
    markup = _markup(out)
    assert _button(markup, "left") is None and _button(markup, "right") is None
    assert 'id="tm-audio-left"' not in markup and 'id="tm-pan-note"' not in markup
    assert 'class="tm-lr-group"' not in markup
    assert "<kbd>1</kbd>/<kbd>2</kbd>/<kbd>3</kbd> トラック切替" in markup
    assert "L のみ" not in markup


def test_lr_tracks_render_buttons_audio_and_help():
    out = render_player(
        "s",
        original="s.original.mp3",
        target="s.guitar.mp3",
        backing="s.guitar.backing.mp3",
        instrument_label=_LABEL,
        left="s.guitar.left.mp3",
        right="s.guitar.right.mp3",
    )
    markup = _markup(out)
    assert 'id="tm-audio-left" data-track="left" preload="auto" src="s.guitar.left.mp3"' in markup
    assert 'id="tm-audio-right" data-track="right" preload="auto" src="s.guitar.right.mp3"' in markup
    left, right = _button(markup, "left"), _button(markup, "right")
    assert left and "L のみ" in left and "disabled" not in left
    assert right and "R のみ" in right and "disabled" not in right
    # After the existing three buttons.
    assert markup.index('data-track="backing"') < markup.index('data-track="left"')
    assert "<kbd>1</kbd>〜<kbd>5</kbd> トラック切替" in markup
    assert "/ L のみ / R のみ) は、この HTML と同じフォルダに置いてください" in markup
    assert 'id="tm-pan-note"' not in markup and 'class="tm-lr-group"' not in markup
    assert "aria-describedby" not in markup
    assert 'case "4": switchTrack("left")' in out
    assert 'case "5": switchTrack("right")' in out


def test_lr_note_renders_disabled_buttons_and_the_reason():
    out = render_player(
        "s",
        original="s.original.mp3",
        target="s.guitar.mp3",
        backing="s.guitar.backing.mp3",
        instrument_label=_LABEL,
        pan_split_note="L/R に分かれていない曲です",
    )
    markup = _markup(out)
    left, right = _button(markup, "left"), _button(markup, "right")
    assert left and "disabled" in left and "（無し）" not in left
    assert right and "disabled" in right and "（無し）" not in right
    # The reason is a tooltip on the wrapper (disabled buttons may not show their
    # own title) plus visually-hidden text for screen readers, not a visible row.
    assert '<span class="tm-lr-group" title="L/R に分かれていない曲です">' in markup
    assert 'aria-describedby="tm-pan-note"' in left and 'aria-describedby="tm-pan-note"' in right
    assert '<span id="tm-pan-note" class="tm-visually-hidden">L/R に分かれていない曲です</span>' in markup
    assert "<p" not in markup.split('class="tm-lr-group"')[1].split("再生")[0]
    group = markup.split('<span class="tm-lr-group"')[1].split("</span>")[0]
    assert 'data-track="left"' in group and 'data-track="right"' in group
    assert 'id="tm-audio-left"' not in markup and 'id="tm-audio-right"' not in markup
    assert "/ L のみ / R のみ)" not in markup


def _render_lr_dir(tmp_path: Path, *, split: bool = True) -> Path:
    for name in ("song.original.mp3", "song.guitar.mp3", "song.guitar.backing.mp3"):
        _make_silence(tmp_path / name)
    if split:
        for name in ("song.guitar.left.mp3", "song.guitar.right.mp3"):
            _make_silence(tmp_path / name)
    html = render_player(
        "練習曲",
        original="song.original.mp3",
        target="song.guitar.mp3",
        backing="song.guitar.backing.mp3",
        instrument_label=_LABEL,
        left="song.guitar.left.mp3" if split else None,
        right="song.guitar.right.mp3" if split else None,
        pan_split_note=None if split else "L/R に分かれていない曲です",
    )
    page_path = tmp_path / "player.html"
    page_path.write_text(html, encoding="utf-8")
    return page_path


@_needs_runtime
def test_lr_track_switch_keeps_position_and_keys_4_5_select_lr(tmp_path):
    with _open(_render_lr_dir(tmp_path)) as page:
        page.wait_for_function("window.__player.state().ready === true", timeout=15_000)
        page.wait_for_function(
            "window.__player.state().tracks.left.available"
            " && window.__player.state().tracks.right.available",
            timeout=15_000,
        )
        page.evaluate("window.__player.play()")
        page.wait_for_function("window.__player.state().currentTime > 0.3", timeout=8_000)

        page.evaluate("window.__player.switchTrack('left')")
        after = page.evaluate("window.__player.state()")
        assert after["activeTrack"] == "left"
        assert after["lastSwitchDrift"] < 0.05, after["lastSwitchDrift"]

        page.keyboard.press("5")
        assert page.evaluate("window.__player.state().activeTrack") == "right"
        page.keyboard.press("4")
        assert page.evaluate("window.__player.state().activeTrack") == "left"


@_needs_runtime
def test_single_player_has_unavailable_lr_tracks(tmp_path):
    with _open(_render_lr_dir(tmp_path, split=False)) as page:
        page.wait_for_function("window.__player.state().ready === true", timeout=15_000)
        st = page.evaluate("window.__player.state()")
        assert st["tracks"]["left"]["available"] is False
        assert st["tracks"]["right"]["available"] is False
        disabled = page.evaluate(
            "['left','right'].map(function(t){return document.querySelector("
            "'button.tm-trk[data-track=\"'+t+'\"]').disabled;})"
        )
        assert disabled == [True, True]
        # The reason is not a visible row: the text is visually hidden, the
        # tooltip lives on the wrapper, and both buttons point at the text.
        info = page.evaluate(
            "(()=>{var n=document.getElementById('tm-pan-note');var r=n.getBoundingClientRect();"
            "var g=document.querySelector('.tm-lr-group');"
            "return {text:n.textContent,w:r.width,h:r.height,title:g.title,"
            "kids:g.querySelectorAll('button').length,"
            "desc:['left','right'].map(function(t){return document.querySelector("
            "'button.tm-trk[data-track=\"'+t+'\"]').getAttribute('aria-describedby');})};})()"
        )
        assert info["text"] == "L/R に分かれていない曲です"
        assert info["w"] <= 1 and info["h"] <= 1, info
        assert info["title"] == "L/R に分かれていない曲です"
        assert info["kids"] == 2 and info["desc"] == ["tm-pan-note", "tm-pan-note"]
        page.keyboard.press("4")
        assert page.evaluate("window.__player.state().activeTrack") == "original"
