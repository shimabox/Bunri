# Bunri Pocket への L のみ / R のみ送信 実装計画

- 日付: 2026-09-26
- リポジトリ: github.com/shimabox/Bunri
- ベースブランチ: main
- ベース SHA: 1ac02e56ea9505eef13cfa47a5606475679d6d97(Bunri v0.6.0 リリースコミット)
- 作業ブランチ: plan/2026-09-26-pocket-lr-send
- 起案担当: 予定: claude-fable-5-1 / high。確認済み: 実モデル claude-fable-5-1(確認源: CLI 結果 JSON の modelUsage)。effort は未確認
- 計画レビュー担当: 予定: gpt-6-astra / high(未実行)
- 編成の例外理由: なし
- 実装担当: claude-opus-5-5 / high(通常段)。protocol 層・preflight・sync・service・CLI・Web ジョブを横断し、契約の細部(schema_version の昇格、再送の差分検出、未対応時の完全互換)を同時に満たす必要があるため
- 実装レビュー担当: 予定: gpt-6-sol / high
- 計画書の扱い: レビュー・privacy 検査後に作業ブランチへ commit(配置先: docs/plans/2026-09-26-pocket-lr-send/plan.md と docs/plans/2026-09-26-pocket-lr-send/request.md)
- 公開範囲: 作業ブランチへの commit まで。push・PR・リリースは別途承認。Bunri のバージョン更新・タグ・Release の作成は本計画の範囲外(本計画の検収後に別途)

## 確定済みの仕様

次の事項は確定済みで、実装時に変えない。

1. 身元ファイルが `left_right` なのに L/R の mp3 が欠けている曲は、他の mp3 と同じく preflight で停止する。`pan_split: "left_right"` と 2 stem で送る案は採らない。
2. L/R を送らない除外オプション(`--lr/--no-lr` など)は設けない。
3. 未対応の音源ポケットへの案内は、Web では同期ジョブの結果表示にだけ 1 行出す。状態一覧(ポケットにある / 未同期 / 差分あり)の表示項目は増やさない。
4. 未対応の案内は、音源ポケットが未対応なら、同期したパッケージの内容(`left_right` の有無)に関わらず常に出す。
5. README とリリースノートには、「L のみ / R のみを送れるのは音源ポケット 0.3.0 以降に接続しているとき」という趣旨をはっきり書く。

## 背景・目的

Bunri v0.6.0 は、ギターの定位が左右 2 か所に分かれた曲で「L のみ / R のみ」(`<safe>.guitar.left.mp3` / `.right.mp3`)を練習パッケージに作り、身元ファイル `.bunri-package.json` の target 項目に `pan_split`(`"left_right"` / `"single"`)を記録する。しかし `bunri pocket sync` と Web UI の同期は L/R を音源ポケット(bunri-pocket)へ送らない。

関連リポジトリ github.com/shimabox/bunri-pocket の v0.3.0 は Release 済みで、タグ v0.3.0 は main の 6766d7d を指す。v0.3.0 は manifest の instrument に `pan_split` と stem role `left` / `right` を受け入れ、`<target>.left.mp3` / `<target>.right.mp3` の保存・配信・PWA の L/R 切替に対応した。本計画では Bunri の同期サービス層を拡張し、L/R 対応の音源ポケットに接続しているときだけ L/R を送るようにする。未対応の音源ポケット(v0.2.x)へは従来とまったく同じ 3 ファイルを送り、成功終了のうえで 1 行案内する。CLI の sync と Web UI の同期は同じサービス層を通るため、両方で同じ動きになる。

Bunri のリリース(バージョン更新・タグ・Release の作成)は本計画の範囲外で、本計画の検収後に別途行う。

## スコープ

- 対象:
  - `src/bunri/pocket/protocol.py`: `validate_manifest` の L/R 対応、`pan_split_supported()` の追加、`merge_manifest` の `pan_split` / `left` / `right` の merge と `schema_version` の `"1.1"` 昇格
  - `src/bunri/pocket/config.py`: `validate_capabilities` が `schemas.manifest.latest` を `"1.0"` に固定している点の緩和(`1.x` を受け入れる)。これがないと `bunri pocket connect` が v0.3.0 の音源ポケットに接続できない
  - `src/bunri/pocket/local.py`: preflight が `pan_split == "left_right"` の target について L/R mp3 を asset に加える。欠損時の扱い
  - `src/bunri/pocket/sync.py`: capabilities による対応判定、L/R asset の送信・除外、`SyncResult` への判定結果の追加
  - `src/bunri/pocket/service.py`: `sync_all` で capabilities を 1 回取得、`BatchResult` への判定結果の追加、`inspect_remote` / `inspect_packages` の差分判定に判定結果を反映
  - `src/bunri/pocket/cli.py`: 未対応ポケットへの 1 行案内
  - `src/bunri/web/jobs.py` と Web UI の同期結果表示: 判定結果を結果 JSON に含め、未対応の 1 行案内を表示
  - `tests/fixtures/bunri_pocket_protocol_v1/`: bunri-pocket 6766d7d のスナップショットへ更新
  - `README.md`: L/R と Pocket の記述の修正、混在環境の案内
  - 上記の単体・経路テスト
