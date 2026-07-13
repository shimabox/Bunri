#!/usr/bin/env bash
# StemLab セットアップスクリプト(macOS / Linux)
#
# やること:
#   1. uv(Python パッケージマネージャ)があるか確認。無ければ公式インストーラの
#      実行を提案(同意したときだけ実行)
#   2. ffmpeg があるか確認。無ければインストール方法を案内
#   3. uv sync --extra web で依存一式(Python 3.13 含む)を導入
#
# 何度実行しても安全です(導入済みの項目はスキップされます)。

set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }

say "StemLab セットアップを開始します"

# --- 1. uv -----------------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
  ok "uv: $(uv --version)"
else
  warn "uv が見つかりません。uv は Python 本体と依存の導入を全部やってくれるツールです。"
  printf '    公式インストーラ (https://astral.sh/uv) を実行しますか? [y/N] '
  read -r answer
  if [ "${answer:-}" = "y" ] || [ "${answer:-}" = "Y" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # インストーラが入れる標準の場所を PATH に足す(このスクリプト実行中だけ)
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 || {
      warn "uv が PATH に見つかりません。ターミナルを開き直してから再実行してください。"
      exit 1
    }
    ok "uv: $(uv --version)"
  else
    warn "中断しました。uv を入れてから再実行してください: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
  fi
fi

# --- 2. ffmpeg ---------------------------------------------------------------
if command -v ffmpeg >/dev/null 2>&1; then
  ok "ffmpeg: $(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f1-3)"
else
  warn "ffmpeg が見つかりません(音声の変換に必須です)。"
  if [ "$(uname)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
    printf '    Homebrew でインストールしますか? (brew install ffmpeg) [y/N] '
    read -r answer
    if [ "${answer:-}" = "y" ] || [ "${answer:-}" = "Y" ]; then
      brew install ffmpeg
      ok "ffmpeg を導入しました"
    else
      warn "中断しました。ffmpeg を入れてから再実行してください。"
      exit 1
    fi
  else
    warn "お使いの環境に合わせて導入してください:"
    warn "  macOS:  brew install ffmpeg"
    warn "  Ubuntu: sudo apt install ffmpeg"
    exit 1
  fi
fi

# --- 3. 依存の導入 -----------------------------------------------------------
say "Python 3.13 と依存パッケージを導入します(初回は数分かかります)"
uv sync --extra web

say "セットアップ完了!"
cat <<'DONE'

  使い方:
    make web          # ブラウザ画面を起動(おすすめ)
    make separate FILE=曲.mp3   # コマンドラインで直接分離

  初回の分離時に、分離モデル(約45MB)が自動ダウンロードされます。
DONE
