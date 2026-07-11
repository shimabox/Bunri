# NOTES

Phase 0(スキャフォールド + 依存関係スパイク)で得た技術的知見をまとめる。

## audio-separator を 0.44.3 に pin する理由

`audio-separator==0.44.3` の `mel_band_roformer.py:314` には、モデル設定から
`mlp_expansion_factor` が読み落とされるバグがある。移行予定の Phase 1 コードは、
このバグに対するパッチ(モンキーパッチ、または該当箇所の直接修正)を内蔵する
前提で書かれている。

そのため `audio-separator` のバージョンを `>=0.44` のような範囲指定にはせず、
**`==0.44.3` に完全 pin する**。バージョンが変わると、

- バグが直っていた場合 → パッチが不要な処理を上書きしてしまい、誤動作する
- バグの内容/行番号が変わっていた場合 → パッチが当たらない、または誤った箇所に
  当たってしまう

いずれにせよ「パッチ前提」が崩れるため、`audio-separator` を更新する際は
必ず下記の「更新時チェックリスト」を実施すること。

## mdxc_separator の失敗時挙動に関する知見

`audio-separator` の `mdxc_separator` は、分離処理に失敗した際に例外を送出する
のではなく **`sys.exit(1)` を呼び出す**。これは `SystemExit` として伝播するため、
通常の `except Exception` では捕捉できない。

呼び出し側(Phase 1 の移植コード)で失敗をハンドリングする場合は、
`except (Exception, SystemExit):` のように **`SystemExit` を明示的に捕捉する**
必要がある。素朴に `try/except Exception` だけを書くと、失敗時にプロセスが
そのまま終了してしまう点に注意。

## audio-separator 更新時チェックリスト

`audio-separator` のバージョンを上げる際は、以下を必ず確認すること。

1. **パッチ要否の再確認**: 新バージョンで `mlp_expansion_factor` 落ちのバグが
   直っているかどうかを確認する(直っていればパッチは不要、あるいは有害)。
2. **`mel_band_roformer.py` の該当箇所確認**: バグが残っている場合、該当コードの
   行番号・実装が変わっていないか確認し、パッチのターゲット(行番号や関数シグ
   ネチャ)を追従させる。
3. **フォールバック動作確認**: パッチが当たらなかった場合にサイレントに壊れず、
   検知できるようになっているか(パッチ適用チェックの有無)を確認する。
