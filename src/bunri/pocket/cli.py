"""Commands for connecting and synchronizing a Bunri Pocket shelf."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from bunri.pocket.config import PocketConfig, read_config, save_config, validate_base_url, validate_capabilities, validate_token
from bunri.pocket.http import PocketHTTPClient
from bunri.pocket.lock import SyncLock, SyncLockBusy
from bunri.pocket.service import PocketServiceError, safe_error, sync_all, sync_one

app = typer.Typer(add_completion=False, rich_markup_mode="rich")
console = Console()


def _fail(message: str) -> None:
    console.print(f"[red]error:[/red] {message}")
    raise typer.Exit(1)


@app.command()
def connect(
    url: str = typer.Argument(..., metavar="URL"),
    output: Path = typer.Option(Path("out"), "--output", "-o", help="Output directory"),
    token_stdin: bool = typer.Option(False, "--token-stdin", help="Read one token line from stdin"),
) -> None:
    try:
        base_url = validate_base_url(url)
        raw = sys.stdin.readline() if token_stdin else typer.prompt("Pocket upload token", hide_input=True)
        token = validate_token(raw)
        client = PocketHTTPClient(base_url, token)
        validate_capabilities(client.capabilities())
        warnings = save_config(output, PocketConfig(base_url, token))
    except (OSError, RuntimeError, ValueError) as exc: _fail(str(exc))
    for warning in warnings: console.print(warning, stderr=True)
    config_path = output / ".pocket" / "config.json"
    console.print(f"Pocket に接続しました: {base_url}")
    console.print(f"設定: {config_path}")
    console.print("注意: upload token はこのファイルに平文で保存されています。設定は -o ごとに分かれます。")
    console.print(f"接続情報を削除するには {config_path.parent} を削除してください。")


@app.command()
def sync(
    safe_name: Optional[str] = typer.Argument(None, metavar="SAFE_NAME"),
    output: Path = typer.Option(Path("out"), "--output", "-o", help="Output directory"),
    original: bool = typer.Option(True, "--original/--no-original", help="Upload original MP3"),
    all_packages: bool = typer.Option(False, "--all", help="Synchronize every package"),
) -> None:
    out = output
    if (safe_name is None) == (not all_packages):
        _fail("SAFE_NAME と --all のどちらか一方だけを指定してください。")
    try: config = read_config(out)
    except (OSError, ValueError) as exc: _fail(str(exc))
    if config is None:
        _fail("Pocket の接続設定がありません。アップロードは開始していません。\n先に接続してください:\n  bunri pocket connect <Pocket URL> -o " + shlex.quote(str(output)))
    try:
        lock = SyncLock(out).acquire()
    except (OSError, SyncLockBusy) as exc:
        _fail(safe_error(exc))
    try:
        if all_packages:
            batch = sync_all(out, include_original=original, lock=lock)
            for name in batch.legacy:
                console.print(f"[yellow]再生成が必要:[/yellow] {name}")
            for item in batch.items:
                if item.status == "done":
                    console.print(f"[green]完了:[/green] {item.safe_name}")
                elif item.status == "error":
                    console.print(f"[red]失敗:[/red] {item.safe_name}: {item.error}")
                else:
                    console.print(f"未実行: {item.safe_name}")
            console.print(
                f"集計: 完了={batch.completed} 失敗={batch.failed} "
                f"未実行={batch.pending} 再生成が必要={len(batch.legacy)}"
            )
            if batch.legacy:
                console.print("旧パッケージは元の入力音源から再生成してください。キャッシュが残っていれば分離処理は省略されます。")
            if batch.failed:
                raise typer.Exit(1)
            return
        assert safe_name is not None
        result = sync_one(out, safe_name, include_original=original, lock=lock)
    except PocketServiceError as exc:
        if exc.kind == "legacy":
            _fail("Pocket 同期情報のない旧パッケージです。元の入力音源から再生成してください。キャッシュが残っていれば分離処理は省略されます。")
        _fail(str(exc))
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(safe_error(exc))
    finally:
        lock.release()
    console.print(f"Pocket 同期が完了しました: {config.base_url}")
    console.print(f"media: uploaded={result.media_uploaded} skipped={result.media_skipped}")
    console.print(f"manifest: updated={result.manifest_updated} skipped={result.manifest_skipped}")
    console.print(f"library: updated={result.library_updated} skipped={result.library_skipped}")
