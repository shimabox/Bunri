# StemLab

音源から特定の楽器パート(まずはギター)の stem を抽出し、練習用パッケージ
(楽器のみ / 楽器なし(カラオケ) / 原曲 + オフラインで再生できる HTML プレイヤー)
を生成するツールです。

tab-maker のスパイク実装から、stem 抽出まわりを切り出して独立させたプロジェクトです。

## セットアップ

```bash
uv sync
```

初回実行時、分離モデル(既定はギター特化 Mel-Band Roformer、becruily 製、
約45MB)が `~/.cache/stemlab/models` に自動ダウンロードされます
(`STEMLAB_MODEL_DIR` 環境変数で保存先を変更可能)。2回目以降はこのキャッシュを
再利用するため、ダウンロードは発生しません。ffmpeg が別途必要です
(`brew install ffmpeg` など)。

## 使い方

```bash
stemlab song.mp3
```

実行すると `song.mp3` からギター stem を抽出し、`out/song/` に練習用パッケージ
一式を生成します:

```
out/song/
├── song.guitar.wav / .mp3     # ギターのみ
├── song.backing.wav / .mp3    # ギターなし(それ以外全部)
├── song.original.mp3          # 原曲
└── song.player.html           # オフライン練習プレイヤー
                                #  (原曲/ギターのみ/ギターなし切替・ABループ・
                                #   ピッチ維持スロー再生)
```

中間生成物(正規化済み音声・分離済み stem)は `out/.cache/<入力ファイルの
ダイジェスト>/` にキャッシュされ、同じ入力・同じオプションでの再実行はスキップ
されます。

### 主なオプション

```bash
stemlab song.mp3 --target guitar             # 抽出対象(既定: guitar。現状 guitar のみ登録)
stemlab song.mp3 --model htdemucs_6s.yaml     # 分離モデルを明示指定(失敗時のフォールバックなし)
stemlab song.mp3 --device cpu                 # auto(既定) | cpu | mps
stemlab song.mp3 --no-mp3                     # wav のみ出力(mp3 変換をスキップ)
stemlab song.mp3 --no-cache                   # キャッシュを無視して全段再計算
stemlab song.mp3 -o path/to/out               # 出力先ディレクトリ
```

`--model` を省略した場合、対象楽器ごとに登録されたデフォルトモデルが失敗した
ときだけ自動でフォールバックモデルに切り替わります(ギターの場合
`htdemucs_6s.yaml`)。`--model` で明示的に指定した場合はフォールバックせず、
失敗はそのままエラーとして報告されます。

## Docker で使う

ffmpeg・Python 3.13・分離モデルをまとめて動かしたいだけなら、`uv sync` の
かわりに Docker イメージを使えます。ローカルに Python 環境を用意する必要は
ありません。

### ワンライナー

```bash
docker build --target cpu -t stemlab:cpu .

docker run --rm \
  -v ./songs:/in \
  -v ./out:/out \
  -v stemlab-models:/root/.cache/stemlab/models \
  stemlab:cpu /in/song.mp3 -o /out
```

- `stemlab-models` という名前付きボリュームに分離モデルを永続化します。初回
  起動時に becruily モデル(約45MB)がここへダウンロードされ、以降の実行
  (同じボリュームを指定する限り)では再ダウンロードされません。
- `ENTRYPOINT` は `stemlab` なので、`docker run` の末尾に渡す引数はそのまま
  CLI オプションとして扱われます(`--target` / `--model` / `--device` など、
  「主なオプション」節と同じものが使えます)。

### compose で使う

```bash
docker compose run --rm stemlab /in/song.mp3 -o /out
```

`compose.yaml` は `./songs` を読み取り専用で `/in` に、`./out` を `/out` に、
`stemlab-models` ボリュームを `/root/.cache/stemlab/models` にマウントします。
`./songs` に音源ファイルを置いてから実行してください。

### 注意: Mac では GPU が使えない

macOS の Docker Desktop はコンテナに GPU を渡せないため、`cpu` イメージの
分離処理は(ホストの Apple Silicon GPU=MPS を使うネイティブ実行に比べて)
大幅に遅くなります(実測: 3分45秒の曲で MPS 実測295秒 → CPU コンテナでは
15〜40分程度かかりうる)。日常的に何曲も分離するなら、Mac 上では
`uv sync` によるネイティブ実行(`--device mps` または既定の `auto`)の方が
高速な経路です。Docker はホスト環境を汚したくない場合や Linux/CI 上での
実行に向いています。

### GPU (cuda) イメージについて

NVIDIA CUDA ランタイムベースの GPU 向けイメージを定義しています。
`onnxruntime-gpu` が x86_64 の wheel しか公開していないため
**linux/amd64 専用**です(Apple Silicon などの arm64 ホストでは
`--platform linux/amd64` の指定が必須):

```bash
docker build --platform linux/amd64 --target cuda -t stemlab:cuda .
```

このビルドが通ること(torch 2.13.0+cu130 / onnxruntime-gpu 1.27 が解決・
インストールされること)は確認済みですが、**開発機(macOS / Apple
Silicon)では GPU コンテナを実行できないため、GPU での分離処理そのものは
未検証です**。Linux + NVIDIA GPU(CUDA 13 対応ドライバ)環境で使う場合は、
まず動作確認してから使ってください。詳しい選定理由は NOTES.md を参照して
ください。

### dev イメージ(テスト実行用)

```bash
docker build --target dev -t stemlab:dev .
docker run --rm stemlab:dev            # pytest -q を実行
```

pytest・playwright(Chromium 込み)を含む開発用イメージです。CI などで
コンテナ内にテストを閉じ込めたい場合に使えます。

## ステータス

Phase 1(コア移植: guitar)・Phase 1.5(Docker 第一級対応: cpu/dev/cuda
イメージ、compose.yaml)完了。`stemlab` CLI で実際にギター抽出パッケージを
生成できます。今後の展開(vocals など他楽器への対応、プレイヤー機能拡張)は
`.claude/plans/stemlab-founding-plan.md` を参照してください。