4. 上記に加え、`pyproject.toml` の `numpy` / `numba` まわりの上限([下記](#スパイク結果)
   参照)が新しいバージョンの依存関係でも引き続き解決可能か `uv sync` で確認する。
5. `audio-separator[cpu]` の extra 経由で入る `onnxruntime` のバージョンが
   問題なく解決されるか確認する(下記参照)。

## スパイク結果

`uv sync` が通る Python バージョンを 3.13 → 3.12 → 3.11 の順で降格して確認した。

- **3.13**: 最終的に成功。`requires-python = ">=3.13"` として確定。
- **3.12**: 検証時点では 3.13 と同じ理由で一度失敗したが、後述の numpy 上限修正
  により成功することも確認済み(参考情報として記録。実際に採用したのは 3.13)。
- **3.11**: 未検証(3.13 が成功したため、それ以上の降格は行わなかった)。

### 発生した問題と対処

1. **`numba`/`resampy` 経由で `llvmlite==0.36.0` のビルドが失敗**
   - 症状: `uv sync` が `llvmlite==0.36.0` のビルドで
     `RuntimeError: Cannot install on Python version 3.13.1; only versions
     >=3.6,<3.10 are supported.` を出して失敗する。
   - 原因: `numpy` の依存指定にバージョン上限を付けていなかったため、
     resolver が最新の `numpy`(2.5.1)を選択。ところが現行の `numba`
     (最新 0.66.0 でも `numpy<2.5` までしか対応していない)がこれと矛盾し、
     resolver は `numpy` の上限を持たない非常に古い `numba==0.53.1`
     (`resampy` 経由、`llvmlite==0.36.0` に依存)まで手繰り寄せてしまい、
     結果としてこの `numba`/`llvmlite` の組が Python 3.12/3.13 未対応で
     ビルドに失敗していた。
   - 対処: `pyproject.toml` の `numpy` 依存に `numpy<2.5` の上限を追加。
     これにより resolver が最新の `numba==0.66.0`(`llvmlite==0.48.0`)を
     選択するようになり、解決した。

2. **`ModuleNotFoundError: No module named 'onnxruntime'`**
   - 症状: `audio_separator.separator.Separator` の import 時に
     `onnxruntime` が無いというエラー。
   - 原因: `audio-separator` の PyPI メタデータ上、`onnxruntime` は
     `extra == "cpu"`(または `dml`/`gpu`)経由でのみ依存として付く任意
     依存になっている。プレーンな `audio-separator==0.44.3` の指定だけでは
     `onnxruntime` はインストールされない。
   - 対処: 依存指定を `audio-separator[cpu]==0.44.3` に変更し、CPU 版の
     `onnxruntime` を明示的にインストールするようにした。

### 確定した依存関係の要点

- `requires-python = ">=3.13"`
- `numpy<2.5`(numba 0.66.0 の上限に合わせる)
- `audio-separator[cpu]==0.44.3`(onnxruntime を明示的に含める)
- `torch==2.13.0`, `numba==0.66.0`, `onnx-weekly==1.23.0.dev20260706` が解決された

---

## tab-maker からの移植知見(Phase 1)

以下は tab-maker の `ISSUES.md`(課題1の項2「分離モデルの変更」、解決済み扱い)
および該当コミット(`Default to guitar-specialized Mel-Roformer separation
(becruily)`)からの要約。StemLab の `separate.py` は、この調査結果をそのまま
前提にして書かれている。

### なぜ becruily の Mel-Band Roformer をギターのデフォルトにしたか

実曲(ミックス済みの楽曲)1曲に対し、4方式(旧来の htdemucs_6s、becruily の
ギター特化 Mel-Band Roformer、二段階パイプライン2種)を実測10指標+
スペクトログラム比較+試聴で比較した結果:

- **becruily のギター特化 Mel-Band Roformer**が、htdemucs_6s に比べて
  - ボーカル混入が **1/3** に減少
  - 静かなアルペジオ部分の保持が向上(htdemucs_6s は落としがちだった)
  - 処理速度は同オーダー(実用上の劣化なし)
- 二段階パイプライン(分離を2回重ねる方式)は音質面では最もクリーンだったが、
  **5倍遅い**割に roformer 単体との体感差がほぼ無かった(B-D 相関 0.993)
- 当時調査した viperx のギター特化 BS-Roformer は、公開リポジトリには存在せず
  MVSep サイト専用の非公開モデルだったため採用候補から除外

この比較結果自体(supported_models_full.json 相当のモデルカタログ調査資料)は
tab-maker リポジトリのコミット履歴・作業メモにのみ残っており、ファイルとして
コピーはしていない。再調査が必要になった場合は tab-maker 側の該当コミット
(`008d401 Default to guitar-specialized Mel-Roformer separation (becruily)`)
を参照すること。

### becruily モデル採用に必要だった3つの回避策(そのまま移植済み)

becruily/mel-band-roformer-guitar は audio-separator のビルトインカタログに
無いモデルのため、採用には以下3つの「検証済みの回避策」が必要だった。
`src/stemlab/separate.py` にコメントごと移植してある:

1. **HuggingFace からの自動ダウンロード**: audio-separator 自身のダウンローダは
   このモデルのファイルを知らないため、`model_file_dir` に事前に配置しておく
   必要がある(`_download_if_missing`)。
2. **カタログへの注入**: `Separator.list_supported_model_files()` の戻り値に
   このモデルのエントリが無いと `load_model()` がファイル名を拒否するため、
   インスタンス単位でこのメソッドをラップして注入する(`_inject_becruily_catalog`)。
3. **audio-separator 0.44.3 自身のバグの回避**: `mel_band_roformer.py:314` が
   `mlp_expansion_factor` を `MaskEstimator` のコンストラクタに渡し忘れており、
   このチェックポイント用の値(1)ではなくクラスデフォルト(4)が使われてしまう。
   結果、チェックポイントの重みの shape が合わずロードに失敗する。
   `load_model()` 実行中だけ `functools.partial` でこの値を差し込む
   (`_patched_mask_estimator_mlp_expansion_factor`)。このバグは upstream の
   master でも未修正(2026-07-11 時点)。

### mdxc_separator の失敗時挙動(再掲・separate.py での扱い)

`mdxc_separator` はチェックポイントの state_dict 不一致時に例外ではなく
`sys.exit(1)` を呼ぶため、`separate()` は `except (Exception, SystemExit)` で
両方を同じ `RuntimeError` に変換している(呼び出し側は1種類の例外だけ見ればよい)。

### フォールバック方針(StemLab での再確認)

デフォルトモデル(`spec.default_model`)がロード/分離に失敗した場合のみ、
`spec.fallback_model`(ギターは `htdemucs_6s.yaml`)へ自動フォールバックする。
`--model` で明示的にモデルを指定した場合は、そのモデルの失敗はそのまま
`RuntimeError` として送出され、フォールバックは一切行わない
(呼び出し側の意図を上書きしないため)。

---

## Phase 1.5(Docker 第一級対応)の技術的知見

### torch/torchvision を linux だけ CPU ホイールにする(`tool.uv.sources` + marker)

PyPI の linux 向け torch デフォルトホイールは CUDA 同梱で、`nvidia-cudnn-cu13`
/ `nvidia-nccl-cu13` などの依存を巻き込んで数 GB に膨れる。`cpu` Docker
イメージは GPU を一切使わないので、linux 解決時だけ
https://download.pytorch.org/whl/cpu の CPU 専用ホイールに向けたい。

```toml
[tool.uv.sources]
torch = [
    { index = "pytorch-cpu", marker = "sys_platform == 'linux'" },
]
torchvision = [
    { index = "pytorch-cpu", marker = "sys_platform == 'linux'" },
]

[[tool.uv.index]]
name = "pytorch-cpu"
url = "https://download.pytorch.org/whl/cpu"
explicit = true
```

**罠**: `tool.uv.sources` はプロジェクトの**直接依存**にしか効かない。
torch/torchvision はもともと `audio-separator` 経由の**推移依存**(直接
`[project.dependencies]` には書かれていない)だったため、この設定を追加
しても `uv lock` の結果は無反応(相変わらず PyPI の CUDA 同梱版が解決され
続けた)。`uv lock -v` で確認したところ、pytorch-cpu インデックスへは
一度もリクエストが飛んでいなかった。

**対処**: `torch>=2.3` と `torchvision` を `[project.dependencies]` に
**直接依存として明示的に追加**した。これで `tool.uv.sources` の marker が
効くようになり、`uv lock -v` のログで
`https://download.pytorch.org/whl/cpu/torch/` への実際のリクエストを確認
できた。結果として linux 解決は `torch==2.13.0+cpu` /
`torchvision==0.28.0+cpu`(`nvidia-*` 系パッケージ0個)、darwin (arm64)
解決は従来どおり PyPI の `torch==2.13.0`(無印、MPS 対応ビルド)のまま
分岐している。

もともと torch はコード上で直接 import している(`separate.py` が
`torch.cuda.is_available` 等を触る)ので、直接依存への昇格自体は自然な
判断だが、「`tool.uv.sources` は直接依存にしか効かない」という制約を
把握していないと、`uv lock` が黙って何も変えない(かつエラーも出さない)
という分かりにくい失敗の仕方をするので明記しておく。

ホスト側の完了条件確認:
- `uv sync`: 問題なく成功(darwin arm64 は無印 torch のまま、追加の
  ダウンロードは stemlab 自体の再ビルドのみ)
- `uv run pytest -q`: 60 件全件グリーン(変更前と同じ)

### diffq==0.2.4 のインストールには gcc が要る(cp313 wheel が存在しない)

`audio-separator` の依存 `diffq==0.2.4` は cp310 までの wheel しか PyPI に
置いておらず、Python 3.13 ではプラットフォームを問わず sdist からの
C 拡張ビルドになる。`uv sync` はこの sdist をビルドしようとし、`gcc` が
無いと `error: command 'gcc' failed: No such file or directory` で失敗する。
`python:3.13-slim` には最初から入っていないため、`build-essential` を
入れる専用ステージ(`build-base`)を挟んだ(下記参照)。

### Dockerfile: レイヤ構成と最終イメージの軽量化

`cpu` ターゲットの最終イメージに `gcc` 一式(数百MB)を残したくないので、
「ビルド専用ステージ」と「出荷用の薄いステージ」を分けた:

```
uv-base ─┬─> build-base(+ build-essential)─> deps(依存のみ)─> app-build(+ src, プロジェクト本体)
         │                                                         │
         └────────────────────────> cpu(app-build から .venv と src だけ COPY)
```

- `deps` は `pyproject.toml`/`uv.lock` だけを COPY して `uv sync --frozen
  --no-install-project --no-dev` を実行 → 依存関係だけのレイヤとしてキャッシュ
  され、`src/` 配下の変更ではこのレイヤは再ビルドされない。
- `app-build` で `src/` を追加してプロジェクト自体をインストール(pure
  Python の `uv_build` バックエンドなのでこの段階では gcc は不要)。
- 最終 `cpu` ステージは `build-essential` を入れていない `uv-base` から
  分岐し、`app-build` から `.venv` と `src` だけを `COPY --from=` する。
  `uv sync` を最終ステージで再実行しないので gcc も uv のダウンロード
  キャッシュも最終イメージには残らない。
- `dev` ターゲットはテスト実行用(出荷しない)なので、この軽量化は行わず
  `build-base` 系列のまま playwright/chromium を追加している。

### cuda イメージ: base / torch / onnxruntime の選定根拠

このマシン(macOS arm64)では GPU 付きコンテナを実行できない
(Docker Desktop の VM に GPU パススルーが無い)ため、`cuda` ターゲットは
**ビルドが通ることまで**を目標とし、**GPU での実行(実分離)は未検証**。
ビルド自体は `--platform linux/amd64`(QEMU)で一度成功している
(2026-07-11: 27分、イメージ 8.83GB、torch 2.13.0+cu130 /
onnxruntime-gpu 1.27.0 が解決・インストールされ、QEMU 上で `stemlab
--help` の起動まで確認)。生成イメージはディスク逼迫のためローカルには
残していない(定義から再ビルド可能)。NVIDIA GPU 環境で使う前に必ず
動作確認すること。

- **linux/amd64 専用**: `onnxruntime-gpu` は PyPI に **x86_64 wheel しか
  公開していない**(1.27.0 で確認: manylinux x86_64 と win_amd64 のみ、
  aarch64 なし)。そのため linux/arm64 向けには依存解決自体が不可能で、
  arm64 ホストでは `docker build --platform linux/amd64 --target cuda`
  (QEMU エミュレーション)でしかビルドできない。GPU 実運用環境は
  x86_64 が普通なので実害はない。
- **base image**: `nvidia/cuda:13.1.2-runtime-ubuntu24.04`。CUDA 13 系を
  選んだ理由は下記 onnxruntime の項を参照(当初 12.8.1 で検討したが、
  onnxruntime-gpu 1.27 の CUDA 13 移行に合わせて 13 系に変更)。
- **Python 3.13 の導入方法**: Ubuntu 24.04 の apt には Python 3.13 パッケージ
  が無い(deadsnakes 等の PPA 追加が必要になる)。代わりに `uv python
  install 3.13` で uv 自身にスタンドアロン Python ビルドを取得させる方式
  にした。プロジェクトで uv を既に使っているので追加の依存が増えない。
- **torch/torchvision の CUDA ビルド選定**: `cpu`/`dev` 系列は
  `pyproject.toml`/`uv.lock` を `uv sync --frozen` でそのまま使うが、
  そのロックファイルは(前述の通り)linux 解決を CPU ホイールに固定して
  いるため、`cuda` ステージでは **`uv sync --frozen` を使わず**、
  `uv pip install` で venv を直接組み立てている。torch の CUDA 版取得には
  uv 組み込みの `--torch-backend`(環境変数 `UV_TORCH_BACKEND=cu130`)を
  使用 — これは uv が公式に持つ「PyTorch エコシステム専用インデックス
  自動解決」機能で、torch/torchvision を対応する `download.pytorch.org`
  の CUDA ビルドから自動的に取得する。uv.lock と同じ torch 2.13.0 は
  cu126/cu129/cu130 の3系で公開されており(インデックスを直接確認)、
  onnxruntime-gpu 1.27 の CUDA 13 移行と揃う cu130 を選んだ。
- **onnxruntime の選定**: `audio-separator` の PyPI メタデータ
  (`Requires-Dist`)を確認すると、`onnxruntime`(CPU版)は
  `extra == "cpu"`、`onnxruntime-gpu` は `extra == "gpu"` の任意依存に
  なっている。そのため `cuda` ステージでは `audio-separator[cpu]` では
  なく **`audio-separator[gpu]`** を指定し、`onnxruntime-gpu` を入れて
  いる(`onnxruntime` 無印は入らない)。バージョンは `>=1.17` 制約から
  最新の 1.27.0 が解決されるが、**onnxruntime 1.27 の PyPI GPU wheel は
  CUDA 13 ターゲット**(1.26 のリリースノートで「CUDA 12 サポートは
  1.27.0 で削除」、1.27.0 で「CUDA 12 パッケージは deprecated」と明言)。
  これが base image を 12.x ではなく 13 系にした決め手。
- **build-essential が必要**: `diffq==0.2.4` は cp313 向け wheel を一切
  公開していない(cp310 まで。arm64 に限った話ではない)ので、x86_64
  でも sdist からの C 拡張ビルドになり gcc が要る。cuda ステージにも
  `build-essential` を入れた。
- **prerelease 設定**: `pyproject.toml` の `[tool.uv] prerelease =
  "if-necessary"` は `uv sync`/`uv lock` にのみ適用され、`uv pip install`
  という低レベル API には自動で伝播しないため、`cuda` ステージの
  `RUN uv pip install ...` には明示的に `--prerelease=if-necessary` を
  付けている(`onnx-weekly` がプレリリースのみ公開のため)。

### `docker build --platform linux/amd64`(QEMU エミュレーション)について

`--platform linux/amd64` でのビルドは QEMU エミュレーション経由になり、
arm64 ネイティブビルドより明らかに遅い(特に `diffq` の gcc ビルドと
apt 展開)。実測(2026-07-11、Apple Silicon / Docker Desktop VM 8GB):

- `cpu` ターゲット arm64 ネイティブ: 約1分50秒(キャッシュなし、
  イメージ 2.04GB)
- `cpu` ターゲット amd64 (QEMU): 17分43秒(イメージ 2.17GB)。
  ビルド成功に加え、QEMU 上で `stemlab --help` の起動も確認。

### Docker Desktop VM のディスク逼迫(ビルド失敗の罠)

VM のディスク(このマシンでは 58.4GB)が満杯になると、`apt-get update`
が **GPG エラー(`At least one invalid signature was encountered`)** という
一見無関係なエラーで失敗する(gpg の一時ファイルが書けないため)。cuda
イメージの初回ビルドはこれで失敗した。ディスク容量不足を疑うこと:
`docker run --rm alpine df -h /` で VM 内の使用率を確認できる。
`docker builder prune` / 不要イメージ削除で解消する。また
`docker rmi` でタグを消してもビルドキャッシュがレイヤを掴んでいる間は
実容量が戻らない(`docker builder prune` まで必要)ことにも注意。

---

## Phase 2(多楽器ターゲット)のモデル選定

### vocals: vocals_mel_band_roformer.ckpt

audio-separator 0.44.3 のカタログ(`Separator.list_supported_model_files()` の
`scores`、公表 SDR 実測値)を vocals SDR でランキングした結果から選定:

| SDR | モデル | stems |
|---|---|---|
| **12.60** | **vocals_mel_band_roformer.ckpt**(採用) | vocals/other |
| 12.52 | melband_roformer_big_beta4.ckpt | vocals/other |
| 12.44 | mel_band_roformer_kim_ft_unwa.ckpt | vocals/other |
| 12.10 | model_bs_roformer_ep_368_sdr_12.9628.ckpt | vocals/instrumental |
| 10.79 | htdemucs_ft.yaml | 4-stem Demucs |

- カタログ組み込みモデルなので audio-separator 自身がダウンロードできる
  (becruily のようなブートストラップは不要)
- 2-stem(vocals/other)なので backing = other stem がそのままカラオケ音源になる
- フォールバックは htdemucs_6s.yaml(Vocals stem あり、guitar と共通)
- 品質はユーザー試聴ゲートで確認(SDR はあくまで序列の目安)

### bass / drums / piano: htdemucs_6s.yaml の stem 流用

カタログにこれらの単独特化モデルで htdemucs_6s を上回る実測スコアのものが
無いため、6-stem Demucs から該当 stem を抽出する。デフォルト=フォールバック
候補と同一になるので fallback_model は None(自分自身への再試行は無意味)。

### キャッシュのターゲット別スコープ化

多楽器化に伴い、分離ステップの meta 名を `separate:<target>`、バッキングを
`<target>.backing.wav` に変更した。共有 meta のままだと同じ曲で --target を
切り替えるたびに params 不一致で全再分離(数分〜数十分)が走り、バッキングも
上書きされるため。パッケージ側の出力も同様に `<曲名>.<target>.backing.*` /
`<曲名>.<target>.player.html` とし、1フォルダに複数ターゲットが共存できる
(original.mp3 のみ全ターゲット共通なので共有)。
