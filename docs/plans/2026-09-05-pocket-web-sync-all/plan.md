# Bunri Pocket Web 棚同期・一括同期 実装計画

- 日付: 2026-09-05
- ブランチ: `plan/2026-09-05-pocket-web-sync-all`
- ベース: `main` / `60043e02722817c7ad83ad5c8ec42387208724f2`
- 実装担当(駒): Sol medium（`codex exec`、`workspace-write`）。既存 Python 実装の整理、CLI/Web の統合、後方互換テストを一続きで実施するため

## 背景・目的

現在の `bunri pocket sync <SAFE_NAME>` は、ローカルパッケージの `.bunri-package.json` と実際の MP3 を検査し、media、manifest、library の順に冪等同期する。この同期機能を Web UI からも利用できるようにし、接続状態、曲単位の棚との差分、個別・一括アップロード、実行状況をローカル画面で確認できるようにする。

同時に CLI に `bunri pocket sync --all` を追加し、出力ディレクトリ内の同期可能な全パッケージを決定的な順序で逐次同期する。Web と CLI は同じ同期サービスを利用し、表示専用の `/api/songs[].downloads` や `_download_files()` を同期判断や manifest 生成に使わない。

## 環境情報

- リポジトリ: `github.com/shimabox/Bunri`
- 起案時の checkout: detached HEAD。ただし `main`、`origin/main` と同じベース SHA
- 起案時の作業ツリー: clean
- Bunri: `0.5.0`
- Python 要件: `>=3.13`。確認環境は `3.13.1`
- uv: `0.11.14`
- ffmpeg: `8.1.2`
- CI: Ubuntu、`uv sync --frozen --extra web`、Playwright Chromium、pytest-xdist
- 全テスト: `uv run pytest -q -n auto` または `make test`
- 専用の lint/format コマンドは定義されていない。lint 相当のゲートはテストと `git diff --check`
- 計画調査は読み取り専用で実施したため、テストと build は未実行。差分確認と `git diff --check` には問題なし
- 主な調査対象: `src/bunri/pocket/{cli,config,http,local,protocol,sync}.py`、`src/bunri/web/{app,jobs}.py`、`src/bunri/web/templates/index.html.j2`、Pocket/CLI/Web の各テスト、CI、Makefile、既存の Pocket/Web 計画

今回の対象テスト:

```bash
uv run pytest -q -n auto \
  tests/test_cli.py \
  tests/test_pocket_*.py \
  tests/test_web_api.py \
  tests/test_web_jobs.py \
  tests/test_web_jobs_properties.py \
  tests/test_web_page.py
```

追加検証:

```bash
uv lock --check
uv build
git diff --check
```

## スコープ

### やること

- Pocket 設定が有効な場合だけ、Web UI に「Bunri Pocket 連携中」バナーを表示する。
- 曲単位に `棚にある`、`未同期`、`差分あり`、`再生成が必要`、`確認中`、`確認できません` などの同期状態を表示する。
- 12桁の song ID が衝突している場合は差分と競合を示し、アップロードを無効にする。
- 曲ごとの `⬆ アップロード` と、ファイルシステム上の全パッケージを対象にする `全曲アップロード` を追加する。
- Web 同期を既存ジョブと同じ `queued / running / done / error`、経過時間、ポーリング表示に載せる。
- `bunri pocket sync --all -o <OUT>` を追加する。
- パッケージ列挙、ローカル preflight、remote 状態判定、個別・一括同期、結果集計を CLI/Web 共通サービスへ整理する。
- CLI と Web をまたいだ sync 排他を設ける。
- 接続先 URL、upload token、`config.json` の内容を Web API、HTML、ジョブ JSON、ログ、例外表示に出さない。
- README と Pocket/CLI/Web の関連テストを更新する。

### やらないこと

- `_download_files()`、Job JSON 内の download URL、Web 表示用ファイル一覧から manifest を組み立てること。
- サイドカーの推測生成、旧 Job JSON からの移行、サイドカーの手作業生成。
- Pocket 側の曲、target、asset、未知フィールドの削除。
- 自動同期、常時監視、双方向同期。
- 複数 sync の並列実行。
- Bunri Pocket Worker/PWA 側の変更。
- 分離処理そのものの並列化や、外部プロセス間のパッケージ生成競合の全面解決。
- 新しい HTTP/runtime dependency の追加。
- バージョン更新やリリース作業。
- Web UI への `--no-original` 相当の選択肢の追加。

