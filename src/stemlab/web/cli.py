"""`stemlab-web` entry point: start the local (127.0.0.1-only) StemLab web UI.

A separate script from `stemlab` (see [project.scripts] in pyproject.toml)
rather than a subcommand of it, so `stemlab song.mp3` keeps working exactly
as before -- stemlab/cli.py is untouched by this feature.
"""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

import typer
import uvicorn

app = typer.Typer(add_completion=False, rich_markup_mode="rich")


@app.command(
    help="Start the local StemLab web UI: upload audio from a browser, run "
    "the stemlab CLI in the background, and open the resulting practice "
    "player. Binds to 127.0.0.1 only (no LAN/authentication support)."
)
def main(
    port: int = typer.Option(8330, "--port", help="Port to bind on 127.0.0.1"),
    output: Path = typer.Option(
        Path("out"), "-o", "--out", help="Output directory (shared with the stemlab CLI)"
    ),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="Open the page in a browser once the server is up"
    ),
) -> None:
    # Imported lazily so `stemlab-web --help` (and any other pure-argparsing
    # path) never pays for constructing the FastAPI app / job store.
    from stemlab.web.app import create_app

    out_dir = output.resolve()
    application = create_app(out_dir)

    if open_browser:
        url = f"http://127.0.0.1:{port}/"
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(application, host="127.0.0.1", port=port)


if __name__ == "__main__":
    app()