- 対象外:
  - 音源ポケット(bunri-pocket)側のコード変更
  - Bunri のバージョン更新・タグ・リリースノートの発行(文言案だけ本計画に残す)
  - L/R を送らない除外オプションの新設(設けないことで確定済み)
  - library スキーマ・曲一覧への L/R 表示
  - ギター以外の target の L/R(Bunri の `TargetSpec.pan_split` が True なのは guitar のみという前提)
  - `bunri pocket connect` の出力に L/R 対応の有無を表示すること(将来の改善候補)

## 方針

### 契約の要点(bunri-pocket 側で確定済み。正本は bunri-pocket の docs/protocol-v1.md と、bunri-pocket の docs/plans/2026-09-26-pocket-lr-tracks/plan.md の「後続計画への引き継ぎ(Bunri 側の契約)」節)

1. sync の開始時(最初の manifest / library 取得の前)に `GET /api/v1/upload/capabilities` を 1 回呼ぶ。`api.major === 1` かつ `features.pan_split === true` のときだけ L/R 対応。`features` が無い・`true` でない・応答が object でない場合は未対応
2. 未対応ポケットへは v0.6.0 とまったく同じ動作(3 ファイル、`pan_split` なし、2 stem、`schema_version` は remote を維持)。終了コードは成功。標準出力に案内を 1 行出す
3. manifest の instrument に任意の `pan_split`、stem role に `left` / `right`(path は `<target>.left.mp3` / `<target>.right.mp3`)。left と right は対で、`pan_split` が `left_right` のときだけ。stems の並びは target、backing、left、right。新フィールドを書く manifest の `schema_version` は `"1.1"`
4. `single` は `pan_split: "single"` と 2 stem。L/R ファイルは送らない
5. `left_right` は `pan_split: "left_right"` と 4 stem。L/R の mp3 も HEAD で sha256 と bytes を比べ、違うものだけ PUT。順序は audio → manifest → library
6. 身元ファイルに `pan_split` が無い target は `pan_split` を書かず 2 stem
7. 未対応ポケットへ同期済みの曲を、ポケット更新後に再同期すると、L/R だけ PUT され、manifest の merge で差分が検出されて manifest と library が更新される
8. song_id、digest 一致の停止条件、DIGEST_COLLISION の扱いは変えない
9. CLI と Web は同じサービス層で同じ動き

### 対応判定と判定結果の受け渡し

- `protocol.py` に純関数 `pan_split_supported(capabilities: object) -> bool` を追加する。判定は契約 1 のとおり。`api.major` は `int` かつ `bool` でないこと、`features.pan_split` は `is True` で比べる(`"true"` などの文字列は未対応)
- `sync.py` に `RemoteFeatures(pan_split: bool)` と `probe_features(client) -> RemoteFeatures` を追加する。`probe_features` は既存の `PocketHTTPClient.capabilities()` を 1 回呼び、結果を `pan_split_supported` に渡す
- 判定できない失敗の扱い: capabilities が HTTP エラー(401、404、5xx など)や JSON として解釈不能なときは `PocketHTTPError` をそのまま送出し、同期を開始しない(fail closed)。v0.2.x の音源ポケットも capabilities を返すので、未対応ポケットへのフォールバックとは区別できる。契約の「応答が object でない」は、JSON として解釈できたが object でない場合だけを未対応として扱う
- `synchronize(package, client, *, include_original=True, clock=None, features: RemoteFeatures | None = None)`: `features` が None のときは `probe_features` を自分で呼ぶ(manifest / library 取得より前)。呼び出し元が渡したときは呼ばない。`sync_one` は None のまま(結果として sync 1 回につき 1 回)。`sync_all` は inventory の後、ループの前に 1 回だけ `probe_features` を呼び、各曲へ渡す(一括で 1 回)。`inspect_packages` も 1 回だけ呼んで各曲の `inspect_remote` へ渡す
- `SyncResult` に `pan_split_supported: bool` を末尾に既定値付きで追加する(既存の位置引数の互換を保つ)。`BatchResult` に `pan_split_supported: bool | None = None` を追加し、probe 後に確定する
- `sync_all` の probe 失敗は各曲の `try` の外で起こすため、`PocketServiceError` や設定エラーと同じ経路で呼び出し元へ伝わる(CLI は `safe_error`、Web は既存のジョブ失敗経路)
- `inspect_packages` の probe(`probe_features`)が失敗した場合(`OSError`、`PocketHTTPError`、`ProtocolError`、`ValueError` など、既存の `inspect_remote` が捕捉して `unknown` にしているのと同じ例外)は、例外を呼び出し元へ伝えない。ローカルで判定できる状態(パッケージ名の競合、legacy、preflight の失敗など、現在 `inspect_packages` がネットワーク前に決めている状態)はそのまま返し、音源ポケットに問い合わせる必要のある曲だけを `RemoteStatus("unknown", False, "音源ポケットの状態を確認できません。")`(既存の `inspect_remote` の失敗時と同じ値)にする。各曲の manifest / library 取得・media HEAD は行わない。理由: 状態 API(Web の `/api/pocket/status`。`inspect_packages` を例外処理なしで呼んでいる)が 500 にならず、従来どおり曲ごとの状態を返すため
- 状態確認と同期で probe 失敗の扱いが違う: 状態確認(`inspect_packages`)は上記のとおり曲ごとの `unknown` にして例外を送出しない。同期(`synchronize` / `sync_all`)は fail closed のまま(例外を送出し、何も送らない)