## 確定仕様

- `sync --all` はサイドカーのない旧パッケージをアップロード対象外として一覧報告し、再生成が必要であることを案内した上で、有効な曲の同期を続行する。legacy の存在だけでは一括同期を失敗扱いにしない。
- 一括同期中に1曲で remote エラーが発生したら、その時点で後続の同期を停止し、完了・失敗・未実行を集計表示して終了する。同期は冪等であり、再実行で収束させる。
- Web の `全曲アップロード` は、Web 一覧に表示されない CLI 生成曲を含め、出力ディレクトリ内の全パッケージを対象にする。対象件数を UI で明示する。
- sync lock の競合時は待機しない。CLI は明示エラー、Web は HTTP 409 を返し、二重ジョブを作らない。
- Web からの個別・一括同期は original MP3 を含める。
- Pocket status API の endpoint 名と JSON の細部は、秘密を返さず、song ID で対象を指定し、remote 検査を軽量なジョブポーリングから分離する限り、実装者が決めてよい。

## 方針

### 1. 同期元とパッケージ探索

`src/bunri/pocket/local.py` の `preflight()` を同期元の正本として維持する。Web と CLI のどちらから実行しても、最終的には `out/<safe_name>/.bunri-package.json` と同じディレクトリ直下の実 MP3 を再検査し、SHA-256 と size を算出する。表示上のキャッシュや Web の download 情報を実行時の正本にしない。

一括処理用には、現在エラーメッセージ候補用に最大20件へ制限されている `package_candidates()` を流用せず、全件を決定的な順序で列挙する関数を追加する。dot directory、`web`、`.cache`、`.pocket`、symlink、通常ディレクトリでないものは同期候補にしない。

Web の曲ボタンではブラウザから任意の `safe_name` を受け取らない。Web の song ID からサーバー側で期待する full SHA-1 とパッケージ候補を解決し、実行直前にサイドカーの full digest がその曲と一致することまで確認する。

### 2. CLI/Web 共通 Pocket サービス

`src/bunri/pocket/sync.py` を低レベルの remote 収束処理として残し、その上に CLI/Web 共通のサービス層を置く。既存モジュールの責務を保てるなら新しいモジュールを追加してよい。

共通化する責務:

- 設定読込。
- パッケージ全件探索と legacy 分類。
- 個別・一括 preflight と、Web 用の expected full digest 検証。
- 重複する local song ID/full digest の検出。
- remote 状態検査。
- sync 排他。
- `synchronize()` の呼び出し。
- バッチ集計と安全なエラー分類。

CLI 固有の Rich 表示や Web 固有の JSON/HTTP status はサービス層へ混ぜず、型付きの結果とエラーをそれぞれの入口で変換する。

### 3. 同期状態の定義

remote 状態は `synchronize()` が no-op になる条件と揃える。

- `未同期`: remote manifest が存在せず、library にも対象曲がない。
- `棚にある`: manifest の source digest が一致し、`merge_manifest()` が実質変更なしになり、対象 media の HEAD 結果がローカルの SHA-256/size と一致し、library に対象曲があり、`merge_library()` も実質変更なしになる。
- `差分あり`: 同じ曲の remote manifest/library/media は存在するが、`棚にある` の条件のいずれかが一致しない。
- `再生成が必要`: パッケージディレクトリはあるが `.bunri-package.json` がない。
- `確認できません`: 設定破損、remote 到達不能、unsupported schema、remote 文書不正など。これを `未同期` に読み替えない。
- `確認中`: remote 状態をバックグラウンド取得している間の表示。既存の曲一覧は保持する。
- 12桁 song ID が同じで full digest が異なる場合: 表示上は差分を示しつつ「競合のためアップロード不可」を併記する。

