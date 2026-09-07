# Bunri Pocket 棚曲削除 実装計画

- 日付: 2026-09-07
- ブランチ: `plan/2026-09-07-pocket-track-delete`
- ベース: `main` / `70ae5855059bc31c6b602983fbad25a7e5288a89`
- 実装担当(駒): Sol medium（codex exec、workspace-write）。Pocket の HTTP・service・CLI・Web ジョブ・画面をまたぐ変更と関連テストを一貫して実装・検証するため

## 背景・目的

Bunri Pocket Worker には、Bearer upload token を使って曲を冪等に削除する `DELETE /api/v1/tracks/:songId` が実装済みである。一方、Bunri には棚から曲を減らす導線がなく、ローカル削除後に棚だけに残った曲も管理できない。

CLI に棚削除コマンドを追加し、Web UI の既存ローカル削除からも任意で棚削除を開始できるようにする。削除と同期は既存の song ID 解決、`out/.pocket/sync.lock`、Pocket service、安全なエラー分類を共有し、Bunri が開始する Pocket 変更を直列化する。

## 実装・検証環境

- リポジトリ: `github.com/shimabox/Bunri`
- 計画時点の `main` と `origin/main`: `70ae5855059bc31c6b602983fbad25a7e5288a89`
- 計画調査時の checkout: detached HEAD。ただし `main`、`origin/main` と同じ SHA
- 計画調査時の作業ツリー: clean
- Bunri: `0.5.0`
- Python: `3.13.1`（要件は `>=3.13`）
- uv: `0.11.14`
- ffmpeg: `8.1.2`
- CI: Ubuntu、`uv sync --frozen --extra web`、Playwright Chromium、pytest-xdist
- 全テスト: `uv run pytest -q -n auto` または `make test`
- 専用の lint、format、typecheck コマンドは定義されていない
- lint 相当の確認: `git diff --check`
- 計画調査時の `git diff --check`: 問題なし
- 読み取り専用調査のためテストは未実行

実装時の主な対象テストは次のとおりとする。

```bash
uv run pytest -q -n auto \
  tests/test_cli.py \
  tests/test_pocket_http.py \
  tests/test_pocket_service.py \
  tests/test_pocket_sync.py \
  tests/test_web_pocket_jobs.py \
  tests/test_web_jobs.py \
  tests/test_web_api.py \
  tests/test_web_page.py
```

## スコープ

### やること

- `bunri pocket delete` を追加する。
- ローカルの `safe_name`、直接指定した12桁の Pocket song ID、棚の library からの対話選択をサポートする。
- CLI に削除確認と自動化用 `--yes` を追加する。
- `PocketHTTPClient` に `/api/v1/tracks/:songId` の DELETE を追加する。
- 棚削除と既存 sync で `out/.pocket/sync.lock` を共有する。
- Web の曲削除ダイアログに「棚からも削除」を追加する。
- Web の棚削除は既存 Pocket ジョブ基盤へ載せ、再起動時の再実行と安全なエラー表示を行う。
- 棚削除後に Pocket 状態を再取得し、ローカルに残った曲を「未同期」として表示する。
- library にしか存在しない曲を識別できる状態 API と、読み取り専用の「棚にのみある曲」一覧を追加する。
- README と CLI、Pocket、Web の関連テストを更新する。

### やらないこと

- bunri-pocket Worker/PWA 側の変更。
- Cookie、password 認証、セッション API の利用。
- upload token の rotation。
- tombstone、削除履歴、削除済み検査の追加。
- DELETE と sync の並列実行。
- Worker API の503に対する無制限な自動リトライ。
- 棚側の楽器単位、asset 単位の部分削除。
- ゴミ箱、Undo、棚からローカルへのダウンロード復元。
- 棚の library にない prefix 残骸の自動探索。既知の song ID を指定した DELETE による収束だけを行う。
- 「棚にのみある曲」一覧への Web 削除ボタン。削除は CLI の `--select` または `--song-id` で行う。
- 大きな library 向けの検索、ページング。
- 新規 HTTP/runtime dependency の追加。
- バージョン更新やリリース作業。

## 方針

### 1. CLI の対象指定

次の3方式を併設する。

```text
bunri pocket delete SAFE_NAME
bunri pocket delete --song-id abcdef123456
bunri pocket delete --select
```

