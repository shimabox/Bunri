"""`bunri lr-split`: add the L/R split to packages that already exist.

Packages built before the split existed lack the "L のみ" / "R のみ" tracks.
Re-uploading the song to the web UI reuses the finished job instead of
rebuilding, and the original audio may be gone anyway, so this command makes
the split from the separated stem kept in out/.cache instead.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import typer
from rich.text import Text

from bunri.local_package import all_package_names
from bunri.lock import ProcessLock, ProcessLockBusy
from bunri.package import (
    PanSplitOutcome,
    add_pan_split,
    console,
    describe_pan_split,
)
from bunri.registry import REGISTRY
from bunri.safepath import verified_mkdir

app = typer.Typer(
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)

_LEGACY_HINT = (
    "旧パッケージは元の入力音源から再生成してください。"
    "キャッシュが残っていれば分離処理は省略されます。"
)


def _safe_display(value: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "�", value)


def _print(line: str | Text) -> None:
    # One result per line, never re-wrapped by rich, so a line stays whole
    # for copying and for anything reading the output.
    console.print(line, markup=False, highlight=False, soft_wrap=True)


def _fail(message: str) -> None:
    _print(Text.assemble(("error:", "red"), f" {message}"))
    raise typer.Exit(1)


def _all_names(out: Path) -> list[str]:
    # `all_package_names` returns [] for an output directory it cannot read,
    # which would look like "nothing to do". Check it first so a wrong -o
    # fails instead of ending with an all-zero summary.
    shown = _safe_display(str(out))
    if not out.exists():
        _fail(f"出力先が見つかりません: {shown}")
    if not out.is_dir():
        _fail(f"出力先がディレクトリではありません: {shown}")
    try:
        list(out.iterdir())
    except OSError:
        _fail(f"出力先を読めません: {shown}")
    return all_package_names(out)


def _report(name: str, outcome: PanSplitOutcome) -> None:
    # Package names and reasons come from the file system, so they go out as
    # plain Text (never parsed as markup) with control characters replaced.
    shown = _safe_display(name)
    if outcome.status == "done":
        assert outcome.decision is not None
        line = Text.assemble(
            ("完了:", "green"), f" {shown} ({describe_pan_split(outcome.decision)})"
        )
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
    help="Add the L/R (stereo position) split tracks to existing packages, "
    "using the separated stem kept in the cache. No input audio is needed."
)
def main(
    safe_name: Optional[str] = typer.Argument(None, metavar="SAFE_NAME"),
    output: Path = typer.Option(Path("out"), "--output", "-o", help="Output directory"),
    target: str = typer.Option("guitar", "--target", help="Target to split"),
    all_packages: bool = typer.Option(False, "--all", help="Process every package"),
    force: bool = typer.Option(
        False, "--force", help="Recompute even when a result is already recorded"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    if (safe_name is None) == (not all_packages):
        _fail("SAFE_NAME と --all のどちらか一方だけを指定してください。")
    spec = REGISTRY.get(target)
    if spec is None or not spec.pan_split:
        _fail("この target は L/R 分割に対応していません")
    out = output.resolve()
    # Listed before the lock is taken: the lock creates out/.cache, which
    # would turn a mistyped output directory into an empty one.
    names = _all_names(out) if all_packages else [safe_name]
    try:
        lock = ProcessLock(
            verified_mkdir(out, ".cache") / "pan_split.lock",
            "別の lr-split が実行中です。完了後に再実行してください。",
        ).acquire()
    except (OSError, ProcessLockBusy) as exc:
        _fail(str(exc))
    try:
        counts = {"done": 0, "skipped": 0, "failed": 0, "legacy": 0}
        last: PanSplitOutcome | None = None
        for index, name in enumerate(names, 1):
            assert name is not None
            with console.status(
                Text.assemble(
                    ("lr-split", "bold"),
                    f" {_safe_display(name)} ({index}/{len(names)}) running…",
                )
            ):
                try:
                    outcome = add_pan_split(out, name, target=target, force=force)
                except (OSError, RuntimeError, ValueError) as exc:
                    if verbose:
                        raise
                    outcome = PanSplitOutcome("failed", str(exc))
            _report(name, outcome)
            counts[outcome.status] += 1
            last = outcome
    finally:
        lock.release()

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
    app(prog_name="bunri lr-split")
