"""Bunri command line interface."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Optional

import typer
from rich.console import Console

from bunri import __version__
from bunri.package import build_package
from bunri.registry import REGISTRY

app = typer.Typer(add_completion=False, rich_markup_mode="rich")
console = Console()


def dispatch() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "pocket":
        from bunri.pocket.cli import app as pocket_app

        sys.argv = [sys.argv[0], *sys.argv[2:]]
        pocket_app(prog_name="bunri pocket")
    else:
        app(prog_name="bunri")


def _validate_target(name: str) -> str:
    if name not in REGISTRY:
        valid = ", ".join(sorted(REGISTRY))
        raise typer.BadParameter(f"unknown target {name!r}; valid targets: {valid}")
    return name


def _validate_device(device: str) -> str:
    if device not in ("auto", "cpu", "mps", "cuda"):
        raise typer.BadParameter("--device must be auto, cpu, mps or cuda")
    return device


@app.command(
    help="Extract an instrument stem from a mixed audio file and build an "
    "offline practice package (target-only / backing / original + HTML player)."
)
def main(
    input_file: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="Audio file (mp3/wav/m4a…)"
    ),
    output: Path = typer.Option(Path("out"), "--output", "-o", help="Output directory"),
    target: str = typer.Option("guitar", help="Instrument to extract (registry key)"),
    model: Optional[str] = typer.Option(
        None,
        help="Separation model file (default: the target's registered model, e.g. "
        "guitar-specialized Mel-Roformer; e.g. htdemucs_6s.yaml for the 6-stem "
        "Demucs). An explicitly named model never falls back on failure",
    ),
    title: Optional[str] = typer.Option(None, help="Song title (default: file name)"),
    device: str = typer.Option(
        "auto",
        help="Separation device: auto|cpu|mps|cuda. An explicitly named device "
        "that isn't actually available raises rather than silently falling "
        "back to another one",
    ),
    mp3: bool = typer.Option(
        True, "--mp3/--no-mp3", help="Also write 192k mp3s (target/backing/original)"
    ),
    no_cache: bool = typer.Option(False, "--no-cache", help="Recompute every step"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    target = _validate_target(target)
    device = _validate_device(device)

    console.print(f"[bold]Bunri[/bold] v{__version__} — {title or input_file.stem}")
    try:
        package_dir = build_package(
            input_file.resolve(),
            output.resolve(),
            target=target,
            model=model,
            title=title,
            device=device,
            mp3=mp3,
            no_cache=no_cache,
        )
    except (RuntimeError, ValueError) as exc:
        if verbose:
            raise
        console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"package: [cyan]{package_dir}[/cyan]")


if __name__ == "__main__":
    dispatch()
