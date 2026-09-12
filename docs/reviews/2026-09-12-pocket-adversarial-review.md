# Bunri Pocket 導線の敵対的レビュー(2026-09-12)

対応計画: `docs/plans/2026-09-12-pocket-review-fixes/plan.md`

対象: `bunri pocket` の全サブコマンドと Web UI の同期・削除導線(v0.5.0、main `aa8f10f`)。
分離処理・出力先配下のパス操作・Web UI の CSRF 基盤は 2026-08 レビュー済みのため、その後に追加された pocket 導線に範囲を絞った。
PoC は `tests/test_pocket_security_review_poc.py` にある。`test_poc_*` は現状の実装に対して失敗する(=所見の再現)。`test_guard_*` は疑った経路が閉じていることの証跡で、現状すべて通る。

実行結果: 11 failed(所見)、7 passed(ガード証跡)。

## 1. 導線の棚卸し

| 導線 | 読むもの | 送るもの | 消すもの |
|---|---|---|---|
| `pocket connect URL` | URL 引数、token(プロンプト or `--token-stdin`) | `GET upload/capabilities`(Bearer) | 既存 `out/.pocket/config.json` を原子的に置換 |
| `pocket sync SAFE_NAME` | `out/.pocket/config.json`、`out/<name>/.bunri-package.json`、同ディレクトリの `*.original.mp3` / `*.<target>.mp3` / `*.<target>.backing.mp3`(SHA-256 計算) | `GET manifest/<id>`、`GET library`、`HEAD/PUT media/<id>/<name>`、`PUT manifest/<id>`、`PUT library` | なし |
| `pocket sync --all` | 上記を `out/` 直下の全パッケージ分(symlink 除外) | 同上をパッケージごとに逐次 | なし |
| `pocket delete SAFE_NAME` | config、対象パッケージの sidecar(song ID と digest の再確認) | `DELETE api/v1/tracks/<id>` | 棚の track のみ(ローカルは消さない) |
| `pocket delete --song-id` | config | 同上 | 同上 |
| `pocket delete --select` | config、`GET library`(一覧表示) | 同上 | 同上 |
| `GET /api/pocket/status` | config、全パッケージ(SHA-256 計算)、`GET manifest`/`GET library`/`HEAD media` | 棚への読み取りのみ | なし |
| `GET /api/pocket/job` | config、ジョブ記録 | なし | なし |
| `POST /api/pocket/sync/{id}` | config(存在確認のみ)、パッケージ | ジョブ実行時に `sync_one`(上記 sync と同じ) | なし |
| `POST /api/pocket/sync` | config(存在確認のみ) | ジョブ実行時に `sync_all` | なし |
| `DELETE /api/songs/{id}?pocket=true&pocket_fingerprint=` | config(fingerprint 照合)、パッケージ sidecar | `DELETE api/v1/tracks/<id>`(fingerprint を実行時に再照合) | 棚の削除成功後に既存の `delete_song`(パッケージ、ジョブ記録、ログ、共有していない upload と cache) |
| `DELETE /api/songs/{id}` | ジョブ記録、パッケージ | なし | 既存の `delete_song` |

## 2. 所見(確信度 0.7 以上)

### F1. 入力ファイルのタグと ffmpeg のビルド文字列が original.mp3 に乗って送信される

