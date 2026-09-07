# 実装依頼: Bunri Pocket 棚曲削除

## 背景

Bunri Pocket Worker には、Bearer upload token で曲を冪等に削除する API がある。Bunri に CLI の棚削除と Web の任意の複合削除を追加し、ローカル削除後に棚だけに残った曲も識別・管理できるようにする。

## 対象

- リポジトリ: `github.com/shimabox/Bunri`
- ベースブランチ: `main`（参考。起点は下記 SHA）
- ベース SHA: `70ae5855059bc31c6b602983fbad25a7e5288a89`（**この SHA から** `git switch -c plan/2026-09-07-pocket-track-delete 70ae5855059bc31c6b602983fbad25a7e5288a89` で作業ブランチを作る。ブランチ名から起点を選ばない）
- 作業ブランチ: `plan/2026-09-07-pocket-track-delete`
- 実装担当: Sol medium（codex exec、workspace-write）

変更してよい範囲は次だけとする。

- `src/bunri/pocket/http.py`
- `src/bunri/pocket/service.py`
- `src/bunri/pocket/cli.py`
- `src/bunri/web/app.py`
- `src/bunri/web/jobs.py`
- `src/bunri/web/templates/index.html.j2`
- `tests/` 内の上記機能に関連する既存テストと、必要な新規テスト
- `README.md`

`docs/plans/2026-09-07-pocket-track-delete/plan.md` と `docs/plans/2026-09-07-pocket-track-delete/request.md` は実装 commit に含めない。この2ファイルを含め、許可範囲外のファイルは変更しない。

## 棚側 DELETE API 契約

実装が従う Worker 側の契約は次のとおりとする。

- method/path: `DELETE /api/v1/tracks/{song_id}`
- `song_id`: 小文字16進12桁、`[0-9a-f]{12}`
- 認証: 既存の upload token を `Authorization: Bearer <token>` で送る
- request: body、Cookie、password、セッション情報、ブラウザ由来の Origin は送らない。server-to-server 呼び出しでは Origin 欠落が許可される
- User-Agent: 既存の Bunri Pocket HTTP client と同じものを使う
- redirect: 従来どおり追従せず拒否する
- timeout: metadata 操作として30秒
- success: 正確に204かつ空 body
- 冪等性: 対象曲、library entry、manifest、media/prefix がすでに存在しない場合も204で収束する
- cleanup: 対象曲の library entry、manifest、media/prefix を削除対象とする。library に載らない残骸は既知の song ID を直接指定した場合にのみ収束させる
- 401: 接続設定または upload token の更新が必要
- 409: schema major 非互換
- 422: library が破損している。変更も cleanup も行われていない
- 429: rate limit。`Retry-After` があれば、安全に検証できた値だけ利用者へ示す
- 503、timeout、応答喪失: 完了を確認できない。同じ song ID の DELETE を再実行してよい
- 404: 冪等成功ではない。正しい契約なら対象なしでも204なので、接続先または protocol の不整合として扱う
- client cache: DELETE response のキャッシュは設けない
- retry: client 内で503を無制限に再試行しない。CLI の再操作または Web ジョブの再起動回復に委ねる
- concurrency: Bunri 内の同期と削除は共有 lock で直列化する。手動 HTTP 呼び出しや別クライアントとの並行結果まで保証しない

URL、Authorization header、token、接続設定内容、raw exception は CLI、Web API、ジョブ JSON、HTML に出してはならない。

## タスク（この順で）

1. 作業開始前にベース SHA を確認し、次のコマンドで作業ブランチを作る。

   ```bash
   git switch -c plan/2026-09-07-pocket-track-delete 70ae5855059bc31c6b602983fbad25a7e5288a89
   ```

2. `src/bunri/pocket/http.py` の URL 構築を upload route と通常 API route に分ける。既存 upload URL の挙動を維持したまま、`delete_track(song_id)` を追加し、上記 DELETE 契約を実装する。

3. `tests/test_pocket_http.py` に、正しい path、Bearer、既存 User-Agent、body なし、redirect 拒否、30秒 timeout、204と空 body、404非成功、401/409/422/429/503、秘密非表示を検証するテストを追加する。

4. `src/bunri/pocket/service.py` に次を追加する。

   - validated library を取得し、現在の順序で選択用の `song_id` と `title` を返す処理。
   - safe name から full SHA-1 と12桁 `cache_key` を得る、既存の厳密な identity 解決。重複 identity と full digest の検証を省略しない。
   - `[0-9a-f]{12}` の直接 song ID をローカルパッケージなしで扱う経路。同期用の `resolve_package(..., resolution="song_id")` は直接 DELETE に使わない。
   - 設定読込、共有 mutation lock、HTTP DELETE をまとめた CLI/Web 共通の棚削除処理と、構造化された結果。
   - DELETE の401、409、422、429、503、timeout、404その他を秘密なしで分類する `safe_error()` 対応。

