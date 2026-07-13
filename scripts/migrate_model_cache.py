#!/usr/bin/env python3
"""既存のモデルキャッシュを StemLab の現行モデルディレクトリへ移行するスクリプト。

コピー元(存在するものだけ処理):
    ~/.cache/tab-maker/models   (tab-maker 時代のダウンロード済みモデル)
    ~/.cache/stemlab/models     (StemLab 旧既定。現在はプロジェクト内 models/ が既定)

コピー先は stemlab.separate と同じ解決規則(STEMLAB_MODEL_DIR > プロジェクト内
models/ > ~/.cache/stemlab/models)に従うため、コードと食い違わない。

- コピーは shutil.copy2 を使い、メタデータ(タイムスタンプ等)を保持する。
- コピー先に同名かつ同サイズのファイルが既に存在する場合はスキップする(冪等)。
- コピー元のディレクトリが存在しない場合はスキップして正常終了する。

使い方:
    uv run python scripts/migrate_model_cache.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from stemlab.separate import _MODEL_DIR

SOURCES = [
    Path.home() / ".cache" / "tab-maker" / "models",
    Path.home() / ".cache" / "stemlab" / "models",
]


def migrate_one(src: Path, dst: Path) -> tuple[int, int]:
    copied = 0
    skipped = 0
    for src_file in sorted(src.rglob("*")):
        if src_file.is_dir():
            continue
        rel = src_file.relative_to(src)
        dst_file = dst / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        if dst_file.exists() and dst_file.stat().st_size == src_file.stat().st_size:
            skipped += 1
            continue
        shutil.copy2(src_file, dst_file)
        copied += 1
        print(f"コピー: {rel}")
    return copied, skipped


def migrate() -> None:
    dst = Path(_MODEL_DIR)
    if not any(src.exists() for src in SOURCES):
        print("移行するモデルキャッシュが見つかりません。何もせず終了します。")
        return
    dst.mkdir(parents=True, exist_ok=True)
    total_copied = total_skipped = 0
    for src in SOURCES:
        if not src.exists() or src.resolve() == dst.resolve():
            continue
        print(f"コピー元: {src}")
        copied, skipped = migrate_one(src, dst)
        total_copied += copied
        total_skipped += skipped
    print(f"完了: {total_copied} 件コピー, {total_skipped} 件スキップ(既存)")
    print(f"コピー先: {dst}")


if __name__ == "__main__":
    migrate()
    sys.exit(0)