- 場所: `src/bunri/audio.py:24`(normalize)、`src/bunri/audio.py:48`(encode)、`src/bunri/package.py:268`(original.mp3 の書き出し)
- 深刻度: Medium(約束との不一致)
- 確信度: 0.95
- 再現: `ffmpeg -f lavfi -i sine=... -metadata comment="purchased by alice@example.com" -metadata artist=Alice -c:a aac input.m4a` を `bunri input.m4a` で分離し、`out/input/input.original.mp3` を `ffprobe -show_entries format_tags` で見る。`artist` / `comment` / `album` / `title` と `encoder=Lavf62.12.102` がそのまま入っている。`bunri pocket sync input` は既定で original.mp3 を送る(Web UI は常に送る)。
- 影響: README は「分離後の MP3 が送信される」としているが、送られる MP3 の中に入力ファイル由来のタグ(購入者情報がコメントに入る配布形態がある)と、ffmpeg のバージョン文字列(環境情報)が含まれる。stem 側の MP3 も `encoder` タグを持つ。
- PoC: `test_poc_original_mp3_carries_no_input_tags_or_encoder_version`
- 修正案: 両方の ffmpeg 呼び出しに `-map_metadata -1 -fflags +bitexact -flags:a +bitexact`(normalize 側は `-vn` も)を付ける。この組み合わせで `format_tags` が空になることを確認済み。キャッシュ済み `input.wav` にはタグが残るので、`_normalize_step` のバージョンを上げて再生成させるか、encode 側だけでも落とす。

### F2. Web の同期ジョブが「画面で確認した棚」に束縛されていない

- 場所: `src/bunri/web/app.py:595-647`(POST が fingerprint を受け取らない)、`src/bunri/web/jobs.py:2706-2722`(実行時に config を読み直す)
- 深刻度: Low
- 確信度: 0.9
- 再現: 分離ジョブが worker を占有している間に画面から「アップロード」を押す(202 で queued)。その間に `bunri pocket connect https://shelf-b ... -o out` で接続先を変える。分離が終わると queued の同期ジョブは shelf-b にアップロードする。画面のバナーと曲ごとの `未同期` 判定は shelf-a に対するものだった。
- 影響: 削除は `pocket_connection_fingerprint` で束縛され実行時にも再照合されるが、同期(個別・全曲とも)は束縛がない。送信先は利用者が自分で connect した棚なので「本人所有」の約束は破れないが、「確認した棚と同一か」は保証されない。
- PoC: `test_poc_queued_web_sync_uploads_to_the_shelf_the_page_showed`
- 修正案: 削除と同じく POST に `pocket_fingerprint` を必須にし、ジョブ記録に保存して `_run_pocket_job` で `sync_one` / `sync_all` の前に `connection_fingerprint(read_config(...))` と照合する。ページ側は `/api/pocket/job` の fingerprint 変化でバナーと状態を無効化する。

### F3. `http_proxy` 環境変数で loopback 宛の平文リクエストが(トークンごと)プロキシへ流れる

- 場所: `src/bunri/pocket/http.py:62`(`build_opener` が既定の `ProxyHandler` を含む)、`src/bunri/pocket/config.py:44`(http は loopback 限定という前提)
- 深刻度: Low
- 確信度: 0.8
- 再現: `http_proxy=http://proxy.example:3128` を設定した環境で `bunri pocket connect http://localhost:8787` を実行する。urllib は `no_proxy` 未設定なら localhost も環境プロキシへ送る(macOS でも環境変数があればシステムの bypass 設定は使われない)。PoC はローカルの偽プロキシで `Authorization: Bearer <token>` の受信を確認する。
- 影響: 「http は loopback だけ」の意図はトークンをホスト外に出さないことだが、プロキシ設定があると平文でホスト外へ出る。https の場合は CONNECT 経由でトークンは TLS 内に留まる。
- PoC: `test_poc_http_proxy_env_does_not_carry_loopback_token_offhost`
- 修正案: `build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))` として環境プロキシを無視する。プロキシ対応が必要なら https のみ許可し、http の loopback は常に直結にする。

### F4. 棚からの不正な応答行が例外処理をすり抜けて生トレースバックになる(locals 表示時はトークンが出る)

