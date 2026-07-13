# Windows 対応プラン(未着手・計画のみ)

2026-07-13 起草。「Windows で使うには?」への回答と、対応する場合の実装計画。

## 結論(先に要点)

- **今すぐ使う道は 2 つある(実装ゼロ)**: WSL2(推奨)か Docker Desktop。
  どちらも現行コードのまま動く見込みが高い(linux として解決される)
- **ネイティブ Windows 対応は「できるが、2 つの壁」がある**:
  ① diffq(音源分離の推移依存)に Python 3.13 用 Windows ホイールが無く、
  MSVC Build Tools(数 GB)のインストールが必要になる
  ② 手元に検証環境が無い — 受入ゲートを GitHub Actions の windows ランナー
  (要 GitHub push)かユーザーの Windows 実機に置く必要がある
- GPU 高速化は **NVIDIA(CUDA)搭載機のみ**対象(分離は torch 経路のため
  DirectML は効かない)。非 NVIDIA 機は CPU 分離 = 1曲 30〜40 分になる点に注意

## 現状: なぜ今 Windows で動かないか(調査済みの具体点)

| # | 障害 | 場所 | 深刻度 |
|---|---|---|---|
| 1 | 依存解決から Windows を除外している | pyproject `[tool.uv] environments`(darwin arm64 / linux のみ)| **必須修正**(uv sync が即失敗) |
| 2 | diffq==0.2.4 の Windows ホイールが cp310 まで(cp313 なし) | audio-separator の推移依存 | **最大の壁**(sdist ビルドに MSVC 必須) |
| 3 | 孤児プロセス刈り取りが `ps` コマンド前提 | web/jobs.py `_process_command` | 要修正(Windows に ps なし → 刈り取りが無言で無効化) |
| 4 | setup.sh(bash)/ Makefile / brew 前提の文言 | setup.sh, Makefile, audio.py のエラーメッセージ | 要追加(setup.ps1 等) |
| 5 | torch の Windows GPU は PyPI 既定では CPU 版 | pyproject(cu ルートは linux 用しかない) | GPU 対応時のみ |
| 6 | `--device mps` の案内が macOS 前提 | cli.py help 等 | 軽微(auto は CUDA→CPU に正しく落ちる) |

問題にならないことが確認済みの点: パス処理(pathlib + `_safe_filename` が
Windows 予約文字対応済み)/ `os.replace` のアトミック置換 / `os.kill(pid, SIGTERM)`
(Windows では TerminateProcess 相当で動く。強制終了だがジョブは再キュー設計)/
soundfile(libsndfile 同梱ホイールあり)/ Playwright / file:// プレイヤー。

## レベル 0: 実装ゼロで今すぐ使う(ドキュメント整備のみ)

### 0-A. WSL2(推奨)

WSL2 の Ubuntu の中では現行コードが **linux としてそのまま動く**(uv.lock も
解決済み)。NVIDIA GPU があれば WSL2 の CUDA パススルーで torch が GPU を使える
(ただし現行 lock は linux=CPU ホイール固定なので、GPU を使うには
`UV_TORCH_BACKEND=cu130 uv sync` 相当の上書きが必要 — レベル 2 参照)。

手順の概略(README に載せる内容):
```powershell
wsl --install -d Ubuntu        # 初回のみ・要再起動
# 以降は Ubuntu ターミナル内で:
sudo apt install ffmpeg make
git clone <StemLab> && cd StemLab
make setup && make web         # → Windows 側のブラウザで http://127.0.0.1:8330/
```
- WSL2 の localhost は Windows 側に自動フォワードされるので Web UI がそのまま開ける
- 曲の受け渡しは Web UI 経由なら意識不要(エクスプローラからブラウザにドロップ)

### 0-B. Docker Desktop

既存の `stemlab:cpu`(linux/amd64 ビルド済み実績あり)がそのまま動く。
NVIDIA 機なら `--gpus all` + cuda イメージ(ビルド成功済・GPU 実行は未検証)。
CPU 実行は遅い(実測 34 分/3.7 分曲)ので、常用には向かない旨を明記する。

**レベル 0 の作業**: README に「Windows で使う」節を追加するだけ(半日未満)。

## レベル 1: ネイティブ Windows(CPU)対応

「エクスプローラで zip を展開 → setup.ps1 → ブラウザ」を成立させる。

### 実装タスク