### preflight(ローカル)

- `preflight` は身元ファイルの target ごとに、`pan_split == "left_right"` のとき `(f"{safe_name}.{target}.left.mp3", target, "left")` と `right` を requested に加え、remote 名を `<target>.left.mp3` / `<target>.right.mp3` とした `AssetInfo(role="left"/"right")` を assets に含める。`single` と None は従来どおり 2 ファイル
- 身元ファイルが `left_right` なのに L/R の mp3 が欠けている場合は、他の mp3 と同じく preflight で停止する(確定済みの仕様 1)。issue 文言は既存の `inspect_artifact` の issue に加えて、`--force` を含む作り直しの案内を付ける。身元ファイルに判定が記録済みのため、`--force` が無いと `bunri lr-split` はその曲をスキップするからである。例: 「身元ファイルには L のみ / R のみありと記録されていますが、L/R の mp3 がありません。`bunri lr-split '<パッケージ名>' --force -o <出力先>` で作り直してください」。文言の細部は実装担当が決めてよいが、`--force` を含むことは必須。`kind` は "general" のまま
- preflight は音源ポケットの対応可否を知らずに動く。L/R asset は常に `LocalPackage.assets` に含め、送信するかどうかは sync 側で role を見て絞る。`sync.py` に `assets_to_send(package, features) -> tuple[LocalAsset, ...]` を置き、未対応のときは role が `left` / `right` の asset を除く。`synchronize` の media ループと `merge_manifest` への `assets`、`inspect_remote` の `media_match` は必ずこの関数を通す

### `validate_manifest` の拡張

- instrument の `pan_split`: 省略可。存在すれば `"left_right"` か `"single"`。他は issue
- stems は 2 個または 4 個。`target` と `backing` を各 1 つ。4 個のときは `left` と `right` を各 1 つ。role の重複、3 個、5 個以上、片方だけの left / right は issue
- `left` / `right` があるとき `pan_split` が `"left_right"` でなければ issue(`pan_split` なし、`"single"` とも不可)
- `pan_split: "left_right"` で 2 stem は有効
- path は role ごとに正確一致: `<target>.left.mp3` / `<target>.right.mp3`
- 既存 fixture(2 stem、`pan_split` なし)と 4 stem fixture(上流の manifest-v1-pan-split.json)がともに通ること。上流 fixture には stem 内の未知フィールド、`single` の instrument、`left_right` で 2 stem の instrument が含まれる

### `merge_manifest` の拡張

- シグネチャに `pan_split_supported: bool = False` を追加する。False のときは v0.6.0 と同じ出力(既存呼び出しとテストの互換)。remote が `pan_split` を持っていれば未知フィールドとして維持し、stems は 2 個を書き、`schema_version` は remote を維持する
- True のとき、target ごとに:
  - 身元ファイルの `pan_split` が `"left_right"` または `"single"` なら instrument に `pan_split` を書く。None なら instrument から `pan_split` キーを取り除く(身元ファイルを正とする。remote の値を残すと、旧 Bunri で再生成したパッケージが「送られていない」状態を作り、再同期しても解消しない)
  - `left_right` かつ assets に `<target>.left.mp3` と `<target>.right.mp3` の両方があるときだけ 4 stem。それ以外は 2 stem。stems の順序は target、backing、left、right。left / right の旧 stem は role で引き継ぎ、他の stem と同じ `asset_value` で更新する
  - 結果に `pan_split` または `left` / `right` stem が 1 つでもあり、`schema_version` の minor が 1 未満なら `"1.1"` に上げる。minor が 1 以上なら維持する(下げない)。新規作成の既定は `"1.0"` のまま