状態確認は GET/HEAD のみに限定し、PUT を送らない。多数曲・多数 asset では遅くなるため、Web の通常の `/api/songs` ポーリングから remote 検査を分離する。初回表示、タブ復帰、同期完了、明示再読込を基本の更新契機とし、2秒ごとのジョブ poll では remote 検査を行わない。同期実行時は必ず再度 preflight し、表示用 snapshot を信頼しない。

### 4. Web API

API の基本形は次のとおりとする。endpoint 名と JSON の細部は確定仕様の制約内で調整できる。

- `GET /api/pocket/status`: 接続有無、曲ごとの同期状態、同期可否、再生成案内、全曲同期の対象件数を返す。接続先 URL、token、config の実値は返さない。
- `POST /api/pocket/sync/{song_id}`: song ID に対応する曲の同期ジョブを登録する。
- `POST /api/pocket/sync`: ファイルシステム上の全対象を処理する一括同期ジョブを登録する。
- 既存 `GET /api/songs`: 曲に紐づく最新同期ジョブの軽量な状態だけを追加し、remote 検査は行わない。

POST は既存 same-origin middleware の対象にする。入力不正、実行中競合、接続なし、再生成必要は、固定された安全な利用者向けメッセージと適切な 4xx に変換する。排他競合は 409 とし、ジョブを登録しない。

### 5. Web ジョブ実行

Pocket 同期も識別可能なジョブ種別として既存の逐次キューへ載せ、同期本体は CLI subprocess ではなく共通サービスの Python 関数を直接呼ぶ。

- 既存分離ジョブの JSON は後方互換を維持し、旧レコードに job kind がなければ分離ジョブとして扱う。
- Pocket 同期ジョブは分離用の `target`、`upload`、`package` を無理に流用せず、識別可能なレコード型にする。
- worker は分離ジョブなら従来どおり CLI subprocess、Pocket ジョブなら共通サービスを直接呼ぶ。
- 同じ Web worker で逐次化し、Web 内の分離処理と同期処理が同じパッケージを同時に更新・読取しないようにする。
- 一括同期は1つの batch job として扱う。legacy を報告対象として分類し、残る同期対象の全ローカル preflight と重複 identity 検査を終えてから、最初の HTTP 書き込みを行う。
- batch job には総件数、完了件数、現在処理中の曲、完了・失敗・未実行の集計を保存できるようにする。
- remote エラーが1曲で起きたら後続を実行せず、そこまでの完了、失敗した曲、未実行を確定して終了する。
- 起動時に `running` のまま残った Pocket ジョブは `queued` に戻し、冪等同期を再実行して収束させる。
- 曲削除は、その曲の同期ジョブが `queued` または `running` なら 409。一括同期中は対象確定との競合を避けるため、曲削除を止める。
- 同一曲の個別ジョブや同一 batch の `queued`/`running` ジョブを重複登録しない。
- UI と永続レコードに保存するエラーは安全に分類したメッセージだけにし、raw exception や秘密情報を残さない。

直接呼び出しを選ぶ理由は、Pocket 層が torch/audio_separator を import せず、サーバー起動やメモリ解放のための subprocess 隔離を必要としないこと、`preflight()` と `synchronize()` の構造化結果を直接扱えること、CLI 出力解析が不要なこと、token を argv、環境変数、ログへ渡さずに済むことにある。

トレードオフとして、実行中の urllib 呼び出しは Web shutdown 時に subprocess のように強制終了できない。既存の30秒/300秒 timeoutを上限とし、shutdown 後の未完了ジョブは次回起動時に再実行する。即時 cancel が必須になる変更はこの計画の範囲を超えるため、実装を止めて報告する。

### 6. sync 排他

Web 内は単一キューで直列化する。CLI/Web 間は `out/.pocket/` 配下に秘密情報を含まない lock file を置き、macOS/Linux/WSL で `fcntl.flock()` の advisory lock を使う。

- lock file は symlink を追わず、安全な `.pocket` ディレクトリ内だけで開く。
- PID、URL、token などは lock file に書かない。
- 個別 sync は1曲の完了まで、一括 sync は batch 全体の完了まで lock を保持する。
- 競合時は待機せず、CLI は明示エラー、Web は 409 とし、二重ジョブを作らない。
- プロセス終了時は OS が lock を解放するため、stale lock の手作業削除を不要にする。

