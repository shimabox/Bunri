# ギター練習パッケージへの L/R 定位分割トラック追加 実装計画

- 日付: 2026-09-26
- リポジトリ: github.com/shimabox/Bunri
- ベースブランチ: main
- ベース SHA: ec07aa0892991f977e390f40312a708421f6cb01
- 作業ブランチ: plan/2026-09-26-guitar-lr-split
- 起案担当: 予定: claude-fable-5-1 / high。確認済み: claude-fable-5-1(CLI 結果 JSON の modelUsage)。effort は未確認
- 計画レビュー担当: 予定: gpt-6-astra / high(未実行)
- 編成の例外理由: なし
- 実装担当: claude-opus-5-5 / high(通常段)。信号処理の移植(scipy から torch)、パッケージ生成・メタデータ・プレイヤー・Web の横断変更、既存パッケージ向けの新コマンドを含むため
- 実装レビュー担当: 予定: gpt-6-sol / high
- 計画書の扱い: レビュー・privacy 検査後に作業ブランチ plan/2026-09-26-guitar-lr-split へ commit(docs/plans/2026-09-26-guitar-lr-split/plan.md と docs/plans/2026-09-26-guitar-lr-split/request.md)
- 公開範囲: 作業ブランチへの commit まで。push・PR 作成・main への統合・リリースは別途承認

## 背景・目的

多くのロック/ポップスでは複数のギターが左右に振り分けられている。現状の練習パッケージは「ギターのみ」stem が 1 本で、左右のパートを聴き分けられない。分離済みギター stem を定位(ステレオ角)で 2 分割し、「L のみ / R のみ」の 2 トラックを練習パッケージとプレイヤーに追加する。分割方式・閾値・名前・UI の挙動は確定済みの仕様(方針 9 の判定ロジックと各既定値)で、本計画では変えない。

既存パッケージについては、元の音源を用意しなくても、キャッシュに残っている分離済み stem から L/R をまとめて追加できるコマンドを設ける。Web で同じ曲を再アップロードしても完了済みジョブが再利用されて build_package が走らないため、この手段が必要になる。

## 確定済みの仕様

以下は確定事項であり、実装で変更しない。

- scipy を直接依存に加えず、STFT/ISTFT は torch で実装する(方針 7)。
- L/R の有無(作ったか、山が 1 つで作らなかったか)を身元ファイル `.bunri-package.json` の target 項目のフィールド `pan_split` に記録する。schema_version は 1 のまま(方針 2)。
- L/R は Bunri Pocket には送らない(方針 4)。
- L/R は Web のダウンロード欄に出す(方針 3)。
- 曲一覧で「L/R に分かれていない曲」の注記は出さない(方針 11)。
- 既存パッケージ用に新コマンド `bunri lr-split` を設ける(方針 12〜16)。内部名(モジュール・キャッシュ段・身元ファイルのフィールド・関数・ロックファイル)は `pan_split` のままとする。
- 分割方式・閾値・トラック名・プレイヤーの挙動は方針 1・9・10 のとおり。

## スコープ

- 対象:
  - 定位分割モジュール `src/bunri/pan_split.py` の新設(torch による STFT、基準実装と同じ判定ロジック)
  - `src/bunri/registry.py`: TargetSpec に分割対象フラグを追加(guitar のみ有効)
  - `src/bunri/package.py`: キャッシュ段 `pan_split:<target>` の追加、L/R の wav/mp3 書き出し、身元ファイルへの記録、既存パッケージ向け関数 `add_pan_split` と build_package との共通化
  - `src/bunri/package_metadata.py`: target 項目に任意フィールド `pan_split` を追加(schema_version は 1 のまま)、フィールドだけを更新する関数、`begin_target` / `complete_target` / 新関数の読み込みから書き込みまでを共通のプロセス間ロックで保護
  - `src/bunri/cache.py`: 段の完了確認ヘルパー(params を問わない)
  - `src/bunri/lock.py` 新設(待ち時間付き取得を持つ `ProcessLock`)と `src/bunri/pocket/lock.py` の内部委譲(公開 API は不変)
  - `src/bunri/lr_split_cli.py` 新設と `src/bunri/cli.py` の dispatch 追加(`bunri lr-split`)
  - `src/bunri/player.py` / `src/bunri/templates/player.html.j2`: 「L のみ / R のみ」ボタン、山 1 つ時の押せない表示と理由、キー 4/5
  - `src/bunri/local_package.py` / `src/bunri/web/app.py`: L/R ファイルの検査とダウンロード欄への追加(完了判定は変えない)
  - テスト(単体・build_package 統合・CLI 統合・プレイヤー Playwright・Web 直列化・Pocket preflight 回帰・ロック)
  - README の「できあがるファイル」表・ツリー・既存パッケージへの追加手順
- 対象外:
  - guitar 以外の target への適用(フラグは用意するが有効にしない)
  - 新しい分離モデルの追加、分割方式・閾値・名前の変更
  - 中央寄せ・スライダー・混ぜ具合調整などの UI
  - Bunri Pocket への L/R 送信(プロトコル v1 の範囲外。将来のプロトコル改訂で扱う)
  - 曲一覧での「L/R に分かれていない曲」の注記(ダウンロード欄に L/R が無いだけで足りる)
  - 山が 1 つの曲を山の位置で機械的に分ける案、ボタンを隠す案
  - 長尺曲向けのチャンク処理(方針 8 で理由を述べる)
  - Web UI からの「既存パッケージに L/R を追加」操作(CLI のみ。必要なら後続)
  - Web の分離ジョブ実行機構(web/jobs.py のランナー)へのロック導入
  - バージョン番号(pyproject の version)の更新はリリース手順に委ねる

## 方針

### 1. 出力ファイル名と形式

既存の backing と同じく target スコープで役割サフィックスを付ける。

| ファイル | 中身 |
|---|---|
| `<safe>.<target>.left.wav` / `.mp3` | L のみ(境界より左側の成分) |
| `<safe>.<target>.right.wav` / `.mp3` | R のみ(境界より右側の成分) |

既存方針「wav は常に、mp3 は `mp3=True` のとき追加」に合わせる。`mp3=False` のときはプレイヤーが wav を参照する(既存の target/backing と同じ分岐)。mp3 のタイトルメタデータは `"<title> (<label_ja> L のみ)"` / `"<title> (<label_ja> R のみ)"`。wav はキャッシュ側 `out/.cache/<key>/<target>.left.wav` / `.right.wav` に書き、既存の `_export` でコピーする。wav の書き出しは PCM16、サンプルレートは入力 stem と同じ。値は [-1, 1] にクリップする(マスクは 1 以下なので実質発生しないが安全のため)。

### 2. 身元ファイル `.bunri-package.json`

target 項目に任意フィールド `pan_split` を追加し、schema_version は 1 のまま据え置く(確定)。

```json
{"target": "guitar", "formats": ["mp3", "wav"], "pan_split": "left_right"}
```

- 値は `"left_right"`(L/R ファイルあり)または `"single"`(山が 1 つで作らなかった)。分割を試みない target(bass 等)や本変更前に作られたパッケージでは省略。
- 記録する理由: 「作らなかった(正常)」と「あるはずが無い」を区別でき、Web・検証・新コマンドのスキップ判定が同じ根拠で動く。
- 互換: `package_metadata._validate` は target 項目の未知キーを無視するため、旧版 Bunri は新フィールド付きの sidecar をそのまま読める。ただし旧版の `_payload` は target/formats しか書き戻さないため、旧版が別 target を追加ビルドした場合は guitar 項目の `pan_split` が落ちる(ファイルは残るが Web のダウンロード欄から消える)。ダウングレード時のみの既知の制限とし、新版では `TargetMetadata` にフィールドを持たせて `_payload` で書き戻す。`begin_target` は他 target の項目を dataclass ごと保持するので、新版同士では消えない。
- 検証は既存と同じ厳格さ: 存在する場合は文字列で上記 2 値のいずれか。それ以外は `ValueError`。`local_package._metadata_issues` も同じ規則で issue を出す。
- フィールドだけを更新する関数 `set_target_pan_split(path, *, safe_name, target, pan_split) -> PackageMetadata` を追加する。方針 15 の身元ファイルロックの下で、読み込み、当該 target 項目の存在確認(無ければ `TargetNotFoundError`。`ValueError` の派生)、`pan_split` だけ差し替えて `write_package_metadata` で原子的に書き戻す。`begin_target` のように項目を一度消さないので、Web が途中で読んでも target が消えた状態を見ない。新コマンドはこれを使い、build_package は従来どおり `begin_target` / `complete_target` を使う(`complete_target` に `pan_split` 引数を足す)。3 関数とも同じロックの下で読み書きするので、別プロセスの更新を上書きして消すことはない(方針 15)。
- Pocket の manifest は `pan_split` を扱わない(方針 4)。