`SAFE_NAME`、`--song-id`、`--select` はちょうど1つだけ指定可能とする。

- `SAFE_NAME`
  - safe name 名前空間としてのみ解釈する。
  - 既存の厳密な解決経路をそのまま利用し、`.bunri-package.json` から full SHA-1 と12桁の `cache_key` を解決する。
  - 重複 identity と full digest の検証を省略しない。
  - `aaaaaaaaaaaa` という safe name が存在しても、自動的に song ID と解釈しない。
- `--song-id`
  - `[0-9a-f]{12}` を直接指定する。
  - ローカルパッケージを要求しないため、ローカル削除済み、別の出力先で生成した曲、library から消えて prefix だけ残った曲にも使える。
  - 既存の `resolve_package(..., resolution="song_id")` はローカルパッケージを song ID で探す同期用なので、直接 DELETE では呼ばない。
- `--select`
  - Bearer token で棚の library を取得・検証し、現在の順序で `タイトル — song ID` の番号一覧を表示する。
  - 同名タイトルは song ID で区別する。
  - library が存在しない、または空なら DELETE を送らず終了する。

単一引数を「12桁なら ID、それ以外なら safe name」と推測して名前空間を混ぜる設計は採らない。

対話用一覧の取得中は lock を保持しない。選択と確認が終わってから lock を取得する。safe name 指定では lock 内でも identity を再解決し、確認時と同じ song ID であることを確かめてから DELETE する。ローカルパッケージが破損して既存の厳密な解決に通らない場合は ID を推測せず、`--song-id` または `--select` を案内する。

### 2. CLI の確認 UX

最終確認では、分かる範囲でタイトル、safe name、song ID を表示する。

```text
Bunri Pocket の棚から次の曲を削除します。
  曲名: Example Song
  song ID: abcdef123456
この操作を続けますか？ [y/N]
```

- デフォルトは拒否する。
- `--yes` で最終確認だけを省略できる。
- `--yes --select` は自動化できるかのような誤解を招くため拒否する。自動化では `SAFE_NAME` または `--song-id` を必須とする。
- 成功時は204を確認して song ID を表示する。
- 503、timeout、応答喪失時は同じコマンドを再実行できる旨を表示し、非0で終了する。
- 404は成功扱いにしない。正しい Worker 契約では曲、library、prefix がなくても204になるため、404は接続先または protocol の不整合として扱う。

### 3. HTTP クライアントと service 層

`PocketHTTPClient._url()` は現在すべてを `/api/v1/upload/` 以下へ送るため、upload route と API route を分ける。既存 upload URL の動作は維持する。

`delete_track(song_id)` は次の契約に従う。

- `/api/v1/tracks/{song_id}` へ DELETE を送る。
- `Authorization: Bearer ...` と既存 `User-Agent` を使う。
- Cookie、password、request body、ブラウザの Origin は送らない。Origin 欠落が Worker 契約で許可されている server-to-server 経路を使う。
- redirect は従来どおり拒否する。
- metadata timeout は30秒とする。
- 成功は正確に204かつ空 body を期待する。
- クライアント側にレスポンスキャッシュを設けない。
- 404を冪等成功へ読み替えない。
- 503を内部で無制限再試行せず、利用者の再操作または再起動回復による同じ DELETE の再実行に委ねる。

`src/bunri/pocket/service.py` には次を追加する。

- 棚 library を取得・検証して、選択用の `song_id` と `title` を返す処理。
- safe name から削除対象の identity を厳密に解決する処理。
- 設定読込、共有 lock、HTTP DELETE をまとめる共通棚削除処理。
- CLI と Web が共通利用できる構造化された削除結果。

`safe_error()` は秘密を含まない分類を維持し、少なくとも次を区別する。

- 401: 接続設定または upload token の更新が必要。
- 409: schema major 非互換。
- 422: 棚の library が破損しており、変更も cleanup も行われていない。
- 429: `Retry-After` があれば安全な値だけを表示。
- 503、timeout: 完了を確認できず、同じ song ID で再実行可能。
- 404を含むその他の想定外応答: 接続先または protocol の確認が必要。

URL、Authorization header、token、設定ファイル内容、raw exception は CLI、Web API、ジョブ JSON、HTMLへ出さない。

### 4. 排他

棚削除にも既存の `out/.pocket/sync.lock` をそのまま使う。