5. `tests/test_pocket_service.py` に、safe name と song ID の名前空間分離、12桁 safe name、直接指定した remote-only ID、重複 identity、full digest 不一致、library 欠落・空・破損、lock 解放、503後の同一 ID 再実行、安全なエラーを検証するテストを追加する。既存 sync の関連回帰は `tests/test_pocket_sync.py` でも確認する。

6. `src/bunri/pocket/cli.py` に次の CLI を追加する。

   ```text
   bunri pocket delete SAFE_NAME
   bunri pocket delete --song-id abcdef123456
   bunri pocket delete --select
   ```

   `SAFE_NAME`、`--song-id`、`--select` はちょうど1つだけ指定可能とする。

   - `SAFE_NAME` は safe name としてのみ扱い、12桁でも song ID と推測しない。厳密な identity 解決に失敗したら ID を推測せず、`--song-id` または `--select` を案内する。
   - `--song-id` は小文字16進12桁を直接受け取り、ローカルパッケージを要求しない。
   - `--select` は Bearer 認証で validated library を読み、現在の順序で `タイトル — song ID` の番号一覧を出す。同名曲は ID で区別する。library が存在しないか空なら DELETE を送らず終了する。
   - 一覧取得中は lock を取らない。選択と確認の後で lock を取り、safe name は lock 内で再解決して同じ song ID であることを確かめる。
   - 分かる範囲でタイトル、safe name、song ID を示して最終確認する。デフォルトは拒否で、拒否または入力不能なら DELETE しない。
   - `--yes` は最終確認だけを省略する。`--yes --select` は拒否し、自動化では `SAFE_NAME` または `--song-id` を必須とする。
   - 非TTYで選択または確認入力が必要な場合は、副作用なしで非0終了する。
   - 成功時は204を確認して song ID を表示する。503、timeout、応答喪失時は同じコマンドを再実行できると安全に案内して非0で終了する。

7. `tests/test_cli.py` に、3つの指定方式、指定方式の相互排他、12桁 safe name、invalid song ID、確認と取消、`--yes`、`--yes --select` 拒否、非TTY、空 library、lock 競合、HTTP エラー別の終了コードと秘密非表示を検証するテストを追加する。

8. 既存の `out/.pocket/sync.lock` を Pocket mutation lock として CLI sync、Web sync、CLI delete、Web delete で共有する。lock 競合時は待たず、CLI は非0、Web は副作用なしの409とする。DELETE 開始前から204またはエラー確定まで保持し、Web ジョブは登録時から queue 待機中も保持する。プロセス終了時の解放は OS の flock に任せ、lock file に URL、token、song ID、PID を書かない。`SyncLock` というクラス名を互換性のため維持してもよいが、docstring と利用箇所では Pocket mutation lock であることを明確にする。

9. `src/bunri/web/jobs.py` に `pocket_delete` ジョブを追加する。ジョブ schema、永続化、`queued` / `running` / `done` / `error`、共有 lock、再起動回復、部分結果、競合防止を既存 Pocket ジョブ基盤へ統合する。複合削除の順序は必ず次のとおりとする。

   1. 対象ローカル曲と sidecar identity を厳密に再検証する。
   2. 共有 Pocket lock を取得する。
   3. `pocket_delete` ジョブを永続化・登録する。
   4. Worker API の DELETE を実行する。
   5. 204を確認した後に既存 `JobStore.delete_song()` でローカル削除する。
   6. ジョブを完了にし、lock を解放する。

   次の障害処理も実装する。

   - 503、timeout、応答喪失ではローカル曲を残し、「棚からの削除を確認できませんでした。ローカルデータは削除していません」と表示できる安全なエラーを保存する。同じ操作を再実行可能にする。
   - 棚204後のローカル削除失敗では、`result` に `pocket_deleted=true`、`local_deleted=false` を保存する。曲を残し、「棚からは削除済み、ローカル削除に失敗」と表示可能にする。通常のローカル削除を再実行できるようにし、rollback は追加しない。
   - Web server が204後に停止した場合、起動時に `running` ジョブを再キューして同じ DELETE を実行し、冪等204後にローカル削除へ進む。204応答喪失も同一 ID の再 DELETE で収束させる。
   - shutdown 中の HTTP は即時 cancel を追加せず、30秒 timeout と起動時の冪等再実行で扱う。
   - `JobStore.delete_song()` には、削除 worker 自身のジョブだけを active job 拒否から除外できる内部呼び出しを追加する。他の Pocket job と対象曲の分離ジョブは引き続き拒否する。
   - pending な削除ジョブと同じ full digest には新しい分離ジョブを登録させない。