### 3. Web UI のダウンロード欄

`pan_split == "left_right"` の target について、既存の「ギターのみ / ギターなし」の後ろに 2 行を足す(確定)。

| track | label | ファイル名サフィックス |
|---|---|---|
| `left` | `L のみ` | `.left` |
| `right` | `R のみ` | `.right` |

ダウンロード時のファイル名は既存規則 `{title}_{label}.{format}` のまま。曲一覧の完了判定(`TargetArtifactInspection.complete_formats`)と `missing_files` は変えない。L/R が無いのは正常であり、`pan_split` が `"left_right"` でファイルが欠けていても状態を変えず、ダウンロード欄に出さないだけとする。`PackageArtifactInspection.issues` にも L/R を追加しない(issues が状態判定に使われる経路を全て確認できないため、状態に影響し得る経路には載せない)。

新コマンドで L/R を追加した後は、曲一覧が `scan_local_packages` で sidecar を読み直した時点でダウンロード欄に反映される。ジョブ記録経由の `_download_files(job)` は変えない(曲一覧は artifact 経路で downloads を上書きするため。未確定事項に条件を残す)。

### 4. Pocket 同期

送らない(確定)。`pocket/local.py` の preflight は original / `<target>.mp3` / `<target>.backing.mp3` だけを要求・送信するので、L/R ファイルは無視される。`pan_split` フィールドは `PackageMetadata` の dataclass 追加で運ばれるが、manifest 生成は target/formats しか使わないため出力に現れない。回帰テストで「L/R ファイルがあるパッケージの preflight 資産が 3 つのまま」を固定する。

### 5. パッケージ検証・曲削除

- 削除は `web/jobs.py` の delete_song がディレクトリ単位で消すため、L/R ファイルも一緒に消える。追加対応なし。
- 検証(`local_package.inspect_package_artifacts`)は `TargetArtifactInspection` に `pan_split: str | None`、`left_files`、`right_files` を追加し、`pan_split == "left_right"` のときだけ各 format の L/R を `inspect_artifact` する。`complete_formats` は変えない。`PackageIdentityInspection.targets` のタプル形状は `pocket/local.py` が展開しているので変えず、`identity.metadata` から `pan_split` を引く。
- 判定が `"single"` のとき、パッケージ内の `<safe>.<target>.left/right.{wav,mp3}` を `unlink(missing_ok=True)` で除去する(過去の版で作られた L/R が sidecar と食い違って残るのを防ぐ。unlink は名前を消すのでシンボリックリンクを辿らない)。build_package と新コマンドの両方で同じ共通関数が行う。

### 6. キャッシュ段

キャッシュ段 `pan_split:<target>`(`_PAN_SPLIT_VERSION = 1`)を separate 段の直後に置く。

- params: `{"target", "n_fft": 4096, "hop": 1024, "pad_mode": "constant", "bins": 45, "smooth": 3, "min_peak_distance": 4, "softness": 0.04, "min_share": 0.1}`
- 常に書く出力: `<target>.pan_split.json`(status、境界角、左側の音量比、山の位置)。判定が `"left_right"` のときは `<target>.left.wav` / `<target>.right.wav` も書く。
- 鮮度: `stage_is_fresh` の outputs は json のみとし、json を読んで `"left_right"` なら両 wav が `is_real_file_in` を満たすことを追加で確認する。満たさなければ再実行。
- カスケード: separate 段が今回実行された場合は強制再実行(normalize から separate への既存規則と同じ)。`no_cache` でも再実行。`clear_stage_meta` を実行前に呼ぶ規律も既存と同じ。
- 効果: 元の音源がある場合は `bunri <入力>` の再実行でも pan_split だけ新規に走り、L/R と sidecar のフィールドが追加される。元の音源が無い、または Web で完了済みジョブが再利用される場合は方針 12 以降の新コマンドを使う。両者は同じキャッシュ段を共有するので、どちらで作っても次回はキャッシュが効く。

### 7. 依存

torch で実装し、scipy を直接依存に加えない(確定)。

- STFT は `torch.stft(x, n_fft=4096, hop_length=1024, window=torch.hann_window(4096)(periodic=True が既定), center=True, pad_mode="constant", return_complex=True)`、ISTFT は `torch.istft(..., n_fft=4096, hop_length=1024, window=同じ窓, center=True, length=n)`。基準とした scipy 実装の既定(hann、nperseg 4096、noverlap 3072、boundary='zeros')と同じ窓・同じホップ・同じ端処理で完全再構成を満たす。マスクは比率なのでスケーリング差は結果に影響せず、`a + b = 1` と線形性から `左 + 右 = 元 stem` が保たれる。
- 端処理: `pad_mode` は既定の reflect ではなく `"constant"`(前後 2048 サンプルのゼロ詰め)に固定する。理由は 2 つ。第 1 に、reflect は入力長が `n_fft // 2`(2048)以下だと失敗し、既存フェイク(tests/test_package.py の 400 サンプル stem)を処理できない。constant は 1 サンプル以上の任意長で動く。第 2 に、scipy.signal.stft の既定 `boundary='zeros'` と同じ端処理なので、基準実装との差が小さくなる。残る差は scipy の `padded=True` による末尾の半端フレーム(hop 未満、23 ms 分)の有無だけで、未確定事項の ±1 区分リスクに含める。
- 復元: `center=True` と hann/hop 1024 の組み合わせでは、`istft` が切り出す範囲 `[2048, 2048 + n)` のどの位置も最寄りのフレーム中心から 512 サンプル以内にあり、窓の重なり(NOLA)の下限が `0.85²` 以上になる。したがって `istft(length=n)` はどの長さでも例外なく元の長さ n に戻る。短い入力を別経路にしない。サンプル数 0 は STFT に入れず方針 9 の防御で `"single"` にする。
- 判定部(ヒストグラム、移動平均、山と谷、音量比)は numpy で書き、torch は STFT/ISTFT のみに使う。CPU 固定で `device` は受けない。
- リスク: 末尾の半端フレームと浮動小数点の差で境界のビンが ±1 ずれる可能性がある。受け入れ基準の合成音テストで判定を固定し、基準実装で確認した実曲での同一判定は利用者側の確認事項とする(未確定事項に記載)。

### 8. 処理時間・メモリ

5 分の stem で、complex64 の STFT が 1 チャンネル約 210 MB、角度・音量・マスクを含めた実装で 1 GB 前後、10 分で 2 GB 前後になる。分離モデル自体の必要メモリを下回るので、チャンク処理は入れない。実装上の配慮として float32/complex64 を維持し、|L|・|R| は角度と音量を作ったら解放し、ISTFT は片側ずつ逐次実行して書き出し後にテンソルを捨てる。処理中は `console.status` で表示し、完了時に経過秒と判定結果(境界の位置、両側の音量比、または「山が 1 つ」)を出す。

### 9. 判定ロジックの契約(基準実装と同一。高さ 0 の区分を山にしない規則だけを追加)

入力は STFT の各時間周波数点(以下「点」)の `angle = atan2(|R|, |L|)`(0〜π/2)と `energy = |L|² + |R|²`。