これは sync 同士の排他である。外部 CLI が同じパッケージを生成しながら sync するケースは、既存の「同一出力への複数生成 process 非対応」のまま残す。

### 7. Web UI

`src/bunri/web/templates/index.html.j2` の既存1ファイル構成とアクセシビリティ方針を維持する。

- 接続済みの場合だけ、ヘッダ付近に接続先を含まない「Bunri Pocket 連携中」バナーを表示する。
- 曲一覧見出し付近に `全曲アップロード` を置き、ファイルシステム全件を対象にすることと対象件数を明示する。
- 各曲カードに同期状態バッジと `⬆ アップロード` を追加する。
- サイドカーなしはボタンを無効にし、「元の入力音源から再生成してください。キャッシュが残っていれば分離処理は省略されます」と案内する。
- sync job の `queued/running/done/error` と経過時間は既存 `badgeFor()`、ポーリング、フォーカス復元の考え方を共用する。
- sync 中は全同期ボタンを無効化して重複送信を防ぐ。
- `棚にある` 完了後も、分離ジョブの「プレイヤーを開く」「⬇ ダウンロード」は変更しない。
- error 詳細には安全に分類した利用者向けメッセージだけを表示し、raw exception、HTTP URL、config 内容を出さない。

### 8. `bunri pocket sync --all`

既存 `sync` の positional `SAFE_NAME` を任意にし、`SAFE_NAME` と `--all` のどちらか一方だけを必須とする。

```bash
bunri pocket sync <SAFE_NAME> -o out
bunri pocket sync --all -o out
bunri pocket sync --all -o out --no-original
```

一括処理は次の順で行う。

1. config を読み込み、非ブロッキングの排他 lock を取得する。
2. 全ローカルパッケージを決定的な順序で探索し、サイドカーなしを `再生成が必要` として分類する。
3. サイドカー付き候補をすべて preflight する。
4. 重複 song ID/full digest を検査する。
5. 同期対象に1件でもローカル不正があれば、最初の HTTP request より前に全体を停止する。legacy は同期対象外として一覧報告し、有効な曲の処理を妨げない。
6. 検証済みパッケージを逐次 `synchronize()` する。
7. remote エラー時はその曲で停止し、曲ごとの結果と完了・失敗・未実行の合計を表示して非0終了する。
8. 全同期対象が成功した場合も、legacy の再生成案内を含む集計を表示する。

legacy は同期対象外であり、その存在だけでは非0終了にしない。同期対象の local/identity 検証失敗、remote エラーなどの実エラーを終了コードへ反映する。

`--no-original` は全曲へ一律適用する。Web UI は個別指定 UI を増やさず、既存 CLI の既定と同じく original を含める。

## タスク分解

| # | タスク | 依存 |
|---|---|---|
| 1 | 現行 CLI、Pocket、Web job/API/UI の回帰テストを固定する | - |
| 2 | 全件パッケージ探索、legacy 分類、expected digest 付き preflight、重複 identity 検出を追加する | 1 |
| 3 | remote manifest/library/media を PUT なしで比較する同期状態検査を追加する | 2 |
| 4 | 個別・一括同期と結果集計を CLI/Web 共通サービスへ切り出す | 2, 3 |
| 5 | 安全な cross-process sync lock を実装し、個別・一括 CLI へ適用する | 4 |
| 6 | `sync [SAFE_NAME] --all` の排他的引数検証、全件表示、終了コードを追加する | 4, 5 |
| 7 | Web job schema を後方互換な識別型へ拡張し、同じ逐次 worker から Pocket サービスを直接呼ぶ | 4, 5 |
| 8 | Pocket job の永続化、再起動復旧、dedup、batch progress、削除競合、秘密を含まないエラー処理を追加する | 7 |
| 9 | Pocket status、個別 sync、全曲 sync API を追加し、`_download_files()` 非依存を固定する | 3, 8 |
| 10 | 連携バナー、同期バッジ、個別/全曲ボタン、進行表示、再生成案内をテンプレートへ追加する | 9 |
| 11 | README へ Web 同期と `sync --all`、旧パッケージ再生成、排他動作を追記する | 6, 10 |
| 12 | 対象テスト、全テスト、lock/build/diff 検証を実行する | 1-11 |

