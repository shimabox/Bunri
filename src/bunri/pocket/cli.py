"""Commands for connecting and managing a Bunri Pocket shelf."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from bunri.pocket.config import PocketConfig, connection_fingerprint, read_config, save_config, validate_base_url, validate_capabilities, validate_token
from bunri.pocket.http import PocketHTTPClient
from bunri.pocket.lock import SyncLock, SyncLockBusy
from bunri.pocket.service import (
    DeleteTargetIdentity,
    PocketServiceError,
    delete_track,
    list_library_tracks,
    resolve_delete_target,
    safe_delete_error,
    safe_error,
    sync_all,
    sync_one,
)

app = typer.Typer(
    add_completion=False,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
)
console = Console()


def _safe_display(value: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "\ufffd", value)


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
            batch = sync_all(
                out,
                include_original=original,
                lock=lock,
                expected_connection_fingerprint=connection_fingerprint(config),
            )
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
        result = sync_one(
            out,
            safe_name,
            resolution="safe_name",
            include_original=original,
            lock=lock,
            expected_connection_fingerprint=connection_fingerprint(config),
        )
    except typer.Exit:
        raise
    except PocketServiceError as exc:
        if all_packages and exc.legacy:
            for name in exc.legacy:
                console.print(f"[yellow]再生成が必要:[/yellow] {name}")
            console.print("旧パッケージは元の入力音源から再生成してください。キャッシュが残っていれば分離処理は省略されます。")
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


def _stdin_is_tty() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)())


@app.command("delete")
def delete_command(
    safe_name: Optional[str] = typer.Argument(None, metavar="SAFE_NAME"),
    song_id: Optional[str] = typer.Option(None, "--song-id", help="Delete this Pocket song ID"),
    select: bool = typer.Option(False, "--select", help="Select from the Pocket library"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the final confirmation"),
    output: Path = typer.Option(Path("out"), "--output", "-o", help="Output directory"),
) -> None:
    """Delete one track from the Bunri Pocket shelf."""
    if sum((safe_name is not None, song_id is not None, select)) != 1:
        _fail("SAFE_NAME、--song-id、--select のいずれか1つだけを指定してください。")
    if yes and select:
        _fail("--yes と --select は同時に指定できません。自動化では SAFE_NAME または --song-id を指定してください。")
    try:
        config = read_config(output)
    except (OSError, ValueError):
        _fail("Pocket の接続設定を確認できません。")
    if config is None:
        _fail("Pocket の接続設定がありません。")
    expected_connection_fingerprint = connection_fingerprint(config)

    target: DeleteTargetIdentity
    try:
        if safe_name is not None:
            target = resolve_delete_target(output, safe_name)
        elif song_id is not None:
            if re.fullmatch(r"[0-9a-f]{12}", song_id) is None:
                _fail("song ID は小文字16進12桁で指定してください。")
            target = DeleteTargetIdentity(song_id=song_id)
        else:
            if not _stdin_is_tty():
                _fail("対話選択には TTY が必要です。--song-id または SAFE_NAME を指定してください。")
            # Use the same immutable connection identity captured before the
            # selection UI, then verify it again immediately before deletion.
            tracks = list_library_tracks(
                output,
                client=PocketHTTPClient(config.base_url, config.token),
            )
            if not tracks:
                console.print("棚に削除できる曲はありません。")
                return
            console.print("Bunri Pocket の棚から削除する曲を選択してください。")
            for index, track in enumerate(tracks, 1):
                console.print(
                    f"  {index}. {_safe_display(track.title)} — {track.song_id}",
                    markup=False,
                    highlight=False,
                )
            choice = typer.prompt("番号", type=int)
            if choice < 1 or choice > len(tracks):
                _fail("選択した番号が範囲外です。")
            selected = tracks[choice - 1]
            target = DeleteTargetIdentity(song_id=selected.song_id, title=selected.title)
    except typer.Exit:
        raise
    except (PocketServiceError, OSError, RuntimeError, ValueError) as exc:
        message = safe_delete_error(exc)
        if safe_name is not None:
            message += " --song-id または --select で対象を指定できます。"
        _fail(message)

    console.print("Bunri Pocket の棚から次の曲を削除します。")
    if target.title:
        console.print(
            f"  曲名: {_safe_display(target.title)}",
            markup=False,
            highlight=False,
        )
    if target.safe_name:
        console.print(
            f"  safe name: {_safe_display(target.safe_name)}",
            markup=False,
            highlight=False,
        )
    console.print(f"  song ID: {target.song_id}")
    if not yes:
        if not _stdin_is_tty():
            _fail("確認入力には TTY が必要です。自動化では --yes を指定してください。")
        if not typer.confirm("この操作を続けますか？", default=False):
            console.print("削除を取り消しました。")
            return

    try:
        mutation_lock = SyncLock(output).acquire()
    except (OSError, SyncLockBusy) as exc:
        _fail(safe_delete_error(exc))
    try:
        result = delete_track(
            output,
            target,
            lock=mutation_lock,
            expected_connection_fingerprint=expected_connection_fingerprint,
        )
    except (PocketServiceError, OSError, RuntimeError, ValueError) as exc:
        _fail(safe_delete_error(exc))
    finally:
        mutation_lock.release()
    console.print(f"棚から削除しました: {result.song_id}")
