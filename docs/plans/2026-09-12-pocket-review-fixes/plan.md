# Bunri Pocket 導線の所見 F1〜F6 の修正 実装計画

- 日付: 2026-09-12
- リポジトリ: github.com/shimabox/Bunri
- ベースブランチ: main
- ベース SHA: aa8f10f8196fcffc811bc53994892879d30d3219
- 作業ブランチ: plan/2026-09-12-pocket-review-fixes
- 起案担当: 予定: claude-fable-5-1 / effort 未取得。確認済み: claude-fable-5-1 / effort 未確認（確認源: Claude Code のシステム供給メタデータ）
- 計画レビュー担当: 予定: gpt-6-astra / high（未実行）
- 編成の例外理由: なし
- 実装担当: gpt-5.6-sol / high。ジョブ記録の検証と回復、Web UI の JavaScript、既存テストの更新が絡む中難度で、実装境界が計画で確定しているため
- 計画書の扱い: レビュー・privacy 検査後に commit（実装担当が実装と一緒に commit する）
- 公開範囲: 作業ブランチへのローカル commit まで。push、PR 作成、main への統合、リリースは追加判断が必要

## 背景・目的

Bunri は、分離処理をローカルで完結させ、入力音源を外部へそのまま送信せず、利用者が明示的に実行した場合だけ分離後の MP3 を本人所有の Pocket 棚へ送る。`docs/reviews/2026-09-12-pocket-adversarial-review.md` に記録された所見 F1〜F6 と、`tests/test_pocket_security_review_poc.py` の再現テストに対応する。両ファイルはベース SHA では未追跡であり、采配役が委譲前に実装 worktree へ配置し、実装ブランチで実装と一緒に commit する。

所見は次のとおり。

- F1（Medium）: `original.mp3` に入力ファイルの ID3 タグ（artist、comment、album、title など）と ffmpeg のビルド文字列（`encoder=Lavf...`）が残ったまま送信される。stem の MP3 にも encoder タグが入る。原因は `src/bunri/audio.py` の ffmpeg 呼び出しがメタデータを引き継ぐこと。
- F2（Low）: Web UI の同期ジョブ（`POST /api/pocket/sync/{id}` と `POST /api/pocket/sync`）が、画面で確認した接続先の fingerprint に束縛されていない。分離ジョブ待ちの間に CLI で別の棚へ `connect` すると、queued の同期が新しい棚へ送られる。削除ジョブは束縛済み。
- F3（Low）: `PocketHTTPClient` の opener が urllib 既定の `ProxyHandler` を含むため、`http_proxy` 環境変数があると loopback 宛の平文リクエストが Bearer トークンごとプロキシへ流れる。
- F4（Low）: 棚が不正な HTTP 応答行を返すと `http.client.BadStatusLine`（`http.client.HTTPException`。`OSError`、`RuntimeError`、`ValueError` のいずれでもない）がすべての `except` を素通りし、CLI では生のトレースバック、`GET /api/pocket/status` では 500 になる。Typer の locals 表示が有効なら `_request` フレームの `request_headers` として Bearer トークンが端末に出る。
- F5（Low）: `bunri pocket delete --select` の一覧と確認表示が棚由来の title を rich markup として解釈し、`[/x]` で `MarkupError` によりクラッシュし、`\x1bc` や `\x1bM` などの端末エスケープを素通しする。
- F6（Info）: README の約束の文言から、「原曲を再エンコードした MP3 も既定で送る（Web UI には除外手段がない）」ことと、「manifest に入力ファイルの SHA-1 が入る」ことを読み取りにくい。

修正後は、送信される MP3 が音声だけになり、Web の同期が画面で確認した棚に束縛され、トークンが環境プロキシへ出ず、通信異常が常に整形されたエラーになり、棚由来の文字列で端末表示を偽装できなくなり、README が実挙動と一致する。

## スコープ

- 対象:
  - `src/bunri/audio.py`（F1）
  - `src/bunri/pocket/http.py`（F3、F4）
  - `src/bunri/pocket/service.py`（F2、F4 のメッセージ）
  - `src/bunri/pocket/cli.py`（F2、F4、F5）
  - `src/bunri/cli.py`、`src/bunri/web/cli.py`（F4: Typer app の `pretty_exceptions_show_locals=False`）
  - `src/bunri/web/app.py`、`src/bunri/web/jobs.py`、`src/bunri/web/templates/index.html.j2`（F2）
  - `README.md`（F3、F6）
  - `tests/test_pocket_security_review_poc.py`（docstring 更新。テスト名は変えない）
  - `docs/reviews/2026-09-12-pocket-adversarial-review.md`（冒頭に対応計画への相対リンクを 1 行追記）
  - 既存テストの更新と追加