1. **pyproject**: `environments` に `sys_platform == 'win32'` を追加し
   `uv lock` 再解決。torch は PyPI の Windows CPU ホイールが既定で解決される
   (macOS/linux の既存解決に影響しないことを lock diff で確認)
2. **diffq 問題の解決** — 選択肢(推奨順):
   - (a) **自前ホイールの同梱**: GitHub Actions の windows ランナーで
     diffq の cp313 win_amd64 ホイールを 1 回ビルドし、リポジトリの
     `vendor/` に置いて `[tool.uv.sources]` の path 指定で解決。
     ユーザー側の MSVC 不要になる。ライセンス(MIT)的に再配布可
   - (b) setup.ps1 で VS Build Tools の導入を案内(winget で入るが数 GB。
     カジュアル配布には重い)
   - (c) upstream(diffq / audio-separator)へ cp313 ホイール公開の issue/PR
     (時間がかかるが根本解決。(a) と並行可)
3. **web/jobs.py**: `_process_command` を Windows 対応に
   - `ps` の代わりに `tasklist /FI "PID eq <pid>" /FO CSV` をパース、または
     psutil を依存に追加して一本化(全 OS ホイールあり・±1 依存。こちらが素直)
4. **setup.ps1**(setup.sh の PowerShell 版):
   - uv: `winget install astral-sh.uv`(同意プロンプト付き)
   - ffmpeg: `winget install Gyan.FFmpeg`
   - `uv sync --extra web` → 完了案内。Makefile 非依存の起動手順
     (`uv run stemlab-web`)を出力
5. **文言**: audio.py の ffmpeg エラーメッセージを OS 別に(brew/apt/winget)。
   cli.py の `--device` ヘルプに cuda を追記(下記レベル 2 と同時でも可)
6. **README**: Windows ネイティブ節(setup.ps1 手順・注意)

### 検証戦略(最重要 — 実機が無い)

- **GitHub Actions `windows-latest` ランナーで CI を組む**のが唯一の
  再現可能なゲート: uv インストール → ffmpeg(choco)→ `uv sync --extra web`
  → `pytest -q`(Playwright は headless で動く。分離の実 E2E は CPU で
  時間がかかるので、FakeSeparator テスト+15 秒クリップの実分離に限定)
- 前提: **GitHub リポジトリ化(push)が先に必要**。可視性の確認を忘れない
- 最終ゲートはユーザー(または知人)の Windows 実機での試用が望ましい

**見積もり**: 実装 1〜2 日相当 + CI 整備。(a) 案のホイールビルドが読めない場合 +α。

## レベル 2: ネイティブ Windows(NVIDIA GPU)対応

- pyproject に win32 + NVIDIA 向けの torch cu ルートを足すか、
  Dockerfile cuda ステージと同じく `UV_TORCH_BACKEND=cu130` を setup.ps1 の
  オプション(「NVIDIA GPU を使いますか?」)にする方が lock を汚さず簡単
- onnxruntime は `audio-separator[gpu]`(onnxruntime-gpu、win_amd64 あり)
- **検証は NVIDIA 実機がないと不可能**(CI の windows ランナーに GPU なし)。
  ユーザー駆動ゲート必須
- 効果: CPU 30〜40 分/曲 → CUDA なら数分/曲の見込み(未実測)

## 非対象と明記すること

- **DirectML(AMD/Intel GPU)**: audio-separator に [dml] extra はあるが、
  効くのは onnxruntime 経路(MDX/VR 系)のみ。本プロダクトの主モデル
  (becruily / vocals Mel-Roformer / htdemucs)は **torch 経路**なので恩恵なし。
  torch-directml は実験的・停滞気味で、賭けない
- Windows on ARM: torch ホイール事情が不安定。対象外と明記

## 推奨ロードマップ

| 段階 | 内容 | 前提 | 工数感 |
|---|---|---|---|
| W0 | README に WSL2 / Docker 手順を追記 | なし | 数時間 |
| W1 | ネイティブ CPU 対応(上記 1〜6)+ Windows CI | GitHub push | 1〜2 日 |
| W2 | NVIDIA GPU オプション | W1 + NVIDIA 実機の協力者 | 半日+実機検証 |

まず W0 だけやって様子を見る(WSL2 で足りる人が多ければ W1 は保留)が
費用対効果として妥当。W1 に進む判断材料 = 「WSL2 を入れたくない/入れられない
Windows ユーザーが実在するか」。