- CLI sync、Web sync、CLI delete、Web delete のすべてを相互排他にする。
- lock 競合時は待機せず、CLI は非0、Web は副作用なしの409とする。
- DELETE 開始前から204またはエラー確定まで lock を保持する。
- Web ジョブは既存 sync と同様、登録時からキュー待機中も lock を保持する。
- プロセス終了時は OS の flock 解放を利用する。
- lock file に URL、token、song ID、PID は書かない。

クラス名 `SyncLock` は互換性のため維持してよいが、docstring と利用箇所では Pocket mutation lock であることを明確にする。この排他は Bunri から開始される DELETE と sync を直列化するものであり、手動 HTTP 呼び出しや別クライアントまで排他しない。その並行結果は Worker 契約どおり未定義のままとする。

### 5. Web 側は Pocket ジョブ方式を採る

棚削除を既存 `DELETE /api/songs/{web_song_id}` の同期処理内で直接待たず、Pocket ジョブへ載せる。Worker の cleanup と通信には最大30秒かかり得る。ジョブ方式なら、既存の `queued`、`running`、`done`、`error`、再起動回復、共有 lock、軽量ポーリングを再利用し、503や応答喪失後の再実行対象を永続化できる。token もブラウザへ渡さず server-to-server で DELETE できる。代わりにジョブ種別、永続レコード、回復テスト、表示状態を追加する。

API の後方互換を維持する。

- 通常の `DELETE /api/songs/{web_song_id}` は従来どおりローカルだけを削除し、空 body の204を返す。
- 「棚からも削除」が選ばれた場合は、同じルートに明示フラグを付けて `pocket_delete` ジョブを登録し、202と `job_id` を返す。フラグの具体名などは既存 API と整合する範囲で実装者が決めてよい。
- ブラウザから受け取った Pocket song ID や safe name を削除権限の根拠にしない。
- server 側で Web song ID から full digest を取得し、`resolve_package(..., resolution="song_id", expected_digest=...)` 相当の厳密な再検証を通して Pocket song ID を確定する。

Web song ID は `sha256(full SHA-1文字列)`、Pocket song ID は full SHA-1 の先頭12桁であり、同じ ID ではない。変換と対応付けは必ずサーバー側で行う。

### 6. Web の削除順と部分成功

複合削除は次の順にする。

1. 対象ローカル曲と sidecar identity を再検証する。
2. 共有 Pocket lock を取得する。
3. `pocket_delete` ジョブを永続化・登録する。
4. Worker API の DELETE を実行する。
5. 204を確認した後に既存 `JobStore.delete_song()` でローカル削除する。
6. ジョブを完了にし、lock を解放する。

棚削除を先にし、棚削除が失敗したのにローカルだけ消える状態を通常経路では作らない。

- 503、timeout、応答喪失時はローカル曲を残し、曲カードに「棚からの削除を確認できませんでした。ローカルデータは削除していません」と表示する。同じ操作を再実行できる。
- 棚204後にローカル削除が失敗した場合、`result` に `pocket_deleted=true` と `local_deleted=false` を保存する。曲カードを残して「棚からは削除済み、ローカル削除に失敗」と表示し、通常のローカル削除を再実行可能にする。既存の不足ファイルを許容する再試行方針を維持し、rollback は追加しない。
- Web server が204後に停止した場合、`running` ジョブを再キューして同じ DELETE を再実行し、Worker の冪等204後にローカル削除へ進む。
- 204応答喪失時、ジョブは失敗または再起動待ちとなるが、同じ DELETE の再実行で収束させる。
- shutdown 中の実行中 HTTP は即時停止せず、30秒 timeout と起動時の冪等再実行で扱う。

既存 `JobStore.delete_song()` は active Pocket job を拒否するため、削除 worker 自身のジョブだけを除外できる内部呼び出しを追加する。他の Pocket job や対象曲の分離ジョブは引き続き拒否する。

pending な削除ジョブと同じ full digest に新しい分離ジョブを登録できないようにする。DELETE 待機中に追加されたローカルジョブを後からまとめて消す競合を防ぐ。

### 7. Web の確認ダイアログ

既存ダイアログに、Pocket 接続済みかつ対象の sidecar identity を厳密に解決できる場合だけ、次のチェックボックスを表示する。

