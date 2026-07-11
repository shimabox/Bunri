# StemLab 創設プラン

2026-07-11 起草。tab-maker で実証済みのギター音源分離機能を独立リポジトリへ移植し、
「楽器 stem 抽出と練習パッケージ生成」の専用ツールとして育てる。

## Context

- tab-maker(https://github.com/shimabox/tab-maker)の中で**最も価値が確認できたのは
  ギター抽出**(ユーザー実感+実測: ギター特化 Mel-Roformer でボーカル混入 1/3)
- タブ譜系の機能と切り離し、抽出・練習体験だけを尖らせる別プロダクトにする
- **スコープ決定(ユーザー確認済み)**: 楽器汎用アーキテクチャ+ギター旗艦
  (`--target guitar` がデフォルト。vocals/bass/drums/piano へ拡張可能な設計)
- **tab-maker との関係(確認済み)**: 当面は独立コピー。tab-maker は現状のまま動く。
  StemLab が安定したら tab-maker の separate ステージを StemLab 依存に置換する選択肢を残す

## プロダクト像

```bash
stemlab song.mp3                     # → out/song/ に練習パッケージ
stemlab song.mp3 --target vocals     # ボーカル抽出(Phase 2)
stemlab song.mp3 --model htdemucs_6s.yaml   # モデル切替
```

パッケージ内容(1曲=1フォルダ):
```
out/<曲名>/
├── <曲名>.guitar.wav / .mp3     # 対象楽器のみ
├── <曲名>.backing.wav / .mp3    # 対象楽器なし(それ以外全部)
├── <曲名>.original.mp3          # 原曲
└── <曲名>.player.html           # オフライン練習プレイヤー
                                 #  (原曲/ギターのみ/ギターなし・ABループ・ピッチ維持スロー)
```

## tab-maker から移植するもの(実証済みコード)

| 移植元(tab-maker) | StemLab での姿 | 変更点 |
|---|---|---|
| `stages/separate.py` | `src/stemlab/separate.py` | Stage プロトコル依存を外し関数化。**becruily ブートストラップ(HF自動DL+カタログ注入+audio-separator 0.44.3 の mlp_expansion_factor パッチ+SystemExit ガード+フォールバック)をそのまま移植** |
| (新規) | `src/stemlab/registry.py` | ターゲット→モデルのレジストリ: `{target: {default, fallback, stem_name}}`。Phase 1 は guitar のみ登録 |
| `stages/audio_io.py` + pipeline の `_export_mp3` | `src/stemlab/audio.py` | ffmpeg 正規化 + mp3 エンコード(list引数・shell不使用の流儀維持) |
| `cache.py` | `src/stemlab/cache.py` | ほぼそのまま(digest ディレクトリ+meta 照合) |
| `render/player_html.py` + `templates/player.html.j2` | `src/stemlab/player/` | トラックラベルをターゲット汎用化(「{楽器}のみ/{楽器}なし」をテンプレート引数に) |
| pipeline の stem_only 分岐 | `src/stemlab/package.py` | パッケージ生成のオーケストレータ(正規化→分離→wav/mp3→プレイヤー) |
| `tests/test_separate.py`(FakeSeparator 一式)+ `test_player_html.py` | `tests/` | **テスト資産ごと移植**(210テスト中の分離・プレイヤー系 ≈ 40件が頭金になる) |
| ISSUES.md の分離関連知見 | `NOTES.md` | audio-separator バグ・モデルカタログ調査結果(supported_models_full.json の要約) |

**移植しないもの**: transcribe / rhythm / quantize / fingering / chords / タブ譜レンダラ / 動画。

## 技術方針

- **Python 3.12+(できれば 3.13)を狙う**: tab-maker が 3.11 固定だった原因は basic-pitch。
  StemLab は audio-separator + torch + soundfile だけなので制約が消える。
  Phase 0 でスパイク(uv sync → import → FakeSeparator テスト)し、駄目なら 3.12 → 3.11 と降格
- uv 管理・src レイアウト・`prerelease = "if-necessary"`(onnx-weekly)・
  macOS arm64 に environments 限定 — tab-maker の pyproject の知見を踏襲
- モデルキャッシュは `~/.cache/stemlab/models`(tab-maker と別。既存 DL 済みモデルは
  初回にコピーすれば再DL不要 — Phase 0 でやる)
- **audio-separator のバージョンは 0.44.3 に pin**(パッチが前提のため。更新時は
  パッチ要否の再確認をリリース手順に含める)
- **Docker 第一級対応(ユーザー要望)**:
  - pyproject の解決環境を macOS arm64 限定から **linux も含む**よう広げる
    (basic-pitch が居ないので広げられる — tab-maker で限定した理由が消えている)
  - イメージ2系統: **cpu**(既定。torch CPU ホイール明示でイメージ肥大を回避、
    linux/amd64 + linux/arm64 のマルチアーチ)と **cuda**(NVIDIA 機用、`--gpus all`)
  - ffmpeg は apt で同梱。モデルキャッシュは名前付きボリューム
    (`-v stemlab-models:/root/.cache/stemlab/models`)で永続化し再DLを防ぐ
  - `compose.yaml` + README に `docker run` ワンライナーを用意
    (`-v ./songs:/in -v ./out:/out` で曲の出し入れ)
  - **注意(既知の制約)**: macOS の Docker は GPU 不可 → Mac ローカルはネイティブ uv が
    高速経路。Docker は配布・Linux/GPU 実行・将来の Web 化の土台
  - 実行時にブラウザ不要(プレイヤーは Jinja 生成のみ)。コンテナ内テスト用の
    dev イメージだけ Playwright chromium を追加インストール

## フェーズ計画(実装=下位モデル、計画・契約・受入=Fable)

| Phase | 内容 | 実装 | ゲート |
|---|---|---|---|
| **0: スキャフォールド+スパイク** | uv init(3.13→3.12→3.11 降格式スパイク)、git init、README/NOTES 雛形、モデルキャッシュ移行スクリプト | sonnet | uv sync+import+スモーク合格。私が pyproject レビュー |
| **1: コア移植(guitar)** | separate/registry/audio/cache/package/player/CLI 移植+テスト移植 | sonnet(移植は機械的作業) | 全テスト通過 + **パリティ検証**: 斜陽を tab-maker と StemLab 両方で分離し guitar.wav の相関 ≈ 1.0 を確認(同一モデルなので一致するはず)+ opus 敵対的レビュー(移植時の取りこぼし・パス処理)+ 私の全 diff 受入 |
| **1.5: Docker 対応** | Dockerfile(multi-stage、cpu/cuda ターゲット)、compose.yaml、README の docker セクション、GHCR 公開は任意 | sonnet | **コンテナ内 E2E**: 斜陽を CPU コンテナで分離しパッケージ生成(遅くても完走すること)+ モデルボリューム永続化の確認 + 私のレビュー |
| **2: 多楽器ターゲット** | registry に vocals/bass/drums/piano 追加。vocals のデフォルトモデルは **8.1 調査の実データ(supported_models_full.json の SDR)から私が選定**(vocals_mel_band_roformer 系)。bass/drums/piano は htdemucs_6s の stem 流用から。プレイヤーラベル汎用化 | sonnet | 斜陽で `--target vocals` E2E → **ユーザー試聴**(ボーカル抽出は歌の練習・カラオケ生成に直結する目玉) |
| **3: プレイヤー 2.0** | 候補: **波形表示**(file:// では decodeAudioData 不可 → **パッケージ生成時に Python で波形PNGを事前描画して data URI 埋め込み**で解決)+波形クリックシーク、トラック音量ミキサー、ループ区間の書き出し | opus(ブラウザ実証必須) | 実ブラウザ検証 + ユーザー試用。着手前にスコープをユーザーと選定 |
| **4: バックログ(ユーザー駆動)** | `stemlab compare`(モデル比較を1コマンド化)/ バッチ処理(フォルダ一括)/ 進捗バー / GUI(メニューバー常駐 or ドラッグ&ドロップ)/ Web アプリ化(tab-maker 時代の検討資料が流用可、著作権注意点も同様)/ PyPI 公開 | 都度 | — |

横断ルール(tab-maker で機能した規律をそのまま):
- フェーズゲート通過ごとに commit(動く状態のみ)
- コアアルゴリズム変更は敵対的レビュー(opus)を挟む
- アルゴリズム変更時は STAGE_VERSION に相当するキャッシュ無効化を忘れない
- GitHub リポジトリ作成は push 時にユーザーへ可視性(private/public)を確認

## リスクと緩和

- **audio-separator 更新でパッチが壊れる/不要になる**: 0.44.3 pin + NOTES.md に
  「更新時チェックリスト」を明記。upstream に issue/PR を出す選択肢も(貢献チャンス)
- **Python 3.13 で未知の非互換**: 降格式スパイクで Phase 0 のうちに確定
- **vocals 等の品質が guitar ほど検証されていない**: Phase 2 ゲートで必ずユーザー試聴。
  ダメなモデルはカタログ内の次点に差し替えるだけ(レジストリ設計の効能)
- **二重管理の乖離**(tab-maker 側の separate と): 当面は「StemLab が本流、tab-maker は凍結」
  と位置づけ、分離系のバグ修正は StemLab 側でのみ行う

## 検証方法

- 各フェーズ: `uv run pytest -q` + 実曲 E2E(songs/ は tab-maker のものを参照。
  リポジトリには入れない)
- Phase 1 パリティ: 同一モデル・同一曲で tab-maker と出力一致(サンプル相関)
- プレイヤー: headless Chromium での file:// 実挙動テスト(移植したテストが担保)
- ユーザー試聴ゲート: Phase 2(vocals)、Phase 3(プレイヤー UX)

## 将来メモ(構想レベル)

- tab-maker を `stemlab` ライブラリ依存に切替(分離コードの一本化)
- ブラウザ内完結版(WASM Demucs 系)— 音声がローカルを出ない構成、静的ホスティングのみ
- 練習ログ(ループ回数・速度の記録)みたいな練習支援機能