- 場所: `src/bunri/pocket/http.py:75-77`(`_request` の locals に `request_headers`)、`src/bunri/pocket/cli.py:51,117,182,212`(捕捉が `OSError, RuntimeError, ValueError` のみ)、`src/bunri/pocket/service.py:322`、`src/bunri/web/app.py:517`
- 深刻度: Low(現行ピンでは情報漏えいなし。トレースバックにローカルパスが出る)
- 確信度: 0.9(トレースバック)、0.9(locals 有効時のトークン表示)
- 再現: HTTP の代わりに `NOPE\r\n\r\n` を返すサーバーへ `bunri pocket connect` または `bunri pocket delete --song-id ... --yes`。`http.client.BadStatusLine` は `HTTPException`(`Exception` 直系)なので全 `except` を素通りし、rich のトレースバックが出る。同じ例外を `/api/pocket/status` で起こすと 500 になる(他の通信エラーは `state: unknown` に丸められるのに)。`pretty_exceptions_show_locals=True` にすると `_request` フレームの `request_headers` として Bearer トークンが端末に出る。uv.lock の Typer 0.26.8 は既定 False なので現状は出ない。
- 影響: 端末のトレースバックはバグ報告に貼られやすい。Typer の既定が変わる、あるいは開発者が locals を有効にした瞬間にトークンが出る構造になっている。`IncompleteRead` など他の `HTTPException` も同じ経路。
- PoC: `test_poc_cli_transport_garbage_is_reported_without_traceback[connect|delete]`、`test_poc_cli_transport_garbage_never_prints_token_even_with_locals`、`test_poc_pocket_status_reports_unknown_on_malformed_remote_status_line`
- 修正案: `_request` で `self._opener.open` と `response.read` を `except http.client.HTTPException as exc: raise PocketHTTPError(0, "TRANSPORT", url) from None` のように包む(トークンを持つフレームで例外を閉じる)。加えて両 Typer app に `pretty_exceptions_show_locals=False` を明示する。

### F5. `delete --select` の一覧が棚側の title で偽装できる(rich markup と端末エスケープが素通し、閉じタグでクラッシュ)

- 場所: `src/bunri/pocket/cli.py:174`(一覧)、`src/bunri/pocket/cli.py:190`(確認表示)
- 深刻度: Low
- 確信度: 0.9
- 再現: library の title に `[bold red]Styled[/]` を入れると装飾として解釈される。`[/x]` を入れると `MarkupError` で落ちる。`\x1bc`(端末リセット)や `\x1bM`(逆改行)を含む title はそのまま端末へ出る。rich が落とす制御文字は BEL/BS/VT/FF/CR だけで、ESC は通る。
- 影響: 一覧の見た目を操作できるのは棚に library を書ける者(トークン保持者か棚の運用者)なので、脅威主体は限定的。選択番号と削除対象の対応自体(`tracks[choice-1].song_id`)はずれない。ただし「一覧表示と削除対象が一致する」保証は表示層で崩せる。Web UI は `textContent` で描画しており同種の問題はない。
- PoC: `test_poc_select_listing_does_not_pass_terminal_escapes_from_remote_titles[RIS-reset|reverse-index]`、`test_poc_select_listing_does_not_interpret_remote_titles_as_markup`、`test_poc_select_listing_does_not_crash_on_unbalanced_remote_markup`
- 修正案: 棚由来の文字列は `console.print(..., markup=False, highlight=False)` で出し、`\x1b` と C0 制御文字を事前に置換する(`rich.markup.escape` だけでは ESC は残る)。確認表示の `曲名:` も同じ扱いにする。

### F6. 約束の書き分け: 「分離後の MP3」に原曲 MP3 が含まれることと、入力ファイルの SHA-1 が manifest に載ることが明記されていない

- 場所: `README.md:3,9`(約束の文言)、`src/bunri/pocket/cli.py:64`(`--original` 既定 True)、`src/bunri/web/jobs.py:2706-2722`(Web は常に original を送る)、`src/bunri/pocket/protocol.py:485`(`source.digest` 40 桁)
- 深刻度: Info(約束との不一致として報告)
- 確信度: 0.8
- 再現: `bunri pocket sync 曲名` を既定で実行すると `original.mp3`(原曲の再エンコード)が送られる。Web UI には除外手段がない。manifest の `source.digest` は入力ファイル全体の SHA-1(40 桁)で、識別に使うのは先頭 12 桁だけ。
- 影響: 「入力音源は外部に送信されない」と「分離後の MP3 が送られる」の間にある「原曲を MP3 にしたものは送られる」が読み取りにくい。SHA-1 は入力ファイルの正確な指紋なので、同一ファイルの照合に使える。
- PoC: なし(仕様の記述の問題)
- 修正案: README 冒頭の約束を「原曲を再エンコードした MP3 を含む」と明記し、Web UI に original を除外する選択肢を置くか、除外できないことを書く。manifest の digest は先頭 12 桁だけで運用できないかプロトコル側と相談する(v1 では 40 桁必須)。