```text
[ ] Bunri Pocket の棚からも削除する
```

- デフォルトは未選択とし、ダイアログを開くたび未選択へ戻す。
- 選択時は削除概要へ「棚のlibrary、manifest、media」を追加し、「棚削除の確認後にローカルデータを削除する」ことを明記する。
- checkbox 未選択なら既存の同期削除 UX を変更しない。
- 選択時に202を受けたらダイアログを閉じ、カードに「棚から削除中」を表示する。
- 送信中の二重送信、Escape、キャンセルを既存どおり抑止する。
- lock 競合や登録前エラーはダイアログ内に表示し、ローカルデータを変更しない。
- job 失敗はカード内の `aria-live` 対象へ安全な文言で表示する。
- 実行中は同じ曲の upload、delete、新規 target 追加を無効にする。

### 8. 削除後の表示

状態表示は local と remote の直積として整理する。

| ローカル | 棚 | 表示 |
|---|---|---|
| あり | あり・一致 | `棚にある` |
| あり | なし | `未同期`。アップロードで正式に復元可能 |
| あり | 不一致 | `差分あり` |
| なし | あり | `棚のみ` |
| なし | なし | 一覧に表示しない |
| 確認不能 | 不明 | `確認できません`。削除済みと断定しない |

棚 DELETE 後もローカルパッケージが残る CLI 操作では、次の status 再取得で「未同期」に戻す。再度 sync すれば、契約上の正式な復元になる。

現在の `/api/pocket/status` はローカルパッケージからしか一覧を作らないため、validated library から local song ID 集合を引いた `remote_only` を追加する。Web ではローカル曲カードと混ぜず、読み取り専用の「棚にのみある曲」一覧としてタイトル、song ID、`棚のみ` badge を表示する。Web からの削除ボタンは付けない。remote 由来の文字列は HTML として挿入せず、既存どおり `textContent` を使う。

remote-only 状態は初回表示、タブ復帰、削除または sync 完了、明示再読込で更新する。軽量 job poll ごとには remote を取得しない。library に載らない prefix 残骸は一覧から発見せず、既知の song ID を `bunri pocket delete --song-id` で指定した場合だけ cleanup できる。

## タスク分解

| # | タスク | 依存 |
|---|---|---|
| 1 | `src/bunri/pocket/http.py` の URL 構築を upload/API route 対応へ整理し、`delete_track(song_id)` を追加する | - |
| 2 | `tests/test_pocket_http.py` に正しい path、Bearer、body なし、redirect 拒否、204、404非成功、401/409/422/429/503、空 body、秘密非表示のテストを追加する | 1 |
| 3 | `src/bunri/pocket/service.py` に safe name 解決、直接 song ID、remote library 一覧、共有 lock 付き棚削除を追加し、`safe_error()` を削除エラーにも対応させる | 1 |
| 4 | `tests/test_pocket_service.py` に名前空間分離、remote-only ID、重複 identity、library 欠落・空・破損、lock 解放、503再実行のテストを追加する | 3 |
| 5 | `src/bunri/pocket/cli.py` に `delete`、`--song-id`、`--select`、`--yes`、確認・取消・結果表示を追加する | 3 |
| 6 | `tests/test_cli.py` に3指定方式、排他、12桁 safe name、invalid ID、取消、`--yes`、非TTY、空 library、各エラー終了コードのテストを追加する | 5 |
| 7 | `src/bunri/web/jobs.py` に `pocket_delete` job schema、永続化、回復、共有 lock、remote-first/local-second 実行、部分結果、同 digest ジョブ登録拒否を追加する | 3 |
| 8 | `tests/test_web_pocket_jobs.py` と `tests/test_web_jobs.py` に再起動回復、503、応答喪失、204後のローカル失敗、自己 job 除外、競合、副作用なしを追加する | 7 |
| 9 | `src/bunri/web/app.py` に複合削除の202応答、job 取得、status の `remote_only`、HTTP エラー変換を追加する | 7 |
| 10 | `tests/test_web_api.py` に従来204の後方互換、複合202、lock 409、Origin、server-side ID 解決、秘密非表示、remote-only status を追加する | 9 |
| 11 | `src/bunri/web/templates/index.html.j2` に checkbox、動的概要、delete job 状態、部分成功表示、棚のみ一覧を追加する | 9 |
| 12 | `tests/test_web_page.py` に checkbox 初期値、ローカルのみ、複合削除、処理中、失敗、再操作、棚のみ表示、フォーカス・アクセシビリティを追加する | 11 |
| 13 | `README.md` へ CLI 構文、確認、再実行、共有 lock、Web 複合削除、正式な復元手順を追記する | 5, 11 |
| 14 | 対象テスト、全テスト、`uv lock --check`、`git diff --check` を実行し、`origin/main...HEAD` の変更範囲も確認する | 2, 4, 6, 8, 10, 12, 13 |