1. 事前の防御: stem が 2 チャンネルでない、サンプル数が 0、または時間領域の全サンプルが 0 のときは STFT を行わず `"single"`(ゼロ除算・NaN を避ける)。
2. ヒストグラム: `hist = np.histogram(angle, bins=45, range=(0, π/2), weights=energy)[0]`。右端 π/2 は最後の区分に含まれる(np.histogram の規則)。energy が 0 の点は angle が 0 になるが重み 0 なので寄与しない。区分 k の中心角は `mids[k] = (k + 0.5) · (π/2) / 45`。
3. 平滑化: `smooth = np.convolve(hist, np.ones(3) / 3, mode="same")`(端はゼロ詰め)。
4. 山: `smooth[i] > 0` かつ `smooth[i] >= smooth[i-1]` かつ `smooth[i] >= smooth[i+1]`(端は存在する側だけ比較)。高さ 0 の区分は山にしない。同値の隣接区分(平坦部)はすべて山になるが、第 1 の山から距離 4 未満は候補にならないので実害はない。
5. 第 1 の山: `smooth` が最大の区分。同値は最小添字(np.argmax)。候補は山のうち `abs(i - first) >= 4` のものを高さの降順、同値は添字の昇順(安定ソート)に並べて順に試す。
6. 谷: 第 1 の山と候補を両端に含む区間 `min(first, cand)` 〜 `max(first, cand)`(両端を含む)から `smooth` が最小の区分。同値は最小添字(np.argmin)。
7. 音量比: 点ごとに `mask = 1 / (1 + exp((angle - mids[valley]) / 0.04))`、`share = Σ(mask · energy) / Σ energy`。`min(share, 1 - share) >= 0.10` を最初に満たした候補を採用し、`status = "left_right"`、境界 = valley、左側の音量比 = share とする。
8. 採用候補が無ければ `"single"`。谷/山の比では足切りしない。softness 0.04、閾値 0.10、区分 45、平滑 3、距離 4 は固定。

- 手順 2〜7 の同値規則(第 1 の山は最大値の最小添字、候補は高さ降順・同値は添字昇順の安定ソート、谷は区間内最小値の最小添字)、np.histogram の区分の取り方、np.convolve の mode="same"、音量比 `Σ(mask · energy) / Σ energy` は基準実装と同一であることを確認済みである。基準実装との違いは手順 4 の「高さ 0 の区分を山にしない」だけである。
- 4 の「高さ 0 を山にしない」は明文化のための追加規則で、実曲(平滑化後の全区分が正)の判定は変えない。これが無いと中央定位の合成音でゼロ区分が山になり、区分 24 を谷にして約 85%/15% で 10% 条件を通ってしまう。L と同じ波形を R に振幅 0.1 で置いた合成音(全点の角が atan(0.1)、区分 2)も同様に区分 4 を谷にして通ってしまう。
- 角度が全点で一定になる入力は、有限長の STFT でも厳密に予測できる。同一信号を両チャンネルに置くと全点が π/4(区分 22)になり、平滑化後の正の区分は 21〜23 の平坦部だけで、第 1 の山 21 から距離 4 以上の候補が無いので `"single"`。L と同じ波形を R に振幅 0.1 で置くと全点が区分 2 になり、正の区分は 1〜3 だけで同じく `"single"`。
- 角度が分布する入力では、有限長 STFT と理想化入力で振る舞いが違う。角度 0 と π/2 に同じ総重みを直接置いた理想化配列では、正の区分が 0〜1 と 43〜44 だけになり、第 1 の山 0 に対する候補は 43、谷は第 1 の山に隣接する最初のゼロ区分 2、左側比率は約 0.45(角 0 の点の mask が約 0.90、π/2 の点がほぼ 0)。一方、L に 220 Hz、R に 440 Hz(同振幅)の 2 秒・8 kHz の合成音を STFT に通すと、窓の漏れで各時間周波数点が中間の角を持ち、平滑化後の全 45 区分が正になる。実測では第 1 の山は端の隣の区分 43(端の区分は平滑化の外側がゼロ詰めで低くなる)、谷は 2 山の間の中央付近の区分 20、左側比率は約 0.500(L の点の mask がほぼ 1、R の点がほぼ 0)。「最初のゼロ区分が谷になる」のは理想化入力だけの性質であり、有限長 STFT の受け入れ条件には境界の範囲と比率の範囲だけを指定する(完了条件)。
- 実装 API(テストの入口): `decide(angle, energy, ...) -> PanSplitDecision` を純 numpy の関数として分け、`split_stem(src, left_dest, right_dest) -> PanSplitDecision` がファイル I/O(`replace_into` 経由)を担う。判定は決定的で、同じ stem からは常に同じ結果が出る(方針 15 の並行動作の根拠)。

### 10. プレイヤー

- `render_player` に `left: str | None = None`、`right: str | None = None`、`pan_split_note: str | None = None` を追加(既存呼び出しは無変更で動く)。
- 表示規則: `left`・`right`・`pan_split_note` の全てが None なら L/R ボタンを描画しない(分割を試みない target 向け)。`left`/`right` があれば既存 3 ボタンの後ろに「L のみ」「R のみ」を並べる。`pan_split_note` があれば両ボタンを `disabled` で描画し、トラック行の下に理由文を出す。理由文は判定が `"single"` のときに「L/R に分かれていない曲です」を渡す。
- JS: `TRACKS` を `["original","target","backing","left","right"]` に、`LABELS` に `left: "L のみ"`、`right: "R のみ"` を追加。既存の同期・切替・resync はトラック名の配列で動くので変更不要。要素やボタンが無いトラックは既存の `absent` 経路で扱われる。
- キー 4/5 で left/right に切替。ヘルプ文を「1〜5 トラック切替」に更新。オフライン注意書きのファイル一覧にも L/R がある場合だけ追記する。
- 実行時にファイルが欠けている場合は既存の `onErr` で「（無し）」表示になる(理由文とは別の状況なのでそのまま)。

### 11. 判定結果の Web 表示

曲一覧に「L/R に分かれていない曲」の注記は出さない(確定)。

### 12. 新コマンド `bunri lr-split` の形

既存の `bunri` 入口(`src/bunri/cli.py`)は単一コマンドの Typer app で、`bunri <入力>` がそのまま root コマンドになっている。ここに 2 つ目のコマンドを足すと Typer がサブコマンド方式に切り替わり、`bunri song.mp3` が壊れる。そのため `bunri pocket` と同じ dispatch 方式を採る。

- `dispatch()` に `sys.argv[1] == "lr-split"` の分岐を足し、`src/bunri/lr_split_cli.py` の Typer app を `prog_name="bunri lr-split"` で起動する。コマンド名は `lr-split`(確定)とし、内部名(モジュール `src/bunri/pan_split.py`・キャッシュ段 `pan_split:<target>`・sidecar フィールド `pan_split`・`TargetSpec.pan_split`・関数 `add_pan_split` / `set_target_pan_split`・ロックファイル `out/.cache/pan_split.lock`)は `pan_split` のままとする。
- 形:

```
bunri lr-split SAFE_NAME [-o out] [--target guitar] [--force] [-v]
bunri lr-split --all     [-o out] [--target guitar] [--force] [-v]
```

- `SAFE_NAME` と `--all` はどちらか一方のみ。両方または無しは `bunri pocket sync` と同じ文面で終了コード 1。
- `--target` の既定は guitar。`TargetSpec.pan_split` が False の target は「この target は L/R 分割に対応していません」で終了コード 1。
- `--force`: sidecar に `pan_split` が記録済みでもスキップせず、キャッシュ段のメタを消して再計算し、ファイル・プレイヤー・sidecar を書き直す。既定ではフィールドが記録済み(`"left_right"` でも `"single"` でも)の target はスキップする。
- mp3 を作るかは引数にしない。sidecar の `formats` に mp3 があれば mp3 も作る(build_package の `mp3` 引数に相当する情報は sidecar が持っている)。
- `-v/--verbose`: 例外を握らずに再送出する(既存 `main` と同じ)。
- 出力先の既定は `out`。`out` は `resolve()` してから渡す。