- 差分判定と `updated_at` の更新は既存どおり `stable_json` の比較。契約 7 の再送はこの比較で自然に検出される

### `validate_capabilities`(connect 時)

- `schemas.manifest.latest` と `schemas.library.latest` の期待値を「`^1\.\d+$` に一致する文字列」に緩める。他の項目は据え置く。sync 時の対応判定にはこの関数を使わない(契約の 2 項目だけを見る)

### CLI

- `bunri pocket sync SAFE_NAME`: 成功出力の末尾に、`result.pan_split_supported` が False のとき次の 1 行を標準出力へ出す
  ```
  この音源ポケットは L のみ / R のみに未対応です。音源ポケットを更新して再同期すると送られます
  ```
- `bunri pocket sync --all`: `batch.pan_split_supported` が False のとき集計行の後に同じ 1 行を 1 回出す。終了コードは従来どおり(未対応は失敗にしない)
- 案内は同期したパッケージに `left_right` があるかどうかに関わらず、未対応ならいつも出す(確定済みの仕様 4。契約 2 の文面どおり)

### Web

- `pocket_single` ジョブの `job.result` は `asdict(SyncResult)` なので、`pan_split_supported` が自動で含まれる。`pocket_all` は `_pocket_batch_result` に `pan_split_supported` を追加する
- Web UI の同期結果表示は、この値が False のとき CLI と同じ 1 行を表示する。状態一覧(ポケットにある / 未同期 / 差分あり)の表示項目は増やさない(確定済みの仕様 3)
- `inspect_remote` は `features` に応じて `merge_manifest(pan_split_supported=...)` と `assets_to_send` を使う。これにより、対応ポケットでは 3 ファイルで同期済みの曲が「差分あり」になって再同期を促し、未対応ポケットでは「ポケットにある」のままになる

### fixture スナップショットの更新

- 出典 commit を bunri-pocket `6766d7d` にし、`schemas/manifest-v1.schema.json` を上流の同 commit の内容で置き換え、`valid/manifest-v1-pan-split.json` を追加する。`UPSTREAM.md` の Commit(完全 SHA `6766d7dd95d66023b8a2828a044ee0d88756d509`)と Sources を更新する
- `stable/` の golden は入力(上流 `manifest-v1.json` / `library-v1.json` と隣接 `*.input.json`)が変わっていないことを確認して据え置く。上流の該当ファイルが a8efc3e から 6766d7d の間で変わっていれば、`UPSTREAM.md` の手順で再生成する
- 上流 6766d7d に L/R 関連の invalid fixture(片方だけの left、`pan_split` なしで left / right など)があれば同じディレクトリへ写し、`UPSTREAM.md` の Sources に加える。無ければ Bunri 側のテストで組み立てる

### README

- 出力ファイル節の「L/R は Bunri Pocket には送信されません。」を次に置き換える:「L/R は、音源ポケット(Bunri Pocket)0.3.0 以降に接続しているときだけ同期で送られます。それより前の音源ポケットには従来どおりの 3 ファイルだけを送り、同期の最後に更新を促す案内を表示します。」
- Pocket 節に段落を追加する:
  - 送る内容: original(`--no-original` で除外可)、`<target>.mp3`、`<target>.backing.mp3`、L/R がある曲では `<target>.left.mp3` / `<target>.right.mp3`(音源ポケット 0.3.0 以降のみ)。L/R に分かれていない曲は「分かれていない」という判定だけを送る
  - 再送: 古い音源ポケットへ同期した曲は、音源ポケットを更新後に再同期すると L/R だけが追加で送られ、Web UI では「差分あり」と表示される
  - 混在環境: 「複数のマシンで Bunri を使い分けている場合、L のみ / R のみを同期した曲は v0.6.0 以前の Bunri から同期・状態確認できません(Bunri 側の検証で停止し、「Pocket の同期に失敗しました」と表示されます)。すべてのマシンの Bunri を更新してください。」
  - 切り戻し: 「L/R を同期した後に音源ポケットを 0.2.x へ戻すと、その曲は音源ポケットで開けず Bunri の同期も失敗します。音源ポケットを再度更新するか、Bunri から曲を削除して再同期してください。」(bunri-pocket の docs/protocol-v1.md と同じ内容)
