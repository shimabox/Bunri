"""`bunri-web` entry point: start the local (127.0.0.1-only) Bunri web UI.

A separate script from `bunri` (see [project.scripts] in pyproject.toml)
rather than a subcommand of it, so `bunri song.mp3` keeps working exactly
as before -- bunri/cli.py is untouched by this feature.
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

import typer
import uvicorn

app = typer.Typer(add_completion=False, rich_markup_mode="rich")


@app.command(
    help="Start the local Bunri web UI: upload audio from a browser, run "
    "the bunri CLI in the background, and open the resulting practice "
    "player. Binds to 127.0.0.1 only (no LAN/authentication support)."
)
def main(
    port: int = typer.Option(8330, "--port", help="Port to bind on 127.0.0.1"),
    output: Path = typer.Option(
        Path("out"), "-o", "--out", help="Output directory (shared with the bunri CLI)"
    ),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the page in a browser once the server is up"
    ),
) -> None:
    # Imported lazily so `bunri-web --help` (and any other pure-argparsing
    # path) never pays for constructing the FastAPI app / job store.
    from bunri.web.app import create_app

    out_dir = output.resolve()
    application = create_app(out_dir)

    if open_browser:
        url = f"http://127.0.0.1:{port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(application, host="127.0.0.1", port=port)


if __name__ == "__main__":
    app()