### 13. 入力の特定と前提確認

1 パッケージあたりの手順を `package.add_pan_split(out_dir, safe_name, *, target, force) -> PanSplitOutcome` にまとめ、CLI は結果を表示するだけにする。`PanSplitOutcome` は `status`(`done` / `skipped` / `legacy` / `failed`)、`reason`、`decision`(done のとき)を持つ。

1. `local_package.inspect_package_identity(out_dir, safe_name)` で身元を確認する。
   - `legacy`(身元ファイル無し): status `legacy`。「身元ファイルがありません。元の入力音源から再生成してください。キャッシュが残っていれば分離処理は省略されます。」(pocket sync と同じ文面)
   - `invalid`: status `failed`、理由は issues の先頭。
   - `ready`: `metadata` を使う。`allow_unknown_targets=True` で読まれているので、未知 target を含む sidecar でも guitar 項目だけを扱える。
2. sidecar に対象 target の項目が無ければ status `skipped`、理由「<target> の項目がありません(<target> のパッケージでないか、分離ジョブの実行中)」。開始時点の不在は、他 target だけのパッケージと Web が同じ target を再生成中(`begin_target` と `complete_target` の間)の両方を含み、どちらも異常ではないので終了コード 0 の側に置く。開始後に項目が消える競合(手順 8)は `failed` として区別する。
3. 項目に `pan_split` が記録済みで `--force` でなければ status `skipped`、理由「記録済み: left_right」など。
4. キャッシュディレクトリ `out/.cache/<source.cache_key>` を確認する。`safepath.real_subdir(out_dir, ".cache", key)` を期待値に `is_really` で一致を確認し、シンボリックリンクでない実ディレクトリであること。満たさなければ status `failed`、理由「キャッシュがありません: out/.cache/<key>。元の入力音源から再生成してください。」。ディレクトリは作らない。
5. `cache.ensure_input_identity(cache_dir, InputDigest(source.digest, source.cache_key))` で身元を照合する。不一致は `ValueError` になるので `failed`(「キャッシュが別の入力のものです」)。識別ファイルが無いときは既存関数の挙動どおり作成される(build_package と同じ)。
6. separate 段の完了を確認する。`stage_is_fresh` は params(モデル名)の一致を要求するが、パッケージがどのモデルで作られたかは sidecar に無いので使えない。新ヘルパー `cache.stage_completed(cache_dir, stage_name, outputs) -> bool` を追加し、メタファイルが実ファイルで JSON として読め、outputs(`<target>.wav`)が `is_real_file_in` を満たすことだけを見る。メタは `clear_stage_meta` により再実行中は消えているので、書きかけの stem を掴まない。満たさなければ `failed`、理由「分離済み stem がキャッシュにありません。元の入力音源から再生成してください。」。
7. パッケージ側の `<safe>.<target>.wav` が実ファイルとして存在する場合は、キャッシュ stem と内容が一致することを確認する(サイズ比較の後に sha256)。不一致は `failed`、理由「パッケージの stem とキャッシュが一致しません。元の入力音源から再生成してください。」。同じキャッシュを共有する別タイトルのパッケージが別モデルで再分離した場合に起きうる食い違いを、L/R の出所が違う状態で残さないためである。パッケージ側 wav が無い(README が「消しても OK」としている)場合はこの確認を飛ばし、キャッシュ stem を正とする。
   - パッケージ側 wav を入力にする案は採らない。wav は削除されうるので入力として保証できず、キャッシュ段(方針 6)への出力先もキャッシュディレクトリが前提だからである。キャッシュ優先で、パッケージ側 wav は整合確認にだけ使う。
8. ここまで通れば方針 14 の共通関数で生成する。最後の `set_target_pan_split` が target 項目の不在(`TargetNotFoundError`。開始後に Web が同じ target の再生成を始めた場合)で失敗したら status `failed`、理由「<target> の項目が消えました(Web で分離ジョブ実行中の可能性)。完了後に再実行してください。」とする。このとき本コマンドが先に書いた L/R・プレイヤーは残りうるが、再生成を完了した build_package がすべて書き直すので最終状態は整合する。

`--all` は `local_package.all_package_names(out_dir)` の順に上記を繰り返す。1 件の失敗で止めず、全件処理して集計する。

### 14. build_package との共通化

`package.py` の以下を private 関数に切り出し、build_package と `add_pan_split` の両方から呼ぶ。重複コードを持たない。

- `_pan_split_step(cache_dir, spec, *, force, upstream_ran) -> PanSplitDecision`: 方針 6 の鮮度確認・カスケード・`clear_stage_meta`・`split_stem` 実行・メタ書き込み。`force` または `upstream_ran` で再実行。cached のときは json を読んで decision を復元する。
- `_export_pan_split(package_dir, safe, spec, cache_dir, decision, *, mp3, song_title) -> tuple[str | None, str | None, str | None]`: `"left_right"` なら wav(と mp3)を `_export` / `_export_mp3` で書き出し、プレイヤー用の参照名 2 つを返す。`"single"` なら方針 5 の旧ファイル除去を行い、理由文を返す。
- `_player_refs(safe, spec, mp3) -> tuple[str | None, str, str]`: original/target/backing の参照名(mp3 か wav か)。build_package の既存分岐をここに移す。
- `_write_player(package_dir, safe, spec, song_title, *, mp3, left, right, note) -> Path`: `render_player` と `replace_into` による書き込み。

`add_pan_split` は sidecar から `song_title = metadata.title`、`mp3 = "mp3" in item.formats` を得て、上記を順に呼び、最後に `set_target_pan_split` で sidecar を更新する。build_package は従来の流れの中で同じ関数を呼び、`complete_target` に `pan_split=decision.status` を渡す。

書き込み順(Web が途中で読んでも矛盾しないため):

- `"left_right"`: L/R の wav と mp3、プレイヤー、sidecar の順。sidecar が更新されるまで Web は L/R を出さないので、ファイルが揃ってから公開される。
- `"single"`: sidecar、旧 L/R ファイルの除去、プレイヤーの順。sidecar が先に `"single"` になるので、消える途中のファイルへのリンクを Web が出さない。
- 各ファイルは `replace_into`(または `audio.encode_mp3` 内の同処理)で原子的に置換される。読み手が半端なファイルを見ることはない。sidecar の更新は `complete_target` / `set_target_pan_split` のいずれも方針 15 の身元ファイルロックの下で行われ、別プロセスの更新を消さない。

### 15. 身元ファイルの排他と Web サーバーとの共存