- 対象外:
  - 分離処理、モデル検証、出力先配下のパス操作、CSRF 基盤
  - キャッシュのバージョン（`_NORMALIZE_VERSION`）の変更。encode 側でタグを落とすため、キャッシュ済み `input.wav` にタグが残っていても送信物には影響しない
  - Web UI に original MP3 を除外する選択肢を追加すること
  - Pocket プロトコル（manifest の `source.digest` が 40 桁必須）の変更
  - push、PR、リリース

## 方針

### F1: MP3 のメタデータ除去

`normalize_to_wav` の ffmpeg 引数に `-map_metadata -1 -vn -fflags +bitexact -flags:a +bitexact` を、`encode_mp3` の引数に `-map_metadata -1 -fflags +bitexact -flags:a +bitexact` を追加する。いずれも入力指定の後ろ、出力ファイルの直前に置く。この組み合わせで ffprobe の `format_tags` が空になることは確認済みである。

`_NORMALIZE_VERSION` は上げない。既存パッケージの MP3 は `bunri <入力ファイル>` の再実行により、分離キャッシュを利用しつつ再出力される。この運用上の注意を README の Pocket 節に 1 文で記載する。

### F2: 同期処理の接続先への束縛

同期を削除と同じ形で接続先に束縛する。

- `src/bunri/pocket/service.py`: `sync_one` と `sync_all` に `expected_connection_fingerprint: str | None = None` を追加する。値が渡された場合、ロック取得後に `read_config` した config の `connection_fingerprint` と照合する。不一致なら `PocketServiceError("接続先が変更されたため同期を中止しました。状態を再読込して確認し直してください", kind="connection_changed")` を送出し、アップロードを開始しない。`safe_error` の `PocketServiceError` メッセージ表に `"connection_changed"` を追加し、同じ文言を割り当てる。
- `src/bunri/web/app.py`: `POST /api/pocket/sync/{pocket_song_id}` と `POST /api/pocket/sync` に query パラメータ `pocket_fingerprint: str | None = None` を追加する。未指定なら 409（detail: 「Pocket の接続先を確認できないため同期を中止しました。状態を再読込して確認し直してください。」）、現在の config の fingerprint と不一致なら 409（detail: 「Pocket の接続先が変更されたため同期を中止しました。状態を再読込して確認し直してください。」）を返す。照合はロック取得後に行う。一致した fingerprint を `create_pocket_job` に渡す。
- `src/bunri/web/jobs.py`: `create_pocket_job` に `connection_fingerprint: str` を追加し、`Job.pocket_connection_fingerprint` に保存する。`_validate_job_record` の fingerprint 検証（64 桁の小文字 16 進）を `pocket_delete` だけでなく `pocket_single` と `pocket_all` にも適用し、全 pocket 種別で必須にする。旧版で queued のまま残った fingerprint なしの同期記録は、既存の不正記録の扱いに従い隔離する。`_run_pocket_job` は `sync_one` と `sync_all` に `expected_connection_fingerprint=job.pocket_connection_fingerprint` を渡す。`connection_changed` で失敗したジョブは `status="error"` とし、`error` には `safe_error` の文言を入れる。
- `src/bunri/web/templates/index.html.j2`: `postPocket(url)` は `pocketConnectionFingerprint` を `?pocket_fingerprint=` として付与する。fingerprint がない（null）場合は送信せず、「Pocket の接続先を確認できません。状態を再読込してください。」を表示する。`/api/pocket/job` のポーリング結果で fingerprint が現在値と異なる場合は、`pocketStatuses` を空にして `refreshPocketStatus()` を呼び、曲ごとの状態と削除ダイアログの fingerprint を新しい接続先で取り直す。
- `src/bunri/pocket/cli.py`: `sync` の起動時に読んだ config の `connection_fingerprint` を `sync_one` と `sync_all` に渡す。これにより、完了メッセージの接続先と実際の送信先を一致させる。
- 既存テストで `POST /api/pocket/sync...` を fingerprint なしで呼んでいる箇所は、テストの意図を保ったまま正しい fingerprint を付ける形に更新する。