## 完了条件・受け入れ基準

- [ ] Pocket 未接続ではバナーと同期操作を表示せず、接続情報の実値も返さない。
- [ ] 接続済みでは「連携中」バナーが表示されるが、URL、token、config 内容は HTML、API、ログに現れない。
- [ ] 曲ごとに `棚にある / 未同期 / 差分あり` が remote 収束条件に基づいて表示される。
- [ ] サイドカーのない旧パッケージは `再生成が必要` となり、HTTP PUT が発生しない。
- [ ] `sync --all` は legacy を再生成対象として全件報告しながら、有効な曲を同期する。
- [ ] legacy の存在だけでは `sync --all` を非0終了にせず、実際の検証失敗または remote エラーを非0終了にする。
- [ ] Web 個別同期と全曲同期が `queued → running → done/error` で表示される。
- [ ] Web の全曲同期は Web 一覧外の CLI 生成曲を含むファイルシステム全件を対象とし、件数を表示する。
- [ ] Web 同期は `preflight()` と `synchronize()` の共通経路を直接利用し、original MP3 を含める。
- [ ] `_download_files()` や downloads レスポンスを壊しても同期結果に影響しないテストがある。
- [ ] Web 同期中に同じ Web から別 sync を開始できず、CLI との同時 sync も lock で即時拒否される。
- [ ] sync lock は異常終了後に stale 状態を残さない。
- [ ] `bunri pocket sync --all` が全対象を決定的な順序で逐次同期する。
- [ ] 一括 local preflight 失敗時、最初の HTTP request より前に停止する。
- [ ] 一括同期の remote 途中失敗時は後続を止め、完了・失敗・未実行を集計し、再実行で収束する。
- [ ] remote の未知フィールド、既存の別 target、別曲を削除しない。
- [ ] Web job の旧 JSON が引き続き読み込め、既存分離ジョブの subprocess/recovery 動作が変わらない。
- [ ] 同期ジョブ実行中の曲削除が拒否され、一括同期中の曲削除も拒否される。
- [ ] status 確認は GET/HEAD のみで PUT しない。
- [ ] token、Authorization、base URL が Web レスポンス、ジョブレコード、stderr、エラー詳細に含まれない。
- [ ] 対象テストが通る。
- [ ] `uv run pytest -q -n auto`、`uv lock --check`、`uv build`、`git diff --check` が通る。

## 未確定事項・リスクと判断の委ね方

着手を妨げる未確定事項はない。以下は確定した設計境界の中で扱う実装リスクである。

| 項目 | 内容 | 実装時の扱い |
|---|---|---|
| duplicate identity | 同じ full digest の別 safe name、または同じ12桁 ID の別 digest が複数存在すると、正しいタイトルや asset を推測できない | 曖昧な対象はアップロード不可にする。一括処理では HTTP 前に全体停止し、競合内容を秘密なしで報告する |
| status 確認コスト | 全 MP3 の hash と remote HEAD は曲数に比例し、初回表示が遅くなり得る | remote 検査をバックグラウンド化し、既存一覧と snapshot を保持する。sync 実行時は必ず再 preflight する |
| 状態更新頻度 | 外部 CLI や Pocket 側だけで状態が変わる可能性がある | 初回、タブ復帰、同期完了、明示再読込を基本とする。追加の更新頻度は負荷と鮮度を保つ範囲で実装者判断可。ジョブ poll ごとの remote 検査はしない |
| direct sync の shutdown | in-process HTTP は subprocess のように即時 kill できない | 既存 timeout と起動時の冪等再実行で扱う。即時 cancel が必要になるなら実装を止め、代替案と推奨を報告する |
| Pocket status API 形状 | endpoint 名や JSON の細部は固定しない | 秘密非表示、song ID 指定、remote 検査と軽量 job poll の分離を守る範囲で実装者判断可 |
| 変更範囲の拡大 | 新規依存、Worker/PWA 変更、分離処理の設計変更は今回の目的を超える | 実装を止め、必要な選択肢と推奨を報告する |