- 同じ混在環境の文言をリリースノート案として本計画に残す(発行は範囲外。下記「リリースノート案」)

### リリースノート案(発行は範囲外)

Bunri のリリース時に転記する文言。上記 README 節の文言と同じ内容にする。

- L のみ / R のみは、音源ポケット(Bunri Pocket)0.3.0 以降に接続しているときだけ同期で送られます。0.2.x の音源ポケットには従来どおりの 3 ファイルだけを送り、同期の最後に更新を促す案内を表示します。
- 対応する音源ポケットの版: 0.3.0 以降で L/R を送信、0.2.x には従来の 3 ファイルのみ。
- 複数のマシンで Bunri を使い分けている場合、L のみ / R のみを同期した曲は v0.6.0 以前の Bunri から同期・状態確認できません(Bunri 側の検証で停止し、「Pocket の同期に失敗しました」と表示されます)。すべてのマシンの Bunri を更新してください。
- L/R を同期した後に音源ポケットを 0.2.x へ戻すと、その曲は音源ポケットで開けず Bunri の同期も失敗します。音源ポケットを再度更新するか、Bunri から曲を削除して再同期してください。

### 選ばなかった案

- preflight に音源ポケットの対応可否を渡して L/R を requested から外す案: preflight はネットワーク前に完了する設計であり、`inventory` / `resolve_package` の多数の呼び出し元へ判定結果を配る変更が大きい。asset を常に集めて sync 側で絞る方が小さく、ローカルの整合性検査も一貫する
- `synchronize` が常に自分で capabilities を呼ぶ案: `sync_all` で曲数分の呼び出しになり契約(一括で 1 回)に反する。`features` 引数の任意化で両立させる
- `validate_capabilities` を sync の判定に流用する案: connect 用の厳密検査で、判定の失敗経路が増えるだけになる。契約の 2 項目だけを見る純関数を別に置く
- L/R の mp3 が欠けた `left_right` の曲を `pan_split: "left_right"` と 2 stem で送る案: 音源ポケットは受理し PWA は「送られていない」と表示するが、PWA が「Bunri で再同期すると使えます」と案内するのに再同期しても解消せず、利用者が抜け出せない。preflight で止める案は未対応ポケットへの同期も L/R 欠損で止まる欠点があるが、修復手段(`bunri lr-split '<パッケージ名>' --force -o <出力先>`)が明確
- L/R を送らない除外オプション(`--lr/--no-lr`、manifest は `pan_split: "left_right"` と 2 stem)を設ける案: 同期済みの曲に使うと stem が消え media が R2 に残る既知の限界を利用者に負わせる。L/R は派生物で original のような秘匿性がなく、Web UI は常に全送信なので CLI にだけ設けると経路差が生まれる。要望が出た時点で別計画とする
- 未対応の案内を状態一覧のヘッダーなどにも出す案: 同期結果表示と情報が二重になる
- 未対応の案内を同期対象に `left_right` のパッケージがあるときだけ出す案: 常に出す方が実装が単純で、更新を促す目的に合う

## タスク分解