- 問題: 既存の `begin_target` / `complete_target` は「読み込み→全体を組み立て→原子的置換」で、読み込みから書き込みまでの排他が無い。Web の分離ジョブが子プロセスで起動する `bunri` CLI と `bunri lr-split` は別プロセスで同じ sidecar を読み書きしうるため、CLI が guitar の項目を読み、Web が bass の追加を完了し、CLI が読み取った内容を書き戻すと bass が消える(逆順なら `pan_split` が消える)。ファイル単位の原子的置換では防げないので、読み込みから書き込みまでをプロセス間ロックで囲む。
- ロックの本体: fcntl による非ブロッキングロックを `src/bunri/lock.py` の `ProcessLock(path, busy_message, *, timeout=None, busy_error=ProcessLockBusy)` に移す。`acquire` はロックファイルを `O_CREAT | O_NOFOLLOW` で開き、通常ファイルであることを確認して `LOCK_EX | LOCK_NB` を試みる。`timeout` が None なら取得できなければ即 `busy_error(busy_message)`(既存 SyncLock と同じ)。数値なら 50 ms 間隔で再試行し、その秒数を超えたら `busy_error(busy_message)`。`ProcessLockBusy` は `RuntimeError` の派生。`pocket/lock.py` の `SyncLock` は同じ公開 API(`acquire` / `release` / コンテキストマネージャ、`SyncLockBusy`、`out/.pocket/sync.lock`、文面)のままこれに委譲する。
- 身元ファイルロック: `out/.cache/metadata.lock`。`package_metadata` に内部関数 `_locked_update(path, safe_name, mutate)` を置き、`ProcessLock(verified_mkdir(out_dir, ".cache") / "metadata.lock", 文面, timeout=10.0)` の下で読み込み→`mutate`→`write_package_metadata` を行う。`out_dir` は `path.parent.parent`(sidecar は検証により常に `out/<safe>/` 直下にある)。`begin_target`(既存 sidecar の読み直しを含む)・`complete_target`・`set_target_pan_split` はすべてこれを通す。出力先ごとに 1 つのロックだが、区間は JSON の読み書きだけでミリ秒なので競合しない。10 秒を超えたら文面「身元ファイルの更新待ちがタイムアウトしました。別の Bunri が実行中です。完了後に再実行してください。」で失敗し、`add_pan_split` は status `failed` の理由にし、build_package は例外のまま上げる。
- ロックファイルの置き場所: `.cache` は出力先直下の予約名で、曲一覧・`package_entry_names`・Pocket の走査に出ない。曲削除(delete_song)はパッケージディレクトリだけを消すので触れない。パッケージディレクトリ内には新しい常設ファイルを置かない(削除の「身元ファイルを最後に消す」規律と検証に影響させないため)。`.cache` 直下を列挙する処理(キャッシュ鍵ディレクトリの走査)がある場合は通常ファイルを無視することを実装担当が確認する(未確定事項)。`bunri lr-split` の二重起動防止用 `out/.cache/pan_split.lock` も同じ場所に置く。既存テストのうち `.cache` 直下を 1 要素と仮定している箇所(tests/test_package.py の `[cache_dir] = (out_dir / ".cache").iterdir()`)は、ロックファイルの常設により複数要素になって通らなくなるので、`[cache_dir] = [p for p in (out_dir / ".cache").iterdir() if p.is_dir()]` に変える。同じ仮定の箇所が他にあれば同じ形で直す。既存テストへの変更はこの種の修正に加え、身元ファイルの target 項目を辞書の完全一致で比較している既存テスト(tests/test_package.py の `sidecar["targets"] == [...]` の箇所など)に新しいフィールド `pan_split` を期待値へ加える更新(guitar の項目に判定結果の `"pan_split"` を加える。他の target の項目と他のキーは変えない)に限り、それ以外の期待値は変えない。
- 新コマンドの二重起動: `out/.cache/pan_split.lock` を `timeout=None` で取り、「別の lr-split が実行中です。完了後に再実行してください。」で終了コード 1。Pocket の `SyncLock` は流用しない(守る資源が違い、Pocket 同期は L/R を送らないので同時実行しても干渉しない)。
- Web の分離ジョブ(build_package)と新コマンドの同時実行: 分離ジョブは `pan_split.lock` に参加しないが、sidecar の更新は両者とも身元ファイルロックで直列化される。
  - Web が別 target(bass)を処理中: `begin_target(bass)` は guitar 項目を `pan_split` ごと保持し、新コマンドの `set_target_pan_split(guitar)` はロック下で読み直して guitar だけ変え、`complete_target(bass)` もロック下で読み直して bass を足す。どの順でも最終 sidecar は両方を含む。
  - Web が同じ target(guitar)を再生成中(`begin_target` と `complete_target` の間): 新コマンドの開始時なら方針 13 手順 2 で `skipped`(終了コード 0)。開始後に始まった場合は `set_target_pan_split` の項目不在(`TargetNotFoundError`)で `failed`(終了コード 1)。Web 側の `complete_target` は自分の判定を `pan_split` に書き、L/R・プレイヤーも書き直すので最終状態は正しい。
  - 新コマンドが先に公開し、その後に Web が再生成: Web が L/R・プレイヤー・sidecar をすべて書き直すので矛盾しない。
  - 同じ曲の同じ target を別プロセスが作り直し(separate 段が実際に再実行され stem が置き換わる)、その完了が本コマンドの 1 曲の処理(数十秒)の途中に重なる場合は、仕組みで防がない。本ツールは 1 人の利用者が自分の PC で使う前提で、Web は完了済みの曲を再分離しないため、この重なりは端末から同じ曲を作り直しながら同時に `bunri lr-split` を実行した場合に限られる。README で「分離の実行中は待ってから実行する」と案内し、重なった場合は `bunri lr-split --force` で作り直せる(未確定事項)。
  - キャッシュ段のメタは両者とも `clear_stage_meta` の後に書く。競合で片方のメタが消えても「鮮度なし」になるだけで、次回再実行される。
  - 読み込み中の stem が `os.replace` で差し替えられても、開いているファイル記述子は旧内容を読み続ける。
- 交差実行の検証(タスク 15): sidecar に guitar 項目(`pan_split` 無し)を用意し、`package_metadata.read_package_metadata` を「スレッド名 `web` から呼ばれたときは `entered` を立てて `proceed` を待ってから返す」ラッパーに差し替える。スレッド `web` で `complete_target(bass)` を開始し、`entered` の後にスレッド `cli` で `set_target_pan_split(guitar, "left_right")` を開始する。0.5 秒後に `cli` がまだ生きていて sidecar が未変更であること、`proceed` を立てて両方を join した後の sidecar に bass 項目と guitar の `"pan_split": "left_right"` が両方あることを確認する。役割を入れ替えた(`cli` 側を止め、`web` 側の `complete_target` を待たせる)ケースも同じ形で確認する。flock は open file description 単位なので同一プロセス内の別スレッドでも排他が効き、テストは決定的である。
- 再分離との交差実行の検証(タスク 14): 単一スレッドで決定的に行う。方針 17 のヘルパーで既存パッケージを作った後、`package._pan_split_step` を「最初の呼び出しだけ、元の関数を呼んで結果を得た後に副作用を挟んで返す」ラッパーに差し替え、CLI(`lr_split_cli.app`)を実行する。
  - 開始後の項目消失: 副作用として `begin_target(guitar)` を sidecar に適用する(Web が再生成を始めた状態)。CLI は終了コード 1、「guitar の項目が消えました」と完了後の再実行案内、sidecar は `begin_target` 後の内容のまま(L/R ファイル・一時ファイルの有無は問わない)。
- 曲削除(delete_song)との交差はロックの対象外で、build_package と同じ既存のリスクとして未確定事項に残す。
- 実行中の Web ジョブの検出は、`web/jobs.py` に digest ごとの queued/running ジョブを安価に列挙できる既存関数がある場合に限り、そのパッケージを「Web で分離ジョブ実行中」としてスキップに回す。無ければ検出機構は作らず、README で「Web の分離実行中は待ってから実行する」と案内する(未確定事項)。

### 16. 進捗表示・集計・終了コード

- 各パッケージの処理中は `console.status("[bold]lr-split[/bold] <safe> (i/N) running…")`。完了時に 1 行:
  - `完了: <safe> (境界 L12、L/R 45%/55%)` または `完了: <safe> (L/R に分かれていない曲)`
  - `スキップ: <safe>: 記録済み (left_right)` / `スキップ: <safe>: guitar の項目がありません(guitar のパッケージでないか、分離ジョブの実行中)`
  - `再生成が必要: <safe>`(legacy。黄色)
  - `失敗: <safe>: <理由>`(赤)
  - 失敗の理由には方針 13 手順 8 の文面(「guitar の項目が消えました(Web で分離ジョブ実行中の可能性)。完了後に再実行してください。」)を含む
- 表示するパッケージ名は `pocket/cli.py` の `_safe_display` と同じ制御文字置換を通し、`markup=False` で出す。
- `--all` の最後に `集計: 完了=N スキップ=N 失敗=N 再生成が必要=N`。legacy が 1 件以上あれば pocket sync と同じ再生成案内を 1 行足す。
- 終了コード: 失敗が 1 件以上なら 1、それ以外は 0(legacy とスキップだけなら 0。pocket sync と同じ)。単一指定で legacy は 1(pocket sync の legacy と同じ扱い)。単一指定でスキップは 0。使用法の誤り(SAFE_NAME/--all の同時指定、非対応 target)は 1。

