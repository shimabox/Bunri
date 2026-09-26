"""`bunri player`: write existing packages' players again from the current
template.

A player is a static HTML file written when its package is built, so a
newer Bunri's player changes never reach the packages made before it. This
command rewrites only the player HTML, from what the package's sidecar
records; nothing is separated or split again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.text import Text

from bunri.lr_split_cli import _LEGACY_HINT, _all_names, _fail, _print, _safe_display
from bunri.package import PlayerRewriteOutcome, rewrite_players

app = typer.Typer(
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)


def _report(name: str, outcome: PlayerRewriteOutcome) -> None:
    # Package names and reasons come from the file system, so they go out as
    # plain Text (never parsed as markup) with control characters replaced.
    shown = _safe_display(name)
    if outcome.status == "done":
        labels = "、".join(spec.label_ja for spec in outcome.targets)
        line = Text.assemble(("完了:", "green"), f" {shown} ({labels})")
    elif outcome.status == "skipped":
        line = Text.assemble("スキップ:", f" {shown}: {_safe_display(outcome.reason or '')}")
    elif outcome.status == "legacy":
        line = Text.assemble(("再生成が必要:", "yellow"), f" {shown}")
    else:
        line = Text.assemble(
            ("失敗:", "red"), f" {shown}: {_safe_display(outcome.reason or '')}"
        )
    _print(line)


@app.command(
    help="Rewrite existing packages' HTML players with the current template. "
    "No audio is separated, split or rewritten."
)
def main(
    safe_name: Optional[str] = typer.Argument(None, metavar="SAFE_NAME"),
    output: Path = typer.Option(Path("out"), "--output", "-o", help="Output directory"),
    all_packages: bool = typer.Option(False, "--all", help="Process every package"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    if (safe_name is None) == (not all_packages):
        _fail("SAFE_NAME と --all のどちらか一方だけを指定してください。")
    out = output.resolve()
    names = _all_names(out) if all_packages else [safe_name]
    counts = {"done": 0, "skipped": 0, "failed": 0, "legacy": 0}
    last: PlayerRewriteOutcome | None = None
    for name in names:
        assert name is not None
        try:
            outcome = rewrite_players(out, name)
        except (OSError, RuntimeError, ValueError) as exc:
            if verbose:
                raise
            outcome = PlayerRewriteOutcome("failed", str(exc))
        _report(name, outcome)
        counts[outcome.status] += 1
        last = outcome

    if all_packages:
        _print(
            f"集計: 完了={counts['done']} スキップ={counts['skipped']} "
            f"失敗={counts['failed']} 再生成が必要={counts['legacy']}"
        )
        if counts["legacy"]:
            _print(_LEGACY_HINT)
        if counts["failed"]:
            raise typer.Exit(1)
        return
    assert last is not None
    if last.status == "legacy":
        _print(last.reason or _LEGACY_HINT)
        raise typer.Exit(1)
    if last.status == "failed":
        raise typer.Exit(1)


if __name__ == "__main__":
    app(prog_name="bunri player")