10. `tests/test_web_pocket_jobs.py` と `tests/test_web_jobs.py` に、正常順序、共有 lock、再起動回復、503、timeout、応答喪失、204後のローカル失敗、自己 job のみの除外、同 digest 競合、登録失敗時の副作用なしを検証するテストを追加する。

11. `src/bunri/web/app.py` で既存 DELETE API の後方互換を維持しつつ複合削除を公開する。

   - 通常の `DELETE /api/songs/{web_song_id}` は、従来どおりローカルだけを同期的に削除し、空 body の204を返す。
   - 「棚からも削除」を明示した場合、同じ route から `pocket_delete` ジョブを登録し、202と `job_id` を返す。フラグ名や query/body の細部は、通常 DELETE の204互換と複合操作の明示的202を守る範囲で決めてよい。
   - ブラウザから Pocket song ID または safe name を削除権限の根拠として受け取らない。Web song ID から server 側で full digest を得て、`resolve_package(..., resolution="song_id", expected_digest=...)` 相当の厳密な検証で Pocket song ID を確定する。
   - Web song ID は `sha256(full SHA-1文字列)`、Pocket song ID は full SHA-1 の先頭12桁であり、同一ではない。対応付けは必ず server 側で行う。
   - job 状態取得、lock 競合の副作用なし409、HTTP エラーの安全な変換を追加する。
   - `/api/pocket/status` に、validated library から local song ID 集合を引いた `remote_only` を追加する。remote-only 状態は初回、タブ復帰、削除/sync 完了、明示再読込で更新し、軽量 job poll ごとには取得しない。
   - 不正 Origin は副作用なしの403とする。

12. `tests/test_web_api.py` に、通常削除の空 body 204、複合削除の202と `job_id`、lock 409、登録前エラーの副作用なし、不正 Origin 403、server-side ID 解決、ブラウザ指定 ID を信用しないこと、秘密非表示、`remote_only` status を検証するテストを追加する。

13. `src/bunri/web/templates/index.html.j2` の既存削除ダイアログと状態表示を更新する。

   - Pocket 接続済みで対象 sidecar identity を厳密に解決できる場合だけ、「Bunri Pocket の棚からも削除する」checkbox を表示する。
   - checkbox はデフォルト未選択で、ダイアログを開くたび未選択へ戻す。
   - 選択時は概要へ棚の library、manifest、media を加え、「棚削除の確認後にローカルデータを削除する」と明記する。未選択時の既存同期削除 UX は変えない。
   - 202後はダイアログを閉じ、カードに「棚から削除中」を表示する。送信中は二重送信、Escape、キャンセルを既存どおり抑止し、同じ曲の upload、delete、新規 target 追加を無効にする。
   - lock 競合と登録前エラーはダイアログ内、job 失敗と部分成功はカード内の `aria-live` 対象へ安全な文言で表示する。
   - local/remote の状態を次のように表示する。

     | ローカル | 棚 | 表示 |
     |---|---|---|
     | あり | あり・一致 | `棚にある` |
     | あり | なし | `未同期`。アップロードで正式に復元可能 |
     | あり | 不一致 | `差分あり` |
     | なし | あり | `棚のみ` |
     | なし | なし | 一覧に表示しない |
     | 確認不能 | 不明 | `確認できません`。削除済みと断定しない |

   - `remote_only` はローカル曲カードと混ぜず、読み取り専用の「棚にのみある曲」一覧にタイトル、song ID、`棚のみ` badge を表示する。Web 削除ボタンは付けず、削除は CLI の `--select` または `--song-id` に限定する。
   - remote 由来文字列は HTML として挿入せず、既存どおり `textContent` を使う。

14. `tests/test_web_page.py` に、checkbox の表示条件と初期値、再オープン時の reset、ローカルのみ削除、複合削除、処理中、失敗、部分成功、再操作、操作無効化、棚のみ読み取り専用一覧、`textContent`、フォーカス、`aria-live` を含むアクセシビリティを検証するテストを追加する。

15. `README.md` に、3種類の CLI 構文、確認と `--yes` の制約、503等で同じ ID を再実行できること、共有 mutation lock、Web 複合削除、棚のみ一覧が読み取り専用であること、ローカルに残った曲を再度 sync する正式な復元手順を追記する。外部クライアントまで排他・線形化できるとは記載しない。

