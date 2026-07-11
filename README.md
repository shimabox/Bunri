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

## ステータス

Phase 1(コア移植: guitar)完了。`stemlab` CLI で実際にギター抽出パッケージを
生成できます。今後の展開(vocals など他楽器への対応、Docker 対応、プレイヤー
機能拡張)は `.claude/plans/stemlab-founding-plan.md` を参照してください。