| # | タスク | 依存 |
|---|---|---|
| 1 | fixture スナップショット更新: `tests/fixtures/bunri_pocket_protocol_v1/schemas/manifest-v1.schema.json` の置換、`valid/manifest-v1-pan-split.json` の追加、`UPSTREAM.md` の Commit(完全 SHA `6766d7dd95d66023b8a2828a044ee0d88756d509`)/ Sources 更新、`stable/` golden の入力不変の確認。`tests/test_pocket_protocol.py` に「valid fixture が全件 `validate_manifest` を通る」ことを確認するテストが無ければ追加(この時点では失敗してよい) | - |
| 2 | `src/bunri/pocket/protocol.py`: `validate_manifest` の L/R 規則、`pan_split_supported()`、`merge_manifest(pan_split_supported=...)` と `schema_version` 昇格。`tests/test_pocket_protocol.py` に真理値表・valid / invalid・merge の各ケースを追加 | 1 |
| 3 | `src/bunri/pocket/config.py`: `validate_capabilities` の `latest` 緩和。`tests/test_pocket_config.py` を更新(`"1.1"` 受理、`"2.0"` / 非文字列 拒否) | - |
| 4 | `src/bunri/pocket/local.py`: preflight の L/R asset 追加と欠損時の issue 文言。`tests/test_pocket_local.py` / `tests/test_pocket_preflight_io.py` に left_right・single・None・欠損の各ケースを追加 | - |
| 5 | `src/bunri/pocket/sync.py`: `RemoteFeatures`、`probe_features`、`assets_to_send`、`synchronize(features=)`、`SyncResult.pan_split_supported`。`tests/test_pocket_sync.py` の `FakeClient` に `capabilities()` を追加し、既存 `test_manifest_does_not_carry_pan_split` を「未対応では持たない / 対応では持つ」に置き換える。再送・順序・冪等のテストを追加 | 2, 4 |
| 6 | `src/bunri/pocket/service.py`: `sync_all` の 1 回 probe と `BatchResult.pan_split_supported`、`inspect_remote(features=)`、`inspect_packages` の 1 回 probe と probe 失敗時の扱い(例外を送出せず、ローカルで判定できる状態はそのまま、その他は `unknown`)。`tests/test_pocket_service.py` に呼び出し回数と状態判定、`inspect_packages` の probe 失敗(capabilities 401)のテストを追加 | 5 |
| 7 | `src/bunri/pocket/cli.py`: 未対応時の 1 行案内(単曲・`--all`)。`tests/test_cli.py` または pocket CLI のテストに標準出力と終了コードの確認を追加 | 6 |
| 8 | `src/bunri/web/jobs.py` の `_pocket_batch_result` と Web UI の同期結果表示に `pan_split_supported` と案内 1 行を追加。`tests/test_web_pocket_jobs.py` / `tests/test_web_page.py` / `tests/test_web_api.py` を更新。状態 API(`/api/pocket/status`)が capabilities の失敗時にも 200 と曲ごとの `unknown` を返すテストを既存の Web テストの作り方に合わせて追加 | 6 |
| 9 | `README.md` の修正(出力ファイル節、Pocket 節の送信内容・再送・混在環境・切り戻し) | 2 |
| 10 | 全体確認: `uv run pytest -q -n auto`、リポジトリ既定の lint / 型検査(存在する場合)。既存テストの期待値(例: `tests/test_pocket_sync.py` の manifest 413 テストが期待する 741 バイト)が変わらないことを確認 | 1〜9 |

## 完了条件・受け入れ基準

経路ごとに、テストが通るべきレイヤーを併記する。HTTP はすべてフェイク(`tests/test_pocket_sync.py` の `FakeClient` 方式)を使う。