## 3. 疑ったが成立しなかった経路(確信度 0.7 未満、または閉じていることを確認)

- トークンの露出: ログ、ジョブ JSON、`/api/pocket/*` と `/api/jobs` `/api/songs` の応答、プロセス引数、環境変数に出ない(`test_guard_pocket_status_and_job_responses_never_contain_token_or_url`)。一時ファイルは `mkstemp` + `fchmod 0600` で作られ `finally` で unlink。`connect` のやり直しは `os.replace` で旧トークンが残らず、既存 0644 の config も 0600 に締め直す(`test_guard_connect_rewrites_config_atomically_with_0600_and_drops_old_token`)。
- 接続先の固定: 3xx は `_NoRedirect` で追従せずエラー(既存 `test_redirects_are_never_followed`)。自己署名証明書は TLS ハンドシェイクで拒否され、リクエストが送られる前に止まる(`test_guard_self_signed_certificate_is_rejected_before_any_request`)。http は loopback 限定(既存テスト)。削除は CLI・Web とも fingerprint を取得時に固定し、ロック内で config を読み直して再照合する。`connect` はロックを取らないが書き込みは原子的で、削除側の再照合で検出される。
- CSRF: `POST /api/pocket/sync`、`POST /api/pocket/sync/{id}`、`DELETE /api/songs/{id}`(pocket あり・なし)は Origin 不一致と `Origin: null` で 403。`GET /api/pocket/status` と `/api/pocket/job` は出力先を一切変更せず `sync.lock` も作らない(`test_guard_pocket_get_endpoints_do_not_mutate_output_dir`、`test_guard_pocket_mutations_reject_cross_origin_requests`)。Origin 欠落を許すのは設計どおりで、ブラウザのページからは欠落させられない。
- 既存ガードの迂回: sync が読むパッケージは `inspect_package_identity` が symlink ディレクトリを除外し、`inspect_artifact` が symlink ファイルを拒否する(`test_guard_sync_never_reads_through_symlinks`)。Web「棚からも削除」のローカル削除は既存の `delete_song` を通り、`validate_output_targets` と symlink 不追従が効く。`saved_package_name` 経路も symlink と親ディレクトリを検証する。ハードリンクは拒否されないが、同一ユーザーが読める範囲に限られ、コピーと等価なので約束の迂回にはならない(確信度 0.3)。
- NFC/NFD: macOS(APFS)は正規化を区別しないため NFC と NFD の selector は同じディレクトリと同じ song ID に解決する(`test_guard_nfc_and_nfd_selectors_resolve_to_the_same_song_id`)。区別するファイルシステムでは NFC キーの重複を競合として同期・削除を止める(既存 `test_pocket_service`)。`--select` は取得した同じタプルから番号で song ID を引くので対象はずれない(表示の偽装は F5)。
- CLI `sync` の完了メッセージ: `src/bunri/pocket/cli.py:70` で読んだ config を表示に使い、実際の送信は `service.sync_one` が再読込した config を使う。その隙間に connect すると表示と送信先が食い違う(確信度 0.4、影響は表示のみ)。
- fingerprint が `sha256(base_url)` で salt なし: 同一オリジン・ローカルにしか出ず、トークンとは無関係。トークンだけ差し替えた connect は「接続先変更」として検出されないが、同じ URL の棚に本人が接続し直した状態であり約束の範囲内(確信度 0.3)。
- `updated_at`: `datetime.now(timezone.utc)` を `Z` 付きで出し、タイムゾーンオフセットは出ない。

## 4. 送信内容の全フィールドと由来

### manifest(`PUT upload/manifest/<song_id>`)