### F3: 環境プロキシの不使用

`src/bunri/pocket/http.py` の opener を `urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))` にし、環境変数のプロキシ設定を使わない。README の Pocket 節に「環境変数のプロキシ設定（`http_proxy` など）は使わず、常に指定した URL へ直接接続する」と 1 文で記載する。

### F4: HTTP 応答異常の安全なエラー変換

`src/bunri/pocket/http.py` の `_request` で、`self._opener.open(...)` と応答の読み取り（正常系の `response.read` と `HTTPError` 分岐の `exc.read`）を `except http.client.HTTPException as exc:` で捕捉し、`raise OSError(f"Pocket の応答を解釈できません: {type(exc).__name__}") from None` に変換する。元例外は連鎖させない。これにより CLI、Web、service の既存の `OSError` 処理に乗せる。

加えて、`src/bunri/cli.py`、`src/bunri/pocket/cli.py`、`src/bunri/web/cli.py` の `typer.Typer(...)` に `pretty_exceptions_show_locals=False` を明示する。

### F5: 端末表示の無害化

`src/bunri/pocket/cli.py` に、表示用の文字列から C0 制御文字（U+0000〜U+001F と U+007F）を U+FFFD に置換するヘルパーを追加する。`delete --select` の一覧（`{index}. {title} — {song_id}`）と削除前の確認表示（曲名と safe name）は `console.print(..., markup=False, highlight=False)` で出力する。棚由来の title とローカル由来の safe name の両方に置換を適用する。

### F6: README の約束と実挙動の一致

`README.md` の冒頭 2 箇所（英語段落と日本語段落）の約束を、次の内容に改める。

- 分離処理はローカルで完結し、入力音源はそのままでは送信されない。
- 利用者が明示的に Pocket へアップロードした場合に限り、分離後の MP3 と、既定では原曲を再エンコードした MP3 を本人所有の Pocket 棚へ送る。
- CLI は `--no-original` で原曲 MP3 を除外できる。Web UI は常に原曲 MP3 を含む。
- 送信する MP3 は入力ファイルのタグを引き継がない。
- 棚の manifest には入力ファイルの SHA-1 が識別子として入る。

Pocket 節の、現在「`sync` の曲名は…」で始まる段落付近にも、SHA-1、タグ、プロキシ、再出力の注意を簡潔に記載する。

### PoC と報告書

`tests/test_pocket_security_review_poc.py` の docstring を、「`test_poc_*` は所見の再現テストで修正後は通る。`test_guard_*` は閉じている経路の証跡」という内容に改める。テスト関数名は変えない。

`docs/reviews/2026-09-12-pocket-adversarial-review.md` のタイトル直後に「対応計画: `docs/plans/2026-09-12-pocket-review-fixes/plan.md`」の 1 行を追記する。

独立実装レビューは commit 後に采配役が gpt-6-astra / high の新規セッションで行い、セキュリティと並行性を確認する。実装担当はレビューを起動しない。

## タスク分解

| # | タスク | 依存 |
|---|---|---|
| 1 | F4: `http.py` の `_request` で `http.client.HTTPException` を `OSError` に変換。3 つの Typer app に `pretty_exceptions_show_locals=False` | - |
| 2 | F3: `http.py` の opener に `ProxyHandler({})` | 1 |
| 3 | F1: `audio.py` の ffmpeg 引数追加 | - |
| 4 | F5: `pocket/cli.py` の表示ヘルパーと `markup=False, highlight=False` 出力 | - |
| 5 | F2 service: `sync_one` / `sync_all` の `expected_connection_fingerprint` と `safe_error` の `connection_changed` | - |
| 6 | F2 CLI: `pocket sync` が fingerprint を渡す | 5 |
| 7 | F2 Web: `jobs.py`（記録・検証・実行）、`app.py`（query と 409）、`index.html.j2`（送信とポーリング時の再読込） | 5 |
| 8 | 既存テストの更新（fingerprint 付与、`_validate_job_record` の期待、ffmpeg 引数を検査するテストがあれば更新）と新規テスト（fingerprint 必須・不一致 409、実行時 `connection_changed`、`HTTPException` の変換、プロキシ無視、タグ除去、表示のエスケープ） | 1〜7 |
| 9 | F6: README 更新 | 3 |
| 10 | PoC の docstring 更新、報告書への対応計画リンク追記 | 1〜9 |
| 11 | `uv run pytest -q -n auto` 全件実行と証拠記録、commit | 1〜10 |

