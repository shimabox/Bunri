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
