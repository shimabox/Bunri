# Bunri

音源から特定の楽器パート(まずはギター)の stem を抽出し、練習用パッケージ
(楽器のみ / 楽器なし(カラオケ) / 原曲 + オフラインで再生できる HTML プレイヤー)
を生成するツールです。処理はすべてお使いのマシン内で完結し、音源が外部に
送信されることはありません。

## クイックスタート

必要なもの: macOS(Apple Silicon 推奨)または Linux。あとはスクリプトが面倒を見ます。
Windows の方は「[Windows で使う](#windows-で使う)」を見てください(WSL2 経由で使えます)。

```bash
# 1. このフォルダを取得して移動(git clone または zip 展開)
cd Bunri

# 2. セットアップ(uv / ffmpeg の確認と依存の導入。初回は数分)
make setup

# 3. 起動 — ブラウザで画面が開くので、曲をドラッグ&ドロップするだけ
make web
```

停止はターミナルで Ctrl+C。作った練習パッケージは `out/曲名/` に残り、
中の `◯◯.player.html` はサーバーなしでもダブルクリックでそのまま使えます。

`make` が無い環境では `./setup.sh` → `uv run bunri-web` でも同じです。

### 注意事項(自己責任でご利用ください)

- 本ツールは現状のまま・無保証で提供されます。利用は自己責任でお願いします
- 音源はローカルで処理されますが、**権利を持っている音源または私的利用の
  範囲**でお使いください。分離結果の公開・配布は権利上の問題が生じ得ます
- 初回の分離時に、分離モデル(約45MB〜、対象楽器による)がこのフォルダ内の
  `models/` へ自動ダウンロードされます(`BUNRI_MODEL_DIR` 環境変数で変更可能)。
  標準設定では、Bunri自身が作るモデル・出力・仮想環境はすべてこのフォルダ内に
  あるため、フォルダを丸ごと削除すれば除去できます。ただし、`make setup` で新たに
  導入したuvやffmpegはシステム側に残るため、不要なら各ツールの方法で別途削除して
  ください。また、`BUNRI_MODEL_DIR`を変更した場合、その保存先も別途削除が必要です
- 分離には Apple Silicon の GPU(MPS)でも1曲あたり数分〜20分程度かかります

## セットアップ(手動でやる場合)

[uv](https://docs.astral.sh/uv/) と ffmpeg を導入済みなら:

```bash
uv sync --extra web   # CLI だけでよければ --extra web は不要
```

## 使い方

```bash
bunri song.mp3
```

実行すると `song.mp3` からギター stem を抽出し、`out/song/` に練習用パッケージ
一式を生成します:

```
out/song/
├── song.guitar.wav / .mp3            # ギターのみ
├── song.guitar.backing.wav / .mp3    # ギターなし(それ以外全部)
├── song.original.mp3                 # 原曲
└── song.guitar.player.html           # オフライン練習プレイヤー
                                      #  (原曲/ギターのみ/ギターなし切替・
                                      #   ABループ・ピッチ維持スロー再生)
```

ファイル名に抽出対象(`guitar` など)が入っているのは、同じ曲を別の
`--target` で追加ビルドしたとき(例: `--target vocals` でカラオケ音源を作る)
に、同じフォルダへ共存できるようにするためです。`song.original.mp3` だけは
どの対象でも同一内容なので共有されます。

中間生成物(正規化済み音声・分離済み stem)は `out/.cache/<入力ファイルの
ダイジェスト>/` にキャッシュされ、同じ入力・同じオプションでの再実行はスキップ
されます。

### 主なオプション

```bash
bunri song.mp3 --target vocals             # 抽出対象(既定: guitar)
bunri song.mp3 --model htdemucs_6s.yaml     # 分離モデルを明示指定(失敗時のフォールバックなし)
bunri song.mp3 --device cpu                 # auto(既定) | cpu | mps | cuda
bunri song.mp3 --no-mp3                     # wav のみ出力(mp3 変換をスキップ)
bunri song.mp3 --no-cache                   # キャッシュを無視して全段再計算
bunri song.mp3 -o path/to/out               # 出力先ディレクトリ
```

`--device` に `mps`/`cuda` を明示した場合、そのデバイスが実際に使えるか
確認したうえで、使えなければ黙って CPU 等へフォールバックせずエラーに
なります(`auto` は従来どおり利用可能な最良のデバイスを自動選択します)。

### 抽出対象(--target)

| target | 既定モデル | 備考 |
|---|---|---|
| `guitar`(既定) | ギター特化 Mel-Band Roformer(becruily) | 実測比較で選定(ボーカル混入が htdemucs_6s の 1/3) |
| `vocals` | vocals_mel_band_roformer | カタログ実測 SDR 首位(12.60)。「ボーカルなし」はそのままカラオケ音源 |
| `bass` / `drums` / `piano` | htdemucs_6s(6-stem Demucs) | 専用モデルなし。6 stem 分離から該当パートを抽出 |

同じ曲でも `--target` ごとに分離キャッシュは独立しているため、対象を
切り替えても過去の分離結果はそのまま再利用されます。

`--model` を省略した場合、対象楽器ごとに登録されたデフォルトモデルが失敗した
ときだけ自動でフォールバックモデルに切り替わります(ギターの場合
`htdemucs_6s.yaml`)。`--model` で明示的に指定した場合はフォールバックせず、
失敗はそのままエラーとして報告されます。

## Web UI で使う

ブラウザから音源をドラッグ&ドロップ/アップロードして分離を実行し、完了したら
そのまま練習用プレイヤーを開ける、ローカル専用の簡易 Web UI です。CLI を
直接叩かない使い方をしたい場合に使ってください。

```bash
uv sync --extra web
uv run bunri-web
```

起動すると `http://127.0.0.1:8330/` がブラウザで自動的に開きます
(**127.0.0.1 のみで待ち受け**。LAN 公開や認証には対応していません)。

- 音源をドロップ(またはクリックして選択)すると曲名確認欄が出るので、
  必要なら曲名を編集してアップロードします。分離はサーバー側で
  `bunri` CLI をサブプロセスとして実行する形で行われるため、ブラウザを
  閉じてもジョブは継続します
- ジョブ一覧は「待機中 / 処理中(経過時間)/ 完了 / 失敗」を表示し、
  完了したジョブには「プレイヤーを開く」リンクが表示されます
  (次回アクセス時もこの一覧・リンクは残ります)
- 同じ音源(内容が同一)を再アップロードしても再分離はされず、
  既存の結果が再利用されます

### 主なオプション

```bash
bunri-web --port 8330       # 待ち受けポート(既定: 8330)
bunri-web -o path/to/out    # 出力先ディレクトリ(既定: out。bunri CLI と共有可能)
bunri-web --no-open         # 起動時のブラウザ自動オープンを無効化
```

## Docker で使う

ffmpeg・Python 3.13・分離モデルをまとめて動かしたいだけなら、`uv sync` の
かわりに Docker イメージを使えます。ローカルに Python 環境を用意する必要は
ありません。

### ワンライナー

```bash
docker build --target cpu -t bunri:cpu .

docker run --rm \
  -v ./songs:/in \
  -v ./out:/out \
  -v bunri-models:/root/.cache/bunri/models \
  bunri:cpu /in/song.mp3 -o /out
```

- `bunri-models` という名前付きボリュームに分離モデルを永続化します。初回
  起動時に becruily モデル(約45MB)がここへダウンロードされ、以降の実行
  (同じボリュームを指定する限り)では再ダウンロードされません。
- `ENTRYPOINT` は `bunri` なので、`docker run` の末尾に渡す引数はそのまま
  CLI オプションとして扱われます(`--target` / `--model` / `--device` など、
  「主なオプション」節と同じものが使えます)。

### compose で使う

```bash
docker compose run --rm bunri /in/song.mp3 -o /out
```

`compose.yaml` は `./songs` を読み取り専用で `/in` に、`./out` を `/out` に、
`bunri-models` ボリュームを `/root/.cache/bunri/models` にマウントします。
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
docker build --platform linux/amd64 --target cuda -t bunri:cuda .
```

このビルドが通ること(torch 2.13.0+cu130 / onnxruntime-gpu 1.27 が解決・
インストールされること)は確認済みですが、**開発機(macOS / Apple
Silicon)では GPU コンテナを実行できないため、GPU での分離処理そのものは
未検証です**。Linux + NVIDIA GPU(CUDA 13 対応ドライバ)環境で使う場合は、
まず動作確認してから使ってください。詳しい選定理由は NOTES.md を参照して
ください。

### dev イメージ(テスト実行用)

```bash
docker build --target dev -t bunri:dev .
docker run --rm bunri:dev            # pytest -q を実行
```

pytest・playwright(Chromium 込み)を含む開発用イメージです。CI などで
コンテナ内にテストを閉じ込めたい場合に使えます。

## Windows で使う

ネイティブの Windows には現状対応していませんが、**WSL2(Windows 標準の
Linux 実行環境)経由でそのまま使えます**。おすすめはこちらです。

### WSL2 で使う(推奨)

```powershell
# PowerShell(管理者)で初回のみ。終わったら再起動
wsl --install -d Ubuntu
```

再起動後、スタートメニューから Ubuntu を開いて、その中で:

```bash
sudo apt update && sudo apt install -y ffmpeg make git
git clone <このリポジトリ> && cd Bunri   # zip 展開でも可
make setup
make web
```

WSL2 の localhost は Windows 側に自動で転送されるので、**Windows のブラウザで
http://127.0.0.1:8330/ がそのまま開きます**。曲もエクスプローラーから
ブラウザへドラッグ&ドロップするだけです(WSL の中のパスを意識する必要は
ありません)。

- 処理速度の注意: GPU なし(または NVIDIA 以外)の場合は CPU 処理になり、
  **1曲あたり 30〜40 分程度**かかります。NVIDIA GPU 搭載機では WSL2 の
  CUDA 対応で高速化できる余地がありますが、現状は未対応です(検討中)
- Docker Desktop をお使いなら「Docker で使う」節の手順もそのまま動きます
  (こちらも CPU 実行)

ネイティブ Windows 対応(setup.ps1・GPU 対応など)の検討状況は
`.claude/plans/windows-support-plan.md` にまとめてあります。

## ステータス

Phase 1(コア移植: guitar)・Phase 1.5(Docker 対応)・Phase 2(多楽器:
vocals/bass/drums/piano)完了。プレイヤー拡張(音量ミキサー・ループ書き出し)は
試作したがユーザー試用の結果見送り(経緯は NOTES.md)。今後の展開(波形表示、
compare コマンド、バッチ処理など)は `.claude/plans/` のプラン各種を参照して
ください。