## 完了条件・受け入れ基準

- [ ] `uv run pytest -q tests/test_pocket_security_review_poc.py` が全件成功する（`test_poc_*` 11 件が失敗から成功に変わり、`test_guard_*` 7 件は成功のまま）
- [ ] `uv run pytest -q -n auto` が全件成功する（CI と同じコマンド）
- [ ] F1: 入力ファイルにタグを付けた m4a を分離すると、`<safe>.original.mp3`、`<safe>.<target>.mp3`、`<safe>.<target>.backing.mp3` のいずれも `ffprobe -show_entries format_tags` の `tags` が空になることをテストで検証する
- [ ] F2: `POST /api/pocket/sync/{id}` と `POST /api/pocket/sync` は `pocket_fingerprint` 未指定または不一致で 409 を返し、ジョブを作成しない。一致した場合はジョブ記録に `pocket_connection_fingerprint` が保存される。実行前に config が別の棚へ変わっていた場合、ジョブは `error` になり、アップロードを開始せず `synchronize` が呼ばれない。`_validate_job_record` は fingerprint のない `pocket_single` と `pocket_all` の記録を不正として扱う
- [ ] F2: Web UI で同期ボタンが `pocket_fingerprint` を付けて送信し、`/api/pocket/job` の fingerprint が変わったら曲ごとの状態を再読込することを、手動確認またはテンプレートの静的確認で検証する
- [ ] F3: `http_proxy` を設定しても `PocketHTTPClient` はプロキシへ接続しないことをテストで検証する
- [ ] F4: 棚が `NOPE\r\n\r\n` を返したとき、`bunri pocket connect` と `bunri pocket delete` はトレースバックを出さず整形されたエラーで終了し、`GET /api/pocket/status` は 200 で `state: unknown` を返す
- [ ] F5: 棚由来の title に `[bold red]...[/]`、`[/x]`、`\x1bc`、`\x1bM` を含めても、一覧と確認表示はそれらを解釈も素通しもせず、コマンドはクラッシュしない
- [ ] F6: README の約束の文言が方針の内容に更新されている
- [ ] 作業ブランチ `plan/2026-09-12-pocket-review-fixes` へ commit 済み。変更対象を明示して staging し、計画書（`plan.md` と `request.md`）、報告書、PoC テストを含める。個人環境のパス、秘密情報、私的リンクを含まない

## 未確定事項・リスクと判断の委ね方

| 項目 | 内容 | 実装時の扱い |
|---|---|---|
| 既存テストの影響範囲 | fingerprint 必須化で `POST /api/pocket/sync` を呼ぶ既存テストが複数ある | テストの意図を保って fingerprint を付ける。挙動の期待値そのものを変える必要が出たら止まって報告する |
| ffmpeg 引数の位置 | `-map_metadata -1` は出力オプション | 出力ファイル名の直前に置く。テストが引数リストを厳密に比較しているなら期待値を更新してよい |
| `-vn` の要否 | normalize の WAV 出力に映像ストリームは載らない | 明示のため normalize 側にだけ付ける。ffmpeg がエラーを返す入力があれば報告する |
| C0 制御文字の置換文字 | U+FFFD か空文字か | U+FFFD を採用する。ヘルパーの名前と配置は実装担当の裁量 |
| ページ側の fingerprint 変化検知 | ポーリング頻度と再読込の競合 | 既存の `pocketStatusGeneration` の仕組みを使い、多重呼び出しを避ける。UI 文言は既存のトーンに合わせる |
| 旧版の queued 同期記録 | fingerprint なしの記録が隔離される | 想定どおり。互換処理は追加しない |
| 止まって報告する条件 | 依存の追加、プロトコルの変更、対象外ファイルの変更、受け入れ基準の変更が必要になった場合 | 実装を進めず、選択肢と推奨を報告する |

## 公開経路

作業ブランチ `plan/2026-09-12-pocket-review-fixes` にローカル commit するまでが承認範囲である。push、PR 作成、main への統合、リリースタグは未承認で、検収後に別途判断する。自動デプロイはない。CI は push 時に `uv run pytest -q -n auto` を実行するのみである。独立実装レビューは commit 後に采配役が gpt-6-astra / high の新規セッションで行い、実装担当は起動しない。