### 17. テストの経路

- CLI 経由(Typer の CliRunner で `lr_split_cli.app` を呼ぶ)を受け入れ基準の主経路にする。`dispatch()` の分岐は `sys.argv` を差し替える既存 test_cli.py の方式で 1 件確認する。
- 「既存パッケージ」は、フェイク分離器で build_package した後に sidecar から `pan_split` を削り、キャッシュの `pan_split:guitar.meta.json` と L/R を消すヘルパーで作る(本変更前の版が作った状態と同じ)。

### 18. README

「できあがるファイル」表・ツリーに L/R を加え、「L/R に分かれていない曲では作られません」を添える。CLI の節に「既存パッケージに L/R を追加する」小節を足し、`bunri lr-split SAFE_NAME` と `bunri lr-split --all` の例、キャッシュが必要なこと(無い場合は元の音源から再生成)、記録済みはスキップされ `--force` でやり直せること、Web の分離実行中は待つこと、分離の実行と重なって L/R が古い stem のものになった場合は `bunri lr-split --force` で作り直せることを書く。

## タスク分解

| # | タスク | 依存 |
|---|---|---|
| 1 | `registry.py`: `TargetSpec` に `pan_split: bool` を追加(guitar のみ True、他は False)。docstring 更新 | - |
| 2 | `pan_split.py` 新設: torch STFT/ISTFT(`pad_mode="constant"`、`center=True`、`length=n`)、`decide`(方針 9 の 1〜8。山は正の高さのみ、同値は最小添字)、`split_stem`、`PanSplitDecision`(json との往復を含む)。定数をモジュール定数に | - |
| 3 | `package_metadata.py`: `TargetMetadata.pan_split: str \| None = None`、`_validate` の検証、`_payload` の書き戻し(None は省略)、`complete_target` に `pan_split` 引数、`set_target_pan_split` と `TargetNotFoundError(ValueError)` の追加、`_locked_update` で `begin_target` / `complete_target` / `set_target_pan_split` を `out/.cache/metadata.lock`(timeout 10 秒)の下に置く | 5 |
| 4 | `cache.py`: `stage_completed(cache_dir, stage_name, outputs)` 追加 | - |
| 5 | `lock.py` 新設(`ProcessLock`: `timeout` と `busy_error` 付き、`ProcessLockBusy`)、`pocket/lock.py` を委譲に変更(API・文面・ロック位置は不変) | - |
| 6 | `package.py`: `_pan_split_step` / `_export_pan_split` / `_player_refs` / `_write_player` に切り出し、build_package をこれらで書き直す。`"single"` 時の旧 L/R 除去、`complete_target` への受け渡し | 1, 2, 3 |
| 7 | `package.py`: `add_pan_split` と `PanSplitOutcome`(方針 13 の前提確認、方針 14 の書き込み順、`TargetNotFoundError` から `failed` への写し) | 3, 4, 6 |
| 8 | `lr_split_cli.py` 新設(引数、ロック、進捗、集計、終了コード)と `cli.py` の dispatch 分岐(`lr-split`) | 5, 7 |
| 9 | `player.py` / `player.html.j2`: 引数追加、ボタン・理由文・注意書き・キー 4/5・ヘルプ | - |
| 10 | `local_package.py`: `_metadata_issues` の検証、`TargetArtifactInspection` に `pan_split`/`left_files`/`right_files`、`inspect_package_artifacts` の検査(`complete_formats`・`issues` は不変) | 3 |
| 11 | `web/app.py`: `_serialize_song` のダウンロード欄に L/R 行を追加。Web 画面側(テンプレート/JS)が downloads 配列を汎用に描画しているか確認し、トラック固定ならその修正も行う | 10 |
| 12 | 単体テスト `tests/test_pan_split.py`: 有限長 STFT の合成音(L に 220 Hz、R に 440 Hz、同振幅、2 秒、8 kHz で `"left_right"`・境界と比率は範囲で確認 / 同一信号を両チャンネルで `"single"` / L と同じ波形を R に振幅 0.1 で `"single"` / 無音・モノラル・サンプル数 0 で `"single"`)、`decide` へ直接渡す理想化配列(角度 0 と π/2 で谷 2・比率 約 0.45 / 区分 22 だけで `"single"` / 区分 10 と 30 で谷 12 / 区分 0 と 8 で谷 2 / 区分 9〜11 と 30 の重み比 0.99:0.01 で閾値により `"single"`、1:1 で `"left_right"` / ゼロ区分は山にならない)、`左 + 右 ≈ 元 stem`、長さ 400・2048・2049・4095・4096・4097・16000 の復元、400 サンプルの定位を振った stem で `"left_right"` | 2 |
| 13 | 統合テスト `tests/test_package.py`: 既存フェイク(400 サンプル、両チャンネル同値)で例外なく L/R が作られず sidecar が `"single"`、プレイヤーに理由文。定位を振った stem(2 秒、8 kHz、L に 220 Hz、R に 440 Hz)を書くフェイクで `.left/.right` の wav/mp3・sidecar `"left_right"`・プレイヤー参照、`mp3=False` で wav のみ、再実行で pan_split 段が cached、フィールド無し sidecar の再実行で L/R が追加されること。既存テストの `[cache_dir] = (out_dir / ".cache").iterdir()` を `.cache` 直下のディレクトリだけを選ぶ形に修正する。身元ファイルの target 項目を辞書の完全一致で比較している既存テスト(`sidecar["targets"] == [...]` の箇所など)は、guitar の項目に判定結果の `"pan_split"` を加える更新だけを行う(他の target の項目と他のキーは変えない)(方針 15) | 6 |
| 14 | CLI 統合テスト `tests/test_lr_split_cli.py`: 方針 17 の既存パッケージ化ヘルパーを用いて、L/R 追加・single 記録・キャッシュ欠如・stem 不一致・legacy・invalid・記録済みスキップと `--force`・`--all` の集計と終了コード・引数の排他・非対応 target・開始時に guitar の項目が無い(他 target だけ、または再生成中を模擬)ときのスキップと終了コード 0・方針 15 の再分離との交差(開始後の項目消失で失敗と終了コード 1)・二重起動でロック busy・dispatch の分岐 | 8 |
| 15 | `tests/test_package_metadata.py`・`tests/test_local_package.py`・`tests/test_cache.py`・`tests/test_lock.py`(新設可): フィールドの往復、不正値の拒否、旧形式の受理、`set_target_pan_split` の動作と target 不在時の `TargetNotFoundError`、方針 15 の sidecar 交差実行テスト(両方向)、ロックファイルが `out/.cache/metadata.lock` に作られること、`ProcessLock` の `timeout` の挙動、`stage_completed`、`inspect_package_artifacts` の L/R 検査と `complete_formats` 不変 | 3, 4, 5, 10 |
| 16 | `tests/test_player_html.py`: 構造テスト(ボタン有無・disabled・理由文・キーのヘルプ)と Playwright(5 トラックで `switchTrack("left")` が切り替わり drift が小さい、single 時は `tracks.left.available` が false でボタンが disabled、キー 4/5) | 9 |
| 17 | `tests/test_web_api.py`・`tests/test_web_jobs.py`: L/R ありパッケージで downloads に 4 行、single で 2 行、status/missing_files が L/R に左右されないこと。`add_pan_split` 実行後の曲一覧に L/R が現れること | 7, 11 |
| 18 | `tests/test_pocket_local.py`・`tests/test_pocket_sync.py`: L/R ファイルと `pan_split` フィールドがあっても preflight の資産が 3 つで manifest が変わらないこと。`SyncLock` の既存テストが無変更で通ること | 3, 5 |
| 19 | README: 「できあがるファイル」表に 2 行追加、ツリー更新、注記、「既存パッケージに L/R を追加する」小節(`bunri lr-split`)。CHANGELOG があれば追記 | 8 |
| 20 | `uv run pytest -q -n auto` と Makefile の lint 系ターゲットを実行し結果を報告。CI(.github/workflows/ci.yml)は既存 workflow に委ねる | 12〜18 |