16. 対象テストから全テストへ順に実行し、lock、エラー、再起動回復、UI 回帰を確認する。

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

   uv run pytest -q -n auto
   uv lock --check
   git diff --check
   ```

17. 許可範囲のファイルだけを明示的に stage する。`git add -A` は使わず、計画書2ファイルを stage しない。日本語の分かりやすい message で commit し、push はしない。commit 後にベースとの差分を範囲形式でも確認する。

   ```bash
   git diff --name-only origin/main...HEAD
   git diff --check origin/main...HEAD
   ```

## 完了条件

- [ ] `bunri pocket delete SAFE_NAME` が sidecar から Pocket song ID を厳密に解決できる
- [ ] `--song-id` でローカルに存在しない曲を削除できる
- [ ] `--select` で validated library のタイトルと song ID から選択できる
- [ ] 12桁 safe name と song ID の名前空間が混ざらない
- [ ] 確認なしでは DELETE されず、`--yes` は決定的な対象だけを自動削除できる
- [ ] HTTP DELETE が正確に `/api/v1/tracks/:songId` へ body なしで送られる
- [ ] 成功は空 body の204で、404を成功扱いしない
- [ ] 503、timeout、応答喪失後に同じ song ID で再実行できる
- [ ] CLI/Web の sync と delete が同じ `out/.pocket/sync.lock` で直列化される
- [ ] lock 競合時は待機せず、副作用を起こさない
- [ ] Web のローカルのみ削除は従来どおり空 body の204で動く
- [ ] Web の複合削除は202で Pocket job として追跡できる
- [ ] 棚削除失敗時はローカル曲が残り、安全な再実行案内が表示される
- [ ] 棚204後のローカル失敗は部分結果を表示し、ローカル削除だけ再実行できる
- [ ] pending な削除対象へ同じ full digest の新規分離ジョブを追加できない
- [ ] Web 再起動後に未完了 DELETE を同じ song ID で再実行できる
- [ ] ローカルに残る削除済み曲が次の状態取得で「未同期」になる
- [ ] library にだけある曲が読み取り専用の「棚のみ」一覧でローカル曲と区別される
- [ ] URL、token、Authorization、config 内容、raw exception が表示または job 記録に残らない
- [ ] 不正 Origin は副作用なしの403になる
- [ ] 対象テストがパスする
- [ ] `uv run pytest -q -n auto` がパスする
- [ ] `uv lock --check` がパスする
- [ ] `git diff --check` と `git diff --check origin/main...HEAD` がパスする
- [ ] `git diff --name-only origin/main...HEAD` が許可ファイル範囲内であり、計画書2ファイルを含まない
- [ ] 作業ブランチに commit 済みである。commit message は日本語で分かりやすさを優先し、変更の「何を・なぜ」だけを一般化して記す
- [ ] push していない

## 未確定事項と判断の委ね方

- 勝手に決めてよい範囲: 既存ローカル DELETE の空 body 204互換と、複合削除の明示的202を守る範囲でのフラグ名、query/body、内部関数名、構造化結果の型名、テスト helper などの実装詳細。
- 確定済みの判断: 棚のみ曲は Web では読み取り専用、safe name は既存の重複 identity・full digest 検証を含む厳密方式、204後のローカル部分削除は既存再試行方針で rollback なし、shutdown は30秒 timeout と冪等再実行、remote-only は初回・タブ復帰・削除/sync 完了・明示再読込で更新、大きな library の検索・ページングはスコープ外。
- 止まって報告すべき範囲: Web の棚のみ一覧への削除操作追加、strict identity 解決の緩和、新規 dependency、即時 HTTP cancel、検索・ページング、Worker/PWA 変更、既存204互換の破壊、外部クライアントを含む排他保証、許可ファイル範囲外の変更、その他のスコープ拡大。

## 禁止事項

- push しない。commit までとする。
- `git add -A` を使わない。stage 対象は許可ファイルを個別指定する。
- `docs/plans/2026-09-07-pocket-track-delete/plan.md` と `docs/plans/2026-09-07-pocket-track-delete/request.md` を実装 commit に含めない。
- commit message は日本語とし、作成経緯、依頼元、計画書への参照を書かない。変更内容と利用者にとっての理由だけを一般化して記す。
- commit message、コードコメント、README その他の文書に、特定の利用元や個人的事情を変更理由として書かない。
- 私的なリンク、個人環境のローカルパス、OS ユーザー名、ホスト名、ローカル一時ファイルへの参照を書かない。
- 許可範囲外のファイルを変更しない。無関係なリファクタ、dependency 追加、version 更新、release 作業を行わない。
- Cookie、password、セッション API を利用せず、token や接続情報をブラウザまたはログへ出さない。
- 404を冪等成功へ読み替えず、503を無制限に自動再試行しない。
- 人間向けの質問 UI を出さない。判断が必要なら選択肢と推奨案を最終報告に記載し、そこで停止する。

## 報告フォーマット

- 変更ファイル一覧
- 実行した対象テスト、全テスト、`uv lock --check`、`git diff --check`、`git diff --check origin/main...HEAD` と各結果
- commit SHA と日本語 commit message
- 判断に迷った点、未解決の懸念、停止した場合は選択肢と推奨案
