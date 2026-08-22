"""Self-contained HTML practice player renderer.

The output is a single offline HTML file that plays three sibling audio files
(``<title>.original.mp3`` / ``.<target>.mp3`` / ``.backing.mp3``) in sync so a
learner can switch between the full mix, the isolated target instrument and
the minus-one backing while keeping their place, loop an A-B section and slow
the tempo down without changing pitch.

Only the HTML is emitted here; the audio is *not* embedded. The player refers
to each track by a plain relative URL on an ``<audio src>`` attribute, which is
the one media-loading path that keeps working under ``file://`` (fetch() and
Web-Audio ``decodeAudioData`` both trip over the null file:// origin). Tracks
passed as ``None`` are rendered as a disabled, "（無し）"-labelled button so a
missing stem degrades gracefully instead of erroring.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from jinja2 import Environment, PackageLoader, select_autoescape

# Matches render/html.py in tab-maker: autoescaping on for html/j2/xml so the
# title and the audio filenames are escaped into inert attribute/text content
# by Jinja.
_env = Environment(
    loader=PackageLoader("stemlab", "templates"),
    autoescape=select_autoescape(("html", "j2", "xml")),
)


def render_player(
    title: str,
    *,
    original: str | None,
    target: str | None,
    backing: str | None,
    instrument_label: str,
    generated_at: str | None = None,
) -> str:
    """Render the standalone practice player HTML.

    Args:
        title: Song title shown in the header.
        original: Relative filename of the full-mix track, or ``None`` if absent.
        target: Relative filename of the isolated target-instrument track, or ``None``.
        backing: Relative filename of the minus-one backing track, or ``None``.
        instrument_label: Japanese label for the target instrument (e.g. "ギター"),
            used to build the "{label}のみ" / "{label}なし" track names and header.
        generated_at: Footer timestamp; defaults to now if not given.

    Returns:
        A single self-contained HTML document (audio referenced, not embedded).
    """
    template = _env.get_template("player.html.j2")

    if generated_at is None:
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return template.render(
        title=title or "Untitled",
        # Percent-encoded here (not just left to Jinja's autoescaping, which
        # only escapes HTML syntax, not URL-reserved characters) so a
        # filename containing a space or "#" still resolves as a single
        # <audio src>: an unescaped space breaks the attribute value and an
        # unescaped "#" truncates the URL at a fragment. Doing this in the
        # renderer (rather than trusting the caller to have already
        # sanitized the filename) also covers preexisting on-disk files that
        # predate stricter filename sanitizing elsewhere in the app.
        original_src=quote(original) if original is not None else None,
        target_src=quote(target) if target is not None else None,
        backing_src=quote(backing) if backing is not None else None,
        instrument_label=instrument_label,
        generated_at=generated_at,
    )