## 完了条件・受け入れ基準

- [ ] `decide` 単体(有限長 STFT): L に 220 Hz、R に 440 Hz(同振幅)、2 秒、8 kHz のステレオを方針 7 の STFT に通した角度・energy で `"left_right"`、境界の区分が 5 以上 39 以下(実測 20)、左側の音量比が 0.45〜0.55(実測 約 0.500)。第 1 の山の添字と境界の厳密な位置は固定しない(実測では平滑化後の全 45 区分が正で、第 1 の山は 43)
- [ ] `decide` 単体(有限長 STFT): 同一信号を両チャンネル(中央)で `"single"`(全点が区分 22、正の区分は 21〜23 だけで候補が無い)。L と同じ波形を R に振幅 0.1 で置いた合成音で `"single"`(全点が区分 2、正の区分は 1〜3 だけ)。無音・モノラル・サンプル数 0 で `"single"` かつ例外なし
- [ ] `decide` 単体(理想化: 角度・energy の配列を直接渡す): 角度 0 と π/2 に同じ総重みで `"left_right"`、谷は 2(第 1 の山 0 に隣接する最初のゼロ区分)、左側比率 0.40〜0.50(約 0.45)。区分 22 の中心角だけに重みで `"single"`。区分 10 と 30 の中心角に同じ重みで `"left_right"` かつ谷は 12(11 より大きく 29 より小さい)。区分 0 と 8 の中心角に同じ重みで谷が 2(最初の最小添字)。区分 9・10・11 の中心角に 0.33 ずつと区分 30 の中心角に 0.01 で、山は 2 つ・谷は 13 だが左側比率が約 0.91 なので閾値 0.10 により `"single"`。同じ配置で区分 30 の重みを 1.0 にすると左側比率 約 0.457(第 1 の山は 29、谷は 13)で `"left_right"`
- [ ] `split_stem` 単体: `left + right` と元 stem の RMS 相対誤差が 1e-3 未満(PCM16 量子化を含む)。出力は元と同じサンプル数・サンプルレート・2 チャンネル。長さ 400・2048・2049・4095・4096・4097・16000 のすべてで例外なく同じ結果
- [ ] `split_stem` 単体: 400 サンプルの定位を振った stem(L に 220 Hz、R に 440 Hz、8 kHz)で `"left_right"` になり、L/R が書かれ、`left + right` が元 stem と一致する。境界の位置と比率は固定しない
- [ ] `build_package` 経由(フェイク分離器): 分割ありで `<safe>.guitar.left.{wav,mp3}` / `.right.{wav,mp3}` が非空、sidecar の guitar 項目に `"pan_split": "left_right"`、プレイヤー HTML に両ファイル名がある
- [ ] `build_package` 経由: 既存フェイク(400 サンプル、両チャンネル同値)で例外なく L/R ファイルが無く、sidecar が `"single"`、プレイヤーに理由文「L/R に分かれていない曲です」と disabled の L/R ボタンがある。既存テストは、`.cache` 直下を 1 要素と仮定する箇所(tests/test_package.py の `[cache_dir] = ...iterdir()`)をディレクトリだけ選ぶ形に直す修正と、身元ファイルの target 項目を辞書の完全一致で比較している既存テスト(tests/test_package.py の `sidecar["targets"] == [...]` の箇所など)に新しいフィールド `pan_split` を期待値へ加える更新(guitar の項目に判定結果の `"pan_split"` を加える。他の target の項目と他のキーは変えない)を除き、無変更で通る
- [ ] `build_package` 経由: `mp3=False` で L/R は wav のみ、プレイヤーは wav を参照。2 回目の実行で「pan_split: cached」となり L/R が同一内容。フィールド無し sidecar の既存パッケージを再実行すると separate は cached のまま L/R とフィールドが追加される
- [ ] `build_package` 経由: `--target bass`(フェイク)で L/R ファイルもフィールドも作られず、プレイヤーに L/R ボタンが無い
- [ ] CLI 経由(`bunri lr-split SAFE_NAME`): フィールド無しの既存パッケージ(定位を振った stem)に L/R の wav/mp3・sidecar `"left_right"`・プレイヤー参照が追加され、終了コード 0。他 target の項目と title・source は不変。元の入力音源はテスト中に削除しておく
- [ ] CLI 経由: 中央定位の既存パッケージで sidecar が `"single"`、L/R ファイル無し、プレイヤーに理由文、終了コード 0
- [ ] CLI 経由: formats が wav のみのパッケージで L/R は wav のみ、プレイヤーは wav を参照
- [ ] CLI 経由: `out/.cache/<key>` が無い場合、終了コード 1、標準出力に「キャッシュがありません」と再生成の案内、sidecar とパッケージのファイルが不変
- [ ] CLI 経由: キャッシュに `<target>.wav` またはメタが無い場合、終了コード 1、「分離済み stem がキャッシュにありません」、sidecar 不変
- [ ] CLI 経由: パッケージの `<safe>.guitar.wav` をキャッシュと異なる内容に書き換えた場合、終了コード 1、「一致しません」、L/R は作られない。パッケージ側 wav を削除した場合は成功する
- [ ] CLI 経由: 身元ファイル無しのパッケージは終了コード 1 で「再生成が必要」の案内。壊れた身元ファイルは終了コード 1 で issue 表示
- [ ] CLI 経由: 記録済み(`"left_right"` / `"single"` のいずれも)のパッケージはスキップ表示で終了コード 0、ファイルの mtime が変わらない。`--force` で再計算され、事前に消した `.left.wav` が復元される
- [ ] CLI 経由(`--all`): 完了 1・スキップ 1・失敗 1・legacy 1 を含む出力先で 4 行の結果と集計行が出て終了コード 1。失敗が無ければ legacy があっても終了コード 0。処理順が `all_package_names` の順
- [ ] CLI 経由: SAFE_NAME と `--all` の同時指定・両方無し、`--target bass` はいずれも終了コード 1 で説明文が出る
- [ ] CLI 経由: `out/.cache/pan_split.lock` を別プロセス(またはテスト内で `ProcessLock` を先に取得)で保持中は「別の lr-split が実行中」で終了コード 1
- [ ] CLI 経由: 開始時点で sidecar に guitar の項目が無いパッケージ(他 target だけのパッケージ、および `begin_target(guitar)` 後で Web が再生成中の状態の両方)に `bunri lr-split SAFE_NAME` を実行すると `スキップ: <safe>: guitar の項目がありません(guitar のパッケージでないか、分離ジョブの実行中)` で終了コード 0、sidecar・ファイル・プレイヤーは不変
- [ ] CLI 経由(交差、開始後の項目消失): `_pan_split_step` の後に `begin_target(guitar)` を挟むと終了コード 1、「guitar の項目が消えました」と完了後の再実行案内、sidecar は `begin_target` 後の内容のまま(L/R ファイル・一時ファイルの有無は問わない)
- [ ] CLI 経由(dispatch): `bunri lr-split ...` が `dispatch()` で新 app に振り分けられ、`prog_name` が `bunri lr-split`
- [ ] 関数単位 `package_metadata`: `pan_split` の往復保存、不正値で `ValueError`、フィールド無しの読み込み成功。`begin_target` / `complete_target` で他 target の `pan_split` が保持される。`set_target_pan_split` は該当 target のフィールドだけを変え、target 不在で `TargetNotFoundError`(`ValueError` の派生)。3 関数の実行後に `out/.cache/metadata.lock` が通常ファイルとして存在する
- [ ] 関数単位 `package_metadata`(交差実行): スレッド `web` の `complete_target(bass)` を読み込み直後に止めた状態で、スレッド `cli` の `set_target_pan_split(guitar)` が 0.5 秒後も完了しておらず sidecar が未変更。再開後の sidecar に bass 項目と guitar の `"left_right"` が両方ある。役割を入れ替えても同じ
- [ ] 関数単位 `package_metadata`: ロックを別の open で保持したまま `set_target_pan_split` を `timeout` 0.2 秒相当で呼ぶと文面「身元ファイルの更新待ちがタイムアウトしました」で失敗し、sidecar が未変更(timeout は monkeypatch で短縮してよい)
- [ ] 関数単位 `lock.ProcessLock`: `timeout=None` で保持中は即 busy。`timeout=0.2` で保持中は 0.2 秒以上待ってから busy。保持が解放されれば `timeout` 内に取得できる。ロックファイルがシンボリックリンクなら `OSError`
- [ ] 関数単位 `cache.stage_completed`: メタと出力が実ファイルなら True。メタ無し・出力がシンボリックリンク・JSON 不正で False。params が違っても True
- [ ] 関数単位: `pocket/lock.py` の既存テストが無変更で通り、`SyncLock` のロックファイルは `out/.pocket/sync.lock` のまま
- [ ] 関数単位 `local_package`: `"left_right"` で L/R が欠けても `complete_formats` と `issues` が変わらない
- [ ] Web API: L/R ありで downloads が target/backing/left/right の 4 行、single で 2 行。status・missing_files は L/R の有無で変わらない。`add_pan_split` を実行した後の曲一覧に L/R の 2 行が現れる
- [ ] 関数単位 Pocket preflight: L/R ファイルと `pan_split` があっても資産は original/target/backing の 3 つ
- [ ] Playwright: 5 トラックの player で `switchTrack("left")` 後に `activeTrack === "left"` かつ `lastSwitchDrift` が既存テストと同じ閾値内。キー 4/5 で切替。single の player で `tracks.left.available === false`
- [ ] README の表・ツリーが新ファイルを含み、既存パッケージへの追加手順(`bunri lr-split`)が載っている
- [ ] `uv run pytest -q -n auto` が全件通過。Makefile の lint 系ターゲットが通過(存在する場合)。CI の既存 workflow(.github/workflows/ci.yml)が PR 作成後に緑
- [ ] `pyproject.toml` の dependencies に scipy を追加していない。`pan_split.py` の `torch.stft` 呼び出しが `pad_mode="constant"` を明示している