- [ ] 関数単位(`tests/test_pocket_protocol.py`): `pan_split_supported` が次を返す。`{"api":{"major":1},"features":{"pan_split":true}}` は True。`features` なし、`pan_split: false`、`pan_split: "true"`、`api.major: 2`、`api.major: true`、応答が list / 文字列 / None は False
- [ ] 関数単位: `validate_manifest` が上流 fixture `valid/manifest-v1-pan-split.json` を受理し、既存の valid fixture もすべて受理する。次を拒否する: left のみ、`pan_split` なしで left / right、`single` で left / right、3 stem、5 stem、role 重複、`pan_split: "both"`、`bass` の instrument に `guitar.left.mp3`
- [ ] 関数単位: `merge_manifest(pan_split_supported=False)` の出力が v0.6.0 と同じ(既存テストが無変更で通る)。remote に `pan_split` があっても維持し、`schema_version` を変えない
- [ ] 関数単位: `merge_manifest(pan_split_supported=True)` で、left_right + 4 asset → `pan_split: "left_right"`、4 stem(target、backing、left、right の順)、`schema_version: "1.1"`。single → `pan_split: "single"`、2 stem、`"1.1"`。None → `pan_split` なし、2 stem、`"1.0"`。remote が 3 ファイル同期済み(2 stem、`"1.0"`)で left_right + 4 asset → `changed=True`、`"1.1"`、`updated_at` 更新。remote が `"1.2"` なら維持。remote instrument に `pan_split` があり身元ファイルが None なら取り除く
- [ ] 関数単位(`tests/test_pocket_config.py`): `validate_capabilities` が `schemas.manifest.latest: "1.1"` と `"1.0"` を受理し、`"2.0"` と非文字列を拒否する
- [ ] 関数単位(`tests/test_pocket_local.py`): preflight が left_right で role left / right の 2 asset(remote 名 `guitar.left.mp3` / `guitar.right.mp3`)を追加し、single と None では追加しない。left_right で `.left.mp3` が無いと `LocalPreflightError` になり、issue に `bunri lr-split` と `--force` を含む案内が含まれる
- [ ] synchronize 経由(`tests/test_pocket_sync.py`): 未対応ポケット(`features` なし)へ left_right パッケージを同期すると、PUT_MEDIA は 3 件、manifest に `pan_split` と left / right が無く、`schema_version` が `"1.0"`、`result.pan_split_supported is False`、例外なし
- [ ] synchronize 経由: 対応ポケットへ left_right パッケージを同期すると PUT_MEDIA は 5 件、manifest は 4 stem・`"1.1"`、library が更新され、`result.pan_split_supported is True`。single では 3 件・`pan_split: "single"`
- [ ] synchronize 経由(再送): 未対応で同期 → capabilities を対応に切り替えて再同期 → PUT_MEDIA は `guitar.left.mp3` と `guitar.right.mp3` の 2 件だけ、`manifest_updated == 1`、`library_updated == 1`。その後の再実行は media 5 件 skipped、manifest / library skipped
- [ ] synchronize 経由(順序): `client.calls` で capabilities の呼び出しが最初の GET manifest より前に 1 回だけある。`features` を渡した場合は capabilities を呼ばない。DIGEST_COLLISION のテストは PUT_MEDIA なしのまま通る
- [ ] synchronize 経由(失敗): capabilities が 401 を返すと `PocketHTTPError` が送出され、GET manifest も PUT も行われない
- [ ] service 経由(`tests/test_pocket_service.py`): 3 パッケージの `sync_all` で capabilities の呼び出しが 1 回、`BatchResult.pan_split_supported` が設定される。`inspect_remote` は、remote が 3 ファイル同期済みの left_right パッケージについて、対応ポケットでは `"different"`(can_sync True)、未対応ポケットでは `"synced"` を返す。`inspect_packages` の capabilities 呼び出しが 1 回
- [ ] service 経由: `inspect_packages` で capabilities が 401 を返す場合、例外を送出せず、ローカルで競合・legacy と判定されるパッケージはその状態のまま、その他のパッケージは `unknown` と「音源ポケットの状態を確認できません。」になり、manifest / library / media への要求を行わない
- [ ] CLI 経由: 未対応ポケットへの `bunri pocket sync SAFE_NAME` と `--all` が終了コード 0 で、標準出力に「この音源ポケットは L のみ / R のみに未対応です。音源ポケットを更新して再同期すると送られます」を 1 回含む。対応ポケットでは含まない
- [ ] Web ジョブ経由(`tests/test_web_pocket_jobs.py`): `pocket_single` と `pocket_all` の `job.result` に `pan_split_supported` が含まれる。Web UI の同期結果表示テストで、False のとき案内 1 行が表示される
- [ ] Web 経由(`tests/test_web_api.py` など既存の Web テストの作り方に合わせる): 状態 API(`/api/pocket/status`)が capabilities の失敗時にも 200 を返し、曲ごとの状態が unknown になる
- [ ] fixture: `tests/fixtures/bunri_pocket_protocol_v1/UPSTREAM.md` の Commit が完全 SHA `6766d7dd95d66023b8a2828a044ee0d88756d509` で、Sources に `valid/manifest-v1-pan-split.json` が含まれる。`stable/` golden は入力不変の確認結果に従って据え置きまたは再生成
- [ ] README の「L/R は Bunri Pocket には送信されません」が残っていない。出力ファイル節と Pocket 節に「L のみ / R のみを送れるのは音源ポケット 0.3.0 以降に接続しているとき」という趣旨がはっきり書かれている。Pocket 節に送信内容・再送・混在環境・切り戻しの記述がある
- [ ] `uv run pytest -q -n auto` が全件通る。リポジトリ既定の lint / 型検査(存在する場合)が通る
- [ ] 計画書と差分に個人環境のパス・ユーザー名・私的リンクが含まれない(privacy 検査)

## 未確定事項・リスクと判断の委ね方