## 完了条件・受け入れ基準

- [ ] `bunri pocket delete SAFE_NAME` が sidecar から Pocket song ID を厳密に解決できる
- [ ] `--song-id` でローカルに存在しない曲を削除できる
- [ ] `--select` で validated library のタイトルと song ID から選択できる
- [ ] 12桁の safe name と song ID の名前空間が混ざらない
- [ ] 確認なしでは DELETE されず、`--yes` で決定的な対象を自動削除できる
- [ ] HTTP DELETE が正確に `/api/v1/tracks/:songId` へ body なしで送られる
- [ ] 成功は空 body の204で、404を成功扱いしない
- [ ] 503、timeout、応答喪失後に同じ song ID で再実行できる
- [ ] CLI/Web の sync と delete が同じ `out/.pocket/sync.lock` で直列化される
- [ ] lock 競合時は待機せず、副作用を起こさない
- [ ] Web のローカルのみ削除は従来どおり204で動く
- [ ] Web の複合削除は202で Pocket job として追跡できる
- [ ] 棚削除失敗時はローカル曲が残り、安全な再実行案内が表示される
- [ ] 棚204後のローカル失敗は部分結果を表示し、ローカル削除だけ再実行できる
- [ ] pending な削除対象へ同 digest の新規分離ジョブを追加できない
- [ ] Web 再起動後に未完了 DELETE を同じ song ID で再実行できる
- [ ] ローカルに残る削除済み曲が次の状態取得で「未同期」になる
- [ ] library にだけある曲が、読み取り専用の「棚のみ」一覧でローカル曲と区別される
- [ ] URL、token、Authorization、config 内容、raw exception が表示や job 記録に残らない
- [ ] 不正 Origin は副作用なしの403になる
- [ ] 対象テストと `uv run pytest -q -n auto` がパスする
- [ ] `uv lock --check` がパスする
- [ ] `git diff --check` と `git diff --check origin/main...HEAD` がパスする

## 未確定事項・リスクと判断の委ね方

| 項目 | 内容 | 実装時の扱い |
|---|---|---|
| 棚のみ曲の Web 操作 | Web から直接削除するかでスコープが変わる | 読み取り専用の「棚にのみある曲」一覧とし、削除ボタンは付けない。追加が必要ならスコープ変更として停止して報告する |
| 不完全なローカルパッケージ | sidecar identity だけを許す緩い解決には別の安全条件が必要 | 既存の重複 identity・full digest 検証を含む厳密な解決経路をそのまま使う。緩和が必要なら停止して報告する |
| ジョブ API のフラグ名 | query parameter、専用 subroute など細部には選択肢がある | 既存ローカル DELETE の204互換と複合操作の明示的202を守る範囲で実装者が決めてよい |
| 204後のローカル部分削除 | 既存ローカル削除は複数ファイルをまたぎ、完全な transaction ではない | job JSON を残し、不足ファイルを許容する既存再試行方針を維持する。rollback は追加しない |
| Web shutdown 中の HTTP | 実行中 HTTP は subprocess のように即時停止できない | 30秒 timeout と起動時の冪等再実行で扱う。即時 cancel が必要なら停止して報告する |
| remote-only 一覧の鮮度 | 外部操作や別クライアントによって library が変わり得る | 初回、タブ復帰、削除/sync 完了、明示再読込で更新する。軽量 job poll ごとの remote 取得は行わない |
| DELETE 後の競合窓 | Worker の最終空確認後に外部から並行 upload が到着する窓は残る | Bunri 内部は共有 lock で防ぐ。外部並行処理まで線形化できるとは表示・文書化しない |
| 大きな library | 上限1MiB以内でも対話一覧が長くなる可能性がある | 番号選択は全件を扱う。検索・ページングは別スコープとして報告する |