| フィールド | 値の由来 |
|---|---|
| `schema_version` | 定数 `"1.0"` |
| `song_id` | sidecar `source.cache_key`(入力ファイル SHA-1 の先頭 12 桁) |
| `title` | sidecar `title`。CLI は `--title` か入力ファイル名の stem、Web はフォームの title かアップロードファイル名の stem |
| `source.algorithm` | 定数 `"sha1"` |
| `source.digest` | 入力ファイル全体の SHA-1(40 桁) |
| `source.cache_key` | 同上の先頭 12 桁 |
| `original` | `null` または `{path:"original.mp3", content_type:"audio/mpeg", bytes, sha256}`。bytes/sha256 はローカル MP3 から計算 |
| `instruments[].target` | sidecar `targets[].target`(レジストリのキー、例 `guitar`) |
| `instruments[].label` | `REGISTRY[target].label_ja`(Bunri 内蔵の日本語ラベル) |
| `instruments[].stems[].role` | 定数 `target` / `backing` |
| `instruments[].stems[].path` | `<target>.mp3` / `<target>.backing.mp3`(固定名。ローカルの safe name は含まない) |
| `instruments[].stems[].content_type` | 定数 `audio/mpeg` |
| `instruments[].stems[].bytes` / `sha256` | ローカル MP3 から計算 |
| `updated_at` | 送信時刻(UTC、`Z`、秒精度)。内容が変わらなければ棚側の値を維持 |
| その他 | 棚にある既存 manifest の未知フィールドは deepcopy で保持(ローカル由来ではない) |

含まれないもの: 出力先のパス、パッケージディレクトリ名(safe name)、OS ユーザー名、ホスト名、Bunri の設定値、モデル名やモデルのハッシュ、分離に使ったデバイス、タイムゾーン。

### library(`PUT upload/library`)

| フィールド | 値の由来 |
|---|---|
| `schema_version` | 定数 `"1.0"` |
| `songs[].song_id` | manifest と同じ |
| `songs[].title` | manifest と同じ |
| `songs[].manifest` | `tracks/<song_id>/manifest.json`(固定形式) |
| `songs[].has_original` | manifest の `original` が null でないか |
| `songs[].instruments[].target` / `label` | manifest と同じ |
| `songs[].updated_at` | manifest の `updated_at` |
| `updated_at` | 送信時刻(UTC) |
| 他の曲 | 棚にある既存 library の内容を保持 |

### media(`PUT upload/media/<song_id>/<name>`)

| 内容 | 由来 |
|---|---|
| 本体 | `original.mp3`(原曲を再エンコード。既定で送る)、`<target>.mp3`、`<target>.backing.mp3` |
| ID3 タグ | 入力ファイルのタグ(title/artist/album/comment 等)が original.mp3 に残る(F1)。全 MP3 に `encoder=Lavf<ffmpeg version>` |

### HTTP ヘッダとクエリ(全リクエスト共通、実測)

| ヘッダ | 値の由来 |
|---|---|
| `Host` | 設定した base_url の host:port |
| `Authorization` | `Bearer <upload token>` |
| `User-Agent` | `bunri/<バージョン>`(OS や Python のバージョンは含まない) |
| `Accept-Encoding` | urllib 既定 `identity` |
| `Connection` | urllib 既定 `close` |
| `Content-Type` | JSON PUT: `application/json`、media PUT: `audio/mpeg` |
| `Content-Length` | 送信バイト数 |
| `If-Match` / `If-None-Match` | 棚から受け取った ETag、または `*` |
| `X-Bunri-Content-SHA256` | media PUT のみ。ローカル MP3 の SHA-256 |
| クエリ文字列 | なし。パスは `api/v1/upload/{capabilities,library,manifest/<id>,media/<id>/<name>}` と `api/v1/tracks/<id>` のみ |

ログ: pocket ジョブはログファイルを作らず、ジョブ JSON には song ID、digest、safe name、fingerprint、件数だけが入る。CLI の出力に送信内容の全文は出ない(F4 のトレースバックを除く)。
