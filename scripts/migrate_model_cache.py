#!/usr/bin/env python3
"""tab-maker のモデルキャッシュを StemLab のキャッシュディレクトリへ移行するスクリプト。

    ~/.cache/tab-maker/models -> ~/.cache/stemlab/models

- コピーは shutil.copy2 を使い、メタデータ(タイムスタンプ等)を保持する。
- コピー先に同名かつ同サイズのファイルが既に存在する場合はスキップする(冪等)。
- コピー元のディレクトリが存在しない場合はメッセージを出して正常終了する。

使い方:
    uv run python scripts/migrate_model_cache.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

SRC = Path.home() / ".cache" / "tab-maker" / "models"
DST = Path.home() / ".cache" / "stemlab" / "models"


def migrate(src: Path = SRC, dst: Path = DST) -> None:
    if not src.exists():
        print(f"コピー元が見つかりません: {src}")
        print("移行するモデルキャッシュがないため、何もせず終了します。")
        return

    dst.mkdir(parents=True, exist_ok=True)

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

    print(f"完了: {copied} 件コピー, {skipped} 件スキップ(既存)")
    print(f"コピー先: {dst}")


if __name__ == "__main__":
    migrate()
    sys.exit(0)