| 項目 | 内容 | 実装時の扱い |
|---|---|---|
| 1. left_right なのに L/R mp3 が欠けている | 確定: 他の mp3 欠損と同じく preflight で停止する(確定済みの仕様 1)。未対応ポケットへの同期も L/R 欠損で止まるが、修復手段は `bunri lr-split '<パッケージ名>' --force -o <出力先>`(身元ファイルに判定が記録済みのため `--force` が必要) | 判断待ちではない。方針どおり実装する |
| 2. L/R を送らない除外オプション | 確定: 設けない(確定済みの仕様 2) | 判断待ちではない。オプションは追加しない。要望が出た時点で別計画とする |
| 3. 古い Bunri(v0.6.0 以前)が 4 stem manifest で止まる案内 | 古い Bunri のコードは変えられないため、新 Bunri 側の文書で案内する。置き場所: README の Pocket 節(本計画で実施)とリリースノート(発行は範囲外、文言は方針の「リリースノート案」に記載)。エラー文言は古い Bunri が出す「Pocket の同期に失敗しました。後で再実行してください。」のまま変わらない | README の文言を方針どおりに書く。新 Bunri の `safe_error` は変更しない |
| 4. Web の状態表示と未対応の案内 | 確定: 状態一覧は既存の 3 状態のまま。未対応の案内は同期ジョブの結果表示にだけ 1 行出す(確定済みの仕様 3)。`inspect_remote` が features を反映するため、対応ポケットでは 3 ファイル同期済みの曲が「差分あり」になる(必須。契約 9) | Web UI のソース(API・テンプレート・JS)は本計画の作成時に未確認のため、実装担当が表示箇所を特定し、テストは既存の `tests/test_web_page.py` / `tests/test_web_api.py` の作り方に合わせる。変更箇所が見つからない、または表示のための API 変更が必要になる場合は止まって報告する |
| 5. capabilities の取得回数 | `sync_all` と `inspect_packages` は 1 回、`sync_one` は 1 回(`synchronize` 内)。契約の「sync の開始時に 1 回」に合わせて確定 | 方針どおり。曲ごとの呼び出しにしない |
| 6. 未対応の案内を出す条件 | 確定: 未対応なら同期したパッケージの内容に関わらず常に出す(確定済みの仕様 4) | 判断待ちではない。方針どおり実装する |
| 7. capabilities の HTTP エラー・JSON 不正 | 未対応扱いにせず、同期を開始しないで失敗させる(fail closed)。v0.2.x も capabilities を返すため、フォールバックとは区別できる。状態確認(`inspect_packages`)では例外を送出せず、ローカルで判定できる状態はそのまま、音源ポケットに問い合わせる曲は `unknown` にする | 方針どおり。`PocketHTTPClient.capabilities()` は変更しない |
| 8. `validate_capabilities` の `latest` 固定(発見事項) | 現行 `config.py` は `schemas.manifest.latest == "1.0"` を要求し、v0.3.0 の音源ポケットには `bunri pocket connect` が失敗する。契約には記載がないが Bunri 側の不備なので本計画で緩める。音源ポケット側の変更は不要 | タスク 3 で対応。既に保存済みの接続設定は connect 時にしか検査されないため影響なし |
| 9. fixture 更新の依存 | 上流 bunri-pocket 6766d7d の内容が必要。上流に L/R 関連の invalid fixture があるかは未確認 | 上流にあれば写して Sources に加える。無ければ Bunri のテスト内で組み立てる。上流 `manifest-v1.json` / `library-v1.json` が変わっていれば `UPSTREAM.md` の手順で golden を再生成し、その事実を UPSTREAM.md に残す |
| 10. 中身を確認していない実装ファイル | `src/bunri/local_package.py`、`src/bunri/web/` の API・テンプレート・JS、`tests/test_pocket_protocol.py`、`tests/test_pocket_local.py`、`tests/test_web_*.py`、`tests/pan_split_helpers.py` の中身は本計画の作成時に未確認 | 実装担当が読んで既存の作り方に合わせる。`tests/pan_split_helpers.py` に L/R ファイル生成の補助があれば再利用する。想定と異なる構造(例: `inspect_remote` を経由しない別の状態判定)があれば止まって報告する |
| 11. 既存テストの期待値 | `tests/test_pocket_sync.py` の manifest 413 テストは 741 バイトを期待する。`FakeClient` の既定を対応ポケットにしても、テスト用パッケージの `pan_split` が None なら新フィールドは書かれず `"1.0"` のまま変わらないはず | 変わった場合は原因(意図しない `schema_version` 昇格など)を確認してから期待値を直す |
| 12. 音源ポケット切り戻し時の新 Bunri の表示 | L/R 同期後に音源ポケットを 0.2.x へ戻すと、manifest GET が 422 になり新 Bunri は「Pocket との通信に失敗しました」と出す。復旧手順は README に記載する。専用の文言追加は行わない | 範囲外。README の切り戻し段落で案内する |

## 公開経路

1. 本計画書(plan.md / request.md)を計画レビュー・privacy 検査の後、作業ブランチ plan/2026-09-26-pocket-lr-send へ commit する(承認済みの範囲)
2. 実装担当が同ブランチ上でタスク 1〜10 を進め、実装レビューを受ける。commit まで
3. push と main への PR 作成は別途承認を得てから行う
4. Bunri のバージョン更新・タグ・Release の作成は本計画の範囲外。bunri-pocket v0.3.0 は Release 済みのため、本計画の検収後に別途行う。リリースノートには方針の「リリースノート案」の文言(音源ポケット 0.3.0 以降に接続しているときだけ L/R を送ること、対応する音源ポケットの版、混在環境、切り戻し)を転記する
