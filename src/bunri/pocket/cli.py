"""Commands for connecting and synchronizing a Bunri Pocket shelf."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import typer
from rich.console import Console

from bunri.pocket.config import PocketConfig, read_config, save_config, validate_base_url, validate_capabilities, validate_token
from bunri.pocket.http import PocketHTTPClient
from bunri.pocket.local import LocalPreflightError, preflight
from bunri.pocket.sync import SyncError, synchronize

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
    safe_name: str = typer.Argument(..., metavar="SAFE_NAME"),
    output: Path = typer.Option(Path("out"), "--output", "-o", help="Output directory"),
    original: bool = typer.Option(True, "--original/--no-original", help="Upload original MP3"),
) -> None:
    out = output
    try: config = read_config(out)
    except (OSError, ValueError) as exc: _fail(str(exc))
    if config is None:
        _fail("Pocket の接続設定がありません。アップロードは開始していません。\n先に接続してください:\n  bunri pocket connect <Pocket URL> -o " + shlex.quote(str(output)))
    try: package = preflight(out, safe_name, include_original=original)
    except LocalPreflightError as exc:
        if exc.kind == "legacy":
            command = shlex.join(["bunri", "/path/to/original.mp3", "-o", str(output), "--title", safe_name]) + " --target <target>"
            _fail("Pocket 同期情報のない旧パッケージが見つかりました。アップロードは開始していません。\n- " + "\n- ".join(exc.issues) + f"\n\n元の入力音源からパッケージを再生成してください:\n  {command}\n\n{output}/.cache/ に同じ入力と target の分離キャッシュが残っていれば、分離処理は再実行されません。\nサイドカーを手作業で作成したり、Web の Job JSON から値を移さないでください。")
        if exc.kind == "no_mp3" and exc.metadata is not None:
            target = next((x.target for x in exc.metadata.targets if "mp3" not in x.formats), "<target>")
            command = shlex.join(["bunri", "/path/to/original.mp3", "-o", str(output), "--title", exc.metadata.title, "--target", target, "--mp3"])
            _fail("MP3 のない target があるため Pocket と同期できません。アップロードは開始していません。\n- " + "\n- ".join(exc.issues) + f"\n\n元の入力音源から MP3 を有効にして対象 target を再生成してください:\n  {command}")
        _fail("パッケージを安全に同期できません。アップロードは開始していません。\n- " + "\n- ".join(exc.issues))
    client = PocketHTTPClient(config.base_url, config.token)
    try: result = synchronize(package, client, include_original=original)
    except SyncError as exc:
        if str(exc).startswith("DIGEST_COLLISION:"):
            remote_digest = str(exc).split(":", 1)[1]
            _fail(f"同じ12桁の song ID に別の入力音源が登録されています。アップロードは開始していません。\nsong_id: {package.metadata.source.cache_key}\nlocal digest: {package.metadata.source.digest}\nremote digest: {remote_digest}")
        if str(exc).startswith("RACE_DIGEST_COLLISION:"):
            _fail("同期中に同じ12桁の song ID へ別の入力音源が登録されました。\nmanifest と library は更新していません。preflight 後に media を上書きした可能性があります。棚の状態を確認してから再実行してください。")
        _fail(str(exc))
    except (OSError, RuntimeError, ValueError) as exc: _fail(str(exc))
    console.print(f"Pocket 同期が完了しました: {config.base_url}")
    console.print(f"media: uploaded={result.media_uploaded} skipped={result.media_skipped}")
    console.print(f"manifest: updated={result.manifest_updated} skipped={result.manifest_skipped}")
    console.print(f"library: updated={result.library_updated} skipped={result.library_skipped}")