## 未確定事項・リスクと判断の委ね方

| 項目 | 内容 | 実装時の扱い |
|---|---|---|
| 未提示ファイル | `src/bunri/separate.py`(stem の wav サブタイプ、stem の配置方法)、Web 画面のテンプレート/JS(downloads の描画方式)、`web/jobs.py`(実行中ジョブの列挙 API の有無、scan_local_packages の呼び出し頻度)、`tests/test_cli.py`(dispatch のテスト方式)、`tests/test_pocket_sync.py`(SyncLock のテスト範囲)、Makefile の lint ターゲット、CHANGELOG の有無 | 実装担当が参照して方針に沿って判断。stem のサブタイプが PCM16 でなければ L/R も stem に合わせる |
| 実行中 Web ジョブの検出 | `web/jobs.py` に digest ごとの queued/running ジョブを列挙する安価な関数があれば、そのパッケージをスキップに回す。無ければ検出は作らず README の案内と方針 15 のロックによる直列化に委ねる | 新しいジョブ記録読み取りを実装しない。既存関数の利用だけ許可 |
| torch と scipy の判定差 | 端処理は scipy の既定 boundary='zeros' と同じ constant にそろえた。残る差は scipy の `padded=True` による末尾の半端フレームと浮動小数点で、境界が ±1 区分ずれ、実曲で判定が変わる可能性 | 合成音テストで契約を固定。基準実装で確認した実曲での同一判定は利用者側で確認し、差が出たら止まって報告(代替: scipy を直接依存に追加) |
| 平滑化後にゼロ区分がある実曲 | 「高さ 0 を山にしない」規則は、ある角度帯にエネルギーが厳密に 0 の stem でだけ基準実装と結果が変わりうる。分離モデルの出力では実質起きず、起きても片側だけに音がある stem で `"single"` が本来の答え | 実曲で差が出た場合は止まって報告 |
| 有限長 STFT の合成音の実測値 | 220/440 Hz 同振幅の実測(第 1 の山 43、谷 20、比率 約 0.500)は基準実装での値で、torch 実装では末尾フレームと浮動小数点で ±1 区分ずれうる。受け入れ条件は範囲指定にした。R を別周波数(440 Hz)で振幅 0.1 にした場合の判定は実測が無いため受け入れ条件に入れない | 範囲を外れたら止まって報告。別周波数の振幅 0.1 は実装担当が実測して `"single"` なら任意にテストを足してよい |
| `.cache` 直下のロックファイル | `metadata.lock` と `pan_split.lock` を `out/.cache` 直下に置く。`.cache` 直下を列挙してキャッシュ鍵ディレクトリとして扱う処理があれば通常ファイルを無視する必要がある。既存テストの `[cache_dir] = (out_dir / ".cache").iterdir()` は複数要素になるためディレクトリだけ選ぶ形に直す(方針 15)。`.cache` を手動で消すと保持中のロックが無効になる | 実装担当が列挙処理を確認し、通常ファイルを無視していなければ直す。テスト修正は同種の箇所に限る。手動削除中の実行は対象外 |
| 曲削除との交差 | delete_song がパッケージを消している最中に sidecar を書き戻すと、削除側が「変更された」で失敗するか sidecar が消える。build_package と同じ既存のリスク | 本計画では扱わない。ロックの対象にしない |
| 同じ曲の再分離との重なり | 同じ曲の同じ target を別プロセスが作り直し、その完了が `bunri lr-split` の 1 曲の処理の途中に重なると、旧 stem から作った L/R・プレイヤーが新 stem と組み合わさりうる。1 人の利用者が自分の PC で使う前提では、端末で同じ曲を作り直しながら同時に実行した場合に限られる | 仕組みでは防がない。README で分離の実行中は待つよう案内し、重なった場合は `--force` で作り直せると書く |
| 身元ファイルロックの待ち時間 | 10 秒固定。Web の分離ジョブと重なっても区間はミリ秒なので通常は待たない | 超過が観測されたら報告。値の変更は実装担当が判断してよい |
| `_download_files(job)` | ジョブ記録経由の直列化に L/R を出すには sidecar 読み取りが要る | Web 画面が曲一覧(artifact 経路)だけで downloads を描画していれば変更しない。job 経路も使うなら sidecar を読んで追加 |
| キャッシュ識別ファイルが無いキャッシュ | `ensure_input_identity` が新規作成する(build_package と同じ挙動) | そのまま。作成を避けたい判断が出たら読み取り専用の照合関数を追加 |
| stem の整合確認のコスト | 5 分の stem(約 50 MB)を 2 本 sha256 する。`--all` でパッケージ数分かかる | 許容とする。サイズ不一致で先に打ち切る。遅すぎると判明したら報告 |
| ダウングレード時のフィールド消失 | 旧版 Bunri が別 target を追加ビルドすると `pan_split` が落ちる | 既知の制限として README には書かず、コードコメントに残す。新コマンドを再実行すれば復元できる |
| 長尺曲のメモリ | 20 分超の入力で 4 GB 前後に達し得る | チャンク処理は入れない。実装で float32 維持と逐次解放を守る。問題が出たら報告 |
| ラベルの空白 | 「L のみ」の空白がダウンロードファイル名に入る(`{title}_L のみ.mp3`) | 既存規則のまま。問題があればラベルからの空白除去のみ実装担当が判断してよい |
| 既存 Web テストの期待値 | downloads の行数を固定しているテストがあれば更新が要る | 実装担当が更新。行数以外の期待値を変える必要が出たら報告 |

## 公開経路

作業ブランチ plan/2026-09-26-guitar-lr-split に計画書と実装を commit する。push・PR 作成・main への統合・リリース(バージョン更新)は本計画の範囲外で、別途承認を得てから行う。CI は PR 作成後に .github/workflows/ci.yml で実行される。
