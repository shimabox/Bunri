# 音源ポケットへの接続と閲覧パスワード再設定 実装計画

- 日付: 2026-10-12
- リポジトリ: github.com/shimabox/Bunri
- ベースブランチ: main
- ベース SHA: 3f628a3ec5ccc03fd55278850b9e41aa1e823a31
- 作業ブランチ: plan/2026-10-12-pocket-reset-password
- 起案担当: 予定 claude-fable-5-1 / high。確認済み: 実モデル claude-fable-5-1(CLI の実行結果のモデル別使用量で確認)、effort は実行結果から取得できず未確認(要求は high)
- 計画レビュー担当: 予定 gpt-6-astra / high(未実行)
- 編成の例外理由: なし
- 実装担当: claude-opus-5-5 / high(Web の入口・画面・暗号の導出・テストにまたがる実装で、推論を要するため通常段の担当を使う)
- 実装レビュー担当: 予定 gpt-6.1-sol / high(通常段)。往復が上限に達した場合は、実装 claude-fable-5-1 / high、レビュー gpt-6-astra / high へ上げる
- 計画書の扱い: レビューと共有内容の検査の後、実装と同じ作業ブランチに commit する(`docs/plans/2026-10-12-pocket-reset-password/plan.md` と `docs/plans/2026-10-12-pocket-reset-password/request.md`)
- 公開範囲: 実装担当は作業ブランチへの commit まで。push、PR の作成、リリース(v0.9.0 の版数の書き換えとタグ)は、結果の報告と了解の後に別途行う。本物の音源ポケットに対する実行は、了解がある場合だけ行う

## 背景・目的

Bunri Pocket(音源ポケット、`github.com/shimabox/bunri-pocket`)0.4.0 は、upload token で認証して閲覧パスワードだけを決め直す `POST /api/v1/password/reset` を提供する。閲覧パスワードを忘れた所有者が、Bunri に保存済みの upload token を使い、曲と upload token を残したまま復旧できるようにする。

Bunri 側には次を 1 つの計画で加える。

- CLI `bunri pocket reset-password`。契約(bunri-pocket v0.4.0 の `docs/protocol-v1.md`「認証」節)のクライアント手順をそのまま実装する。
- Web UI(ローカル画面)からの音源ポケットへの接続、接続先の変更、接続の解除。いまは CLI の `bunri pocket connect` と、`.pocket` を手で消す操作でしかできない。
- Web UI からの閲覧パスワードの再設定。
- CLI `bunri pocket connect` の upload token の入力を、端末からの非表示入力だけにする(`--token-stdin` を外す)。秘密の入口を端末と Web 画面の 2 つにそろえる。

CLI と Web の両方で、再設定の要求を送った後に応答が返ってこなかった場合は、認証なしの `GET /api/v1/auth/params` の `kdf.salt` を今回生成した salt と照合し、「完了した / 完了していない / 確認できなかった」を区別して伝える。

## スコープ

- 対象:
  - `bunri pocket reset-password`(対話の非表示入力のみ、`--yes`、`-o`)。
  - PBKDF2-HMAC-SHA256 による derived key の導出、salt の生成、契約どおりの要求本文の組み立て(標準ライブラリのみ)。
  - capabilities の `features.password_reset` による対応判定と、未対応・判定不能の案内。
  - HTTP クライアントへの `reset_password`(204 の厳密確認)と `auth_params`(Bearer を付けない GET)の追加。
  - 応答喪失時の結果確認(salt の照合、待って再確認、3 つの結果の区別)。CLI と Web で同じ関数と同じ文言を使う。
  - CLI `connect` から `--token-stdin` を削除し、upload token の入力に端末(TTY)を必須にする。既存テスト 3 件の書き換えと、非 TTY のテストの追加。
  - Web API `POST /api/pocket/connect`、`POST /api/pocket/password-reset`、`DELETE /api/pocket/connection`、およびこの 3 つに限った Origin 必須の守り(本文のある 2 つの POST は JSON も必須)。
  - `src/bunri/pocket/config.py` への `delete_config` の追加。
  - Web 画面: ヘッダーの接続状態表示、「音源ポケットの設定」のダイアログ、接続ダイアログ、再設定ダイアログ、接続解除の確認ダイアログ、ブラウザ内(WebCrypto)での鍵の導出。
  - テスト: 関数単体、偽サーバー、CLI 経由、Web API(TestClient)、実ブラウザ(Playwright + Chromium。公開テスト値との一致を含む)。
  - README「Web UI で使う」節と「Bunri Pocket へ同期する」節への追記。
- 対象外:
  - `reset-password --password-stdin`(設けない)。
  - CLI `connect` の検査・保存の手順の書き換え(オプションの削除と端末入力の検査だけを行う)。
  - 本物の音源ポケットに対する実行(テストでも行わない)。
  - 契約スナップショット(`tests/fixtures/bunri_pocket_protocol_v1/`)の 0.4.0 への更新。
  - 版数の書き換え、タグ、push、PR、リリース。
  - upload token の再発行や login 機能。
  - メモリ上の秘密のゼロ消去の保証。
  - 既存の同一オリジン強制(Origin 欠落を通す)の変更。新しい 3 つの入口だけを厳しくする。
  - `docs/plans/` と `docs/reviews/` にある過去の文書の `--token-stdin` の記述の変更(当時の記録なので変えない)。

## 方針

### 主要な契約・不変条件

- 平文のパスワードは Bunri の外へ送らない。音源ポケットへ送るのは `kdf`(name / iterations / salt / dk_len の 4 キー)と `derived_key` の 2 キーだけ。Web では平文のパスワードはブラウザの中にとどまり、Bunri サーバーにも送らない。
- KDF は固定値: PBKDF2-HMAC-SHA256、600,000 iterations、32 byte、salt は暗号学的乱数 16 byte。salt は再設定ごとに新規に生成し、`auth/params` の現在値は使い回さない。
- base64url は padding なし。受け取る値は正準形だけを受け付ける(再エンコードして同じ文字列になるもの)。
- 要求ヘッダー: `Authorization: Bearer <token>`、`User-Agent: bunri/<version>`、`Content-Type: application/json`、`Content-Length`(本文の byte 数)。`Origin` と `Cookie` は送らない。
- 成功は status 204 だけ。それ以外の 2xx は失敗として扱う(`UNEXPECTED_RESPONSE`)。`http.client` は 204 の応答の本文を読まないため、204 に本文が付いていても検出できない。これは検査の対象にしない。
- 再設定が成功すると全端末がログアウトされる。曲、upload token、Bunri の接続設定は変わらない。直前の login 失敗の backoff(最大 60 秒)は残る。
- 秘密(パスワード、UTF-8 化したパスワード、derived key、要求本文、upload token)は、CLI の画面、Web の応答、HTML、ジョブの記録、ログ、例外メッセージ、引数、環境変数へ出さない。
- 接続先 URL は秘密ではないが、Web の応答、HTML、ジョブの記録、ログ、例外メッセージには出さない(既存の Web API の方針)。接続の識別には `connection_fingerprint`(URL の SHA-256)だけを画面へ渡す。CLI は、既存の `connect` / `sync` と同じく、利用者が指定した接続先 URL を端末に表示してよい(`reset-password` は実行前の確認で表示する)。
- 秘密の入口は、端末の非表示入力と Web 画面の入力欄だけにする。パスワードも upload token も、コマンド引数・環境変数・標準入力(パイプ)からは受け取らない。
- 状態の断定の規則:
  - 「閲覧パスワードは変わっていません」と断定するのは、音源ポケットが 4xx の応答を返した場合(契約で書き込み前に拒否される)、要求を送る前に失敗した場合、または `auth/params` の照合で salt の不一致を確認した場合だけ。Bunri が応答を受け取った後の検査で付ける code(`UNEXPECTED_RESPONSE`、`RESPONSE_TOO_LARGE`)は、status が 4xx でも音源ポケットの拒否ではないので、ここに含めない。
  - 「完了した」と伝えるのは、204 を受け取った場合、または `auth/params` の salt が今回の salt と一致した場合だけ。
  - それ以外(通信失敗、503 以外の 5xx、照合の問い合わせ自体が失敗)は「確認できなかった」。
- Bunri の Web サーバーは 127.0.0.1 専用で認証が無い前提を変えない。接続・再設定・接続解除の入口は、Origin が存在し request URL の origin と正確に一致するときだけ処理する。本文のある接続と再設定の入口は、加えて `Content-Type` が `application/json` であるときだけ処理する。
- 接続の解除で消すのは、出力先の `.pocket/config.json` だけ。音源ポケットにある曲と、手元の曲は消さない。
- 依存パッケージを追加しない。ブラウザ側は外部の資源を読み込まず、KDF は `crypto.subtle` で導出する。

### 確定した設計

#### 1. CLI: パスワードの入力方法

- 対話入力だけ。`typer.prompt(..., hide_input=True)` を 2 回(「新しい閲覧パスワード」「新しい閲覧パスワード(確認)」)。typer の `confirmation_prompt` は英語の文言になるため使わず、自前で 2 回出す。不一致なら「2 回の入力が一致しません。閲覧パスワードは変わっていません。」で終了コード 1。
- stdin が TTY でないときは何も読まず「閲覧パスワードの入力には TTY が必要です。端末から実行するか、Web UI から再設定してください。」で終了コード 1。`--yes` があっても同じ(`--yes` は最終確認の省略だけに効く)。getpass の非 TTY フォールバックに頼らない。
- `--password` のようなコマンド引数の経路、環境変数の経路、`--password-stdin` は設けない。`--help` に `--password` が現れないことをテストで固定する。

#### 2. 長さ検査、正規化、UTF-8 化(CLI とブラウザで同じ規則)

- 12 Unicode コードポイント以上。Python は `len(str)`、ブラウザは `Array.from(value).length`。未満なら「閲覧パスワードは 12 文字(Unicode コードポイント)以上にしてください。」
- Unicode 正規化(NFC / NFD)は行わない。Python は `str.encode("utf-8")`(strict)、ブラウザは `TextEncoder`。
- UTF-8 にできない入力(孤立 surrogate)は Python では `UnicodeEncodeError` を捕捉し、文字を含まない固定文言「パスワードに UTF-8 にできない文字が含まれています。」で失敗する。
- 上限は設けない。制御文字や NUL は禁止しない。前後の空白は保つ(`validate_token` の `strip()` とは意図的に異なる。音源ポケットの PWA の入力欄と byte 単位で一致させるため)。

#### 3. CLI: 実行前の確認と実行順

- `delete` と同じ形式で、接続先 URL と効果(全端末ログアウト、曲と token は残る)を表示してから `typer.confirm("この操作を続けますか？", default=False)`。接続先 URL は `_safe_display` を通し、`markup=False, highlight=False` で表示する。`--yes` / `-y` で省略。TTY でなく `--yes` も無い場合は確認入力を読まず失敗する(ただし 1 の TTY 検査が先に走るため、実際には TTY でない時点で止まる)。
- 確認はパスワード入力の前に行う。辞退時に秘密をメモリに置かずに済む。辞退時は「閲覧パスワードの再設定を取り消しました。」で終了コード 0。
- 実行順: 設定読込 → TTY 検査 → capabilities 取得と対応判定 → 確認 → パスワード入力と検査 → salt 生成と導出 → POST → (応答喪失なら照合)→ 結果表示。capabilities を先に見るのは、token 無効や未対応をパスワードを打つ前に知らせるため。
- 接続設定が無いときは「Pocket の接続設定がありません。」、設定を読めない・壊れているときは「Pocket の接続設定を確認できません。」で終了コード 1(`delete` と同じ文言)。どちらも `PocketHTTPClient` を作らない。

#### 4. capabilities の判定

- `protocol.py` に `password_reset_supported(capabilities: object) -> bool` を追加する。`src/bunri/pocket/protocol.py` の `pan_split_supported` と同じ形: `capabilities` が dict、`api.major` が bool でない int の 1、`features` が dict、`features["password_reset"] is True` のときだけ True。
- 3 つの結果を区別する(CLI と Web で共通)。
  - 対応: 続行。
  - 未対応(capabilities は JSON object だが条件を満たさない): 「この音源ポケットは閲覧パスワードの再設定に未対応です。音源ポケットを 0.4.0 以降へ更新してください。閲覧パスワードは変わっていません。」
  - 判定不能(object でない、取得に失敗): 「音源ポケットの対応状況を確認できませんでした。」に原因別の案内を添える。未対応とは言わない。
- `validate_capabilities` は通さない(upload 契約の検査であり、復旧操作を upload の limits の変化で止めない。`sync` の `probe_features` も通していない前例に合わせる)。

#### 5. HTTP クライアント(`http.py`)

- `_request` に `authenticated: bool = True` を加え、False のとき `Authorization` ヘッダーを付けない。既存の呼び出しは変えない。
- `reset_password(self, body: bytes) -> None`:
  - 送る前に `len(body) <= 4096` を検査し、超えていれば `PocketHTTPError(413, "PAYLOAD_TOO_LARGE", url)`。
  - `self._request("POST", "password/reset", data=body, headers={"Content-Type": "application/json", "Content-Length": str(len(body))}, timeout=self.metadata_timeout, limit=JSON_LIMIT_BYTES, upload_route=False)`。
  - `status != 204 or body` なら `PocketHTTPError(status, "UNEXPECTED_RESPONSE", url)`。既存の `delete_track` と同じ形。204 で本文が空でなくなる経路は無いので、実際に効くのは status の検査である。
  - 応答の本文が上限(`limit`)を超えた場合、既存の `_request` が `PocketHTTPError(413, "RESPONSE_TOO_LARGE", url)` を出す。これは要求を送った後に起きる。`reset_password` はこの挙動を変えない。
  - `Content-Type` を明示しないと urllib が `application/x-www-form-urlencoded` を補うため必ず明示する。timeout は属性 `metadata_timeout` を使う(既存 `delete_track` は定数を直接使っておりテストで短縮できない。新規メソッドでは属性を使う)。
- `auth_params(self) -> object`:
  - `self._request("GET", "auth/params", authenticated=False, upload_route=False, timeout=self.metadata_timeout)`。認証なしの経路に upload token を送らないため Bearer を付けない。
  - 200 以外は `PocketHTTPError(status, "UNEXPECTED_STATUS", url)`。JSON として読めなければ `PocketHTTPError(422, "INVALID_DOCUMENT", url)`(`capabilities` と同じ流儀)。解釈は呼び出し側(`confirm_reset`)が行う。
- 本文の組み立てはクライアントの外(`password.py`)で行い、クライアントは bytes を受け取るだけにする。

#### 6. 共通処理の置き場所(`src/bunri/pocket/password.py`、新規)

定数: `KDF_NAME = "PBKDF2-HMAC-SHA256"`、`PBKDF2_ITERATIONS = 600_000`、`DK_LEN = 32`、`SALT_LEN = 16`、`MIN_PASSWORD_CODEPOINTS = 12`、`REQUEST_LIMIT_BYTES = 4096`、`CONFIRM_ATTEMPTS = 3`、`CONFIRM_INTERVAL_SECONDS = 5.0`(呼び出し時に読む。テストで 0 にする)。

関数と型:

- `PasswordResetError(RuntimeError)`。`kind` を持つ: `too_short` / `not_utf8` / `unsupported` / `undetermined` / `invalid_request`。メッセージは固定文言だけ。
- `validate_new_password(password: str) -> bytes`(長さ検査と UTF-8 化)。
- `generate_salt() -> bytes`(`secrets.token_bytes(16)`)。
- `derive_key(password_utf8: bytes, salt: bytes) -> bytes`(`hashlib.pbkdf2_hmac("sha256", ..., 600_000, dklen=32)`)。
- `b64url(data: bytes) -> str`、`decode_b64url(text: str, length: int) -> bytes`。後者は alphabet、`len % 4 != 1`、再エンコードの一致(正準形)、byte 長を検査し、違えば `PasswordResetError(kind="invalid_request")`。
- `build_reset_body(salt: bytes, derived_key: bytes) -> bytes`(`json.dumps(..., separators=(",", ":"), ensure_ascii=True)`、キー順は `kdf` → `derived_key`、`kdf` 内は name / iterations / salt / dk_len)。
- `parse_reset_request(value: object) -> tuple[bytes, bytes]`(Web 用。JSON object、top-level は `kdf` と `derived_key` の 2 キーだけ、`kdf` は 4 キーだけ、`name` / `iterations` / `dk_len` が固定値、`salt` が 16 byte、`derived_key` が 32 byte。違えば `invalid_request`)。
- `check_password_reset_support(capabilities: object) -> None`(未対応は `unsupported`、object でなければ `undetermined`)。
- `ResetOutcome`(frozen dataclass): `state: "completed" | "not_applied" | "unknown" | "retry_later"`、`via: "response" | "auth_params"`、`error: BaseException | None`(`repr=False`。文言生成にだけ使う)。
- `confirm_reset(client, salt: bytes, *, sleep=time.sleep) -> "completed" | "not_applied" | "unknown"`(7 に詳述)。
- `run_reset(client, salt: bytes, derived_key: bytes, *, sleep=time.sleep) -> ResetOutcome`: 本文組み立て → `client.reset_password` → 例外の分類(8 に詳述)→ 必要なら `confirm_reset`。CLI と Web の両方がこれを呼ぶ。
- `reset_with_password(client, password: str, *, sleep=time.sleep) -> ResetOutcome`(CLI 用。検査 → salt → 導出 → `run_reset`)。
- `safe_probe_error(exc) -> str`(capabilities 段階の文言)と `outcome_messages(outcome: ResetOutcome) -> list[str]`(結果の文言。CLI はそのまま 1 行ずつ表示し、Web は応答の `messages` として返す)。

新規モジュールにする理由: `service.py` は package / sync / delete の責務で既に大きく、KDF と本文の組み立ては単体で検証したい純粋な処理だから。`service.py` と `sync.py` は変更しない。`config.py` は、接続の解除のための `delete_config` の追加(20)だけを行い、既存の関数は変えない。

#### 7. 応答喪失時の結果確認(`confirm_reset`)

- 対象: POST が `OSError`(通信失敗、timeout、応答の解釈不能)、503 以外の 5xx(500、502、520 番台など)、または code が `UNEXPECTED_RESPONSE`(204 以外の 2xx)か `RESPONSE_TOO_LARGE`(応答の本文が上限超え)の `PocketHTTPError` で終わった場合。この 2 つの code は Bunri が応答を受け取った後に付けるもので、status に関係なく対象にする。それ以外の 4xx と 503 では照合しない(4xx は契約上「書き込み前に拒否」、503 は契約上「auth を変えずに返す」であり、保存系の障害時は照合の GET も失敗しやすく、待ち時間が増えるだけだから)。
- `GET /api/v1/auth/params` は認証なしの経路で、200 のとき次の形の JSON を返す。再設定が成功した直後は、`kdf.salt` が要求で送った salt になる。GET 以外は 405、認証情報が無い・壊れている場合は 503。

  ```json
  {
    "schema_version": 1,
    "kdf": {
      "name": "PBKDF2-HMAC-SHA256",
      "iterations": 600000,
      "salt": "<base64url>",
      "dk_len": 32
    }
  }
  ```

- 手順: `CONFIRM_ATTEMPTS` 回まで、`client.auth_params()` を呼ぶ。
  - 応答が JSON object で `kdf` が dict、`kdf["salt"]` が str で `decode_b64url` できる(長さは 16 byte 以上を許す)とき、decode した bytes が今回の salt と等しければ即「completed」。
  - 等しくなければ「不一致」として記録し、最後の試行でなければ `sleep(CONFIRM_INTERVAL_SECONDS)` して再試行する。
  - 応答が上の形でない、`PocketHTTPError`(503 や 404 を含む)、`OSError` の場合は「読めなかった」として記録し、同じく待って再試行する。
  - 全試行を終えたとき、最後の試行が「不一致」なら「not_applied」、それ以外なら「unknown」。
- 「不一致」で待って再確認するのは、クライアントの timeout の後にサーバー側で要求が完了する可能性(遅れて届く要求)を潰すためである。再設定の処理は R2 の読み書き数回と最大 8 回の CAS 再評価で終わるので、既定の 3 回・5 秒間隔(追加の待ちは約 10 秒)で十分とみなす。それでも残る「後から反映される」可能性は、再実行のときに同じパスワードを使えば結果に影響しないため、文言でそれを勧める。
- 比較は salt の bytes で行う。16 byte の乱数が偶然一致することは無いので、一致は「今回の要求が反映された」ことを意味する。別のクライアントによる再設定が後から上書きした場合は不一致になり「反映されていない」と出るが、その場合も現在のパスワードは利用者が今回決めたものではないので案内として正しい。
- 最大の待ち時間: POST の timeout 30 秒 + GET 3 回 × 最大 30 秒 + 待ち 10 秒。Web では画面に「時間がかかることがある」と示す。

#### 8. 例外の分類と文言(`run_reset` と `outcome_messages`)

probe 段階(`safe_probe_error`。要求を送る前なので末尾に「閲覧パスワードは変わっていません。」を付ける):

| 原因 | 表示 |
|---|---|
| 未対応(`unsupported`) | この音源ポケットは閲覧パスワードの再設定に未対応です。音源ポケットを 0.4.0 以降へ更新してください。 |
| 401 | upload token が無効です。音源ポケットで token を再発行した場合は接続し直してください。 |
| 404 | 接続先に音源ポケットの API が見つかりません。接続先 URL を確認してください。 |
| 429 | 音源ポケットが要求を制限しました。しばらく待って再実行してください。(Retry-After があれば秒数を添える) |
| 503 | 音源ポケットを利用できません。時間をおいて再実行してください。 |
| その他の HTTP エラー | 音源ポケットとの通信に失敗しました(HTTP {status} {code})。 |
| OSError | 音源ポケットに接続できませんでした。 |
| capabilities が object でない(`undetermined`) | 音源ポケットの対応状況を確認できませんでした。 |

reset 段階の分類(`run_reset`):

分類は、status より先に code を見る。code が `UNEXPECTED_RESPONSE` または `RESPONSE_TOO_LARGE` の `PocketHTTPError` は、status に関係なく「応答を確認できなかった」として `confirm_reset` に回す。この 2 つは Bunri が応答を受け取った後に付ける code で、契約上、音源ポケットは返さない。

| POST の結果 | state | via | 照合 |
|---|---|---|---|
| 204 | completed | response | しない |
| `PocketHTTPError` で code が `UNEXPECTED_RESPONSE`(204 以外の 2xx)または `RESPONSE_TOO_LARGE`(応答の本文が上限超え) | `confirm_reset` の結果 | auth_params | する |
| 上以外の 4xx(401 / 403 / 404 / 400 / 405 / 411 / 413 / 415 / 422 / 429 / その他。送信前に Bunri が止めた `PAYLOAD_TOO_LARGE` を含む) | not_applied | response | しない |
| 503 | retry_later | response | しない |
| 503 以外の 5xx、`OSError` | `confirm_reset` の結果 | auth_params | する |

`outcome_messages` の文言:

- completed / response:
  ```
  閲覧パスワードを決め直しました。
  音源ポケットにログインしていたすべての端末はログアウトされました。新しいパスワードで入り直してください。
  直前にログインに失敗していた場合は、待ち時間(最大 60 秒)が明けるまでログインできないことがあります。
  曲と upload token、Bunri の接続設定はそのままです。
  ```
- completed / auth_params: 先頭に「音源ポケットからの応答を確認できなかったため、結果を問い合わせました。再設定は完了していました。」を置き、続けて上の 2〜4 行目。
- not_applied / response: 下の表の 1 行と「閲覧パスワードは変わっていません。」
- not_applied / auth_params: 「音源ポケットからの応答を確認できなかったため、結果を問い合わせました。再設定は反映されていません。閲覧パスワードは以前のままです。」「同じ操作をもう一度実行してください。再実行では今回と同じパスワードを使ってください(遅れて届いた前回の要求が後から反映されても結果が変わらないようにするため)。」
- unknown / auth_params: 「音源ポケットからの応答を確認できなかったため、結果を問い合わせましたが、確認できませんでした。」「再設定が完了したかどうかは分かりません。新しいパスワードでログインできれば完了しています。古いパスワードで入れる場合は完了していません。同じ操作をもう一度実行しても安全です。再実行では今回と同じパスワードを使ってください。」
- retry_later(503): 「音源ポケットの認証情報または保存領域を利用できませんでした。時間をおいて同じ操作を再実行してください。」(「変わっていません」とも「完了しました」とも言わない)

not_applied / response の 1 行目(status と code で判定。`PocketHTTPError.user_message()` と `str(exc)` は使わない。既存の 422 文言が UNSUPPORTED_KDF を誤って説明するため):

| 原因 | 表示 |
|---|---|
| 401 | upload token が無効です。音源ポケットで token を再発行した場合は接続し直してください。 |
| 403 code=ORIGIN_MISMATCH | 音源ポケットが Origin の不一致として拒否しました。接続先 URL と音源ポケットの設定を確認してください。 |
| 403 その他 | 音源ポケットの手前で要求が拒否されました(HTTP 403)。接続先の保護設定を確認してください。 |
| 404 | 音源ポケットに再設定の経路がありません。音源ポケットを 0.4.0 以降へ更新するか、接続先 URL を確認してください。 |
| 422 code=UNSUPPORTED_KDF | この Bunri の鍵導出方式を音源ポケットが受け付けません。Bunri を更新してください。 |
| 400 / 405 / 411 / 413 / 415 / 422 その他 | 音源ポケットが要求の形式を拒否しました(HTTP {status} {code})。Bunri と音源ポケットの版の組み合わせを確認してください。 |
| 429 | 音源ポケットが要求を制限しました。しばらく待って再実行してください。(Retry-After があれば秒数) |
| その他の 4xx | 音源ポケットが要求を拒否しました(HTTP {status} {code})。 |

CLI の終了コード: completed は 0(via を問わない)、それ以外は 1。CLI は照合に入る前に「音源ポケットからの応答を確認できませんでした。音源ポケットに結果を問い合わせています…」を表示する。

#### 9. 排他ロック

- 再設定は `SyncLock` を取らない。変えるのは音源ポケットの `_system/auth.json` の 3 項目だけで、同期と削除は auth を読み書きせず upload token も変わらないため、競合する状態が無い。ロック中でも再設定できることを CLI と Web のテストで固定し、README に明記する。
- Web からの接続(設定の保存)は `SyncLock` を取る。実行中の同期・削除ジョブは `connection_fingerprint` で接続先に束縛されており、その最中に接続先を書き換えるのを防ぐ。取れなければ 409「別の Pocket 操作が実行中です。完了後に再実行してください。」で何も保存しない。
- Web からの接続の解除(設定の削除)も `SyncLock` を取る。実行中の同期・削除ジョブの接続先を足元から消さないため。取れなければ同じ 409 で何も消さない。

#### 10. 秘密の扱い

- Python で秘密を保持するのは `cli.py` のローカル変数、`password.py` の関数引数、Web の入口関数のローカル変数だけ。dataclass や属性に入れない(入れる必要があれば `field(repr=False)`)。
- 例外は固定文言だけを持つ。`UnicodeEncodeError` は捕捉して文字を含まない文言に差し替える。`PocketHTTPError` は既存どおり url と status / code しか持たず、Web の `detail` には url を出さない。
- `console.print` にパスワード、本文、token を渡す経路を作らない。Web の応答には、それらに加えて接続先 URL も渡さない。CLI のテストは全件で「出力にパスワード、UTF-8 化したパスワードの base64、derived key、upload token が含まれない」ことを、Web API のテストは全件で「応答に derived key、salt、upload token、接続先 URL が含まれない」ことを確かめる。
- 実装上の配慮として、派生物(UTF-8 bytes、derived key、本文)を作ったら元の参照を `del` し、POST の後に本文を `del` する。ゼロ上書きは求めず、完了条件にもしない(検証できない)。限界は README に書く。
- ブラウザ側: パスワードの入力欄は `type="password"`。導出が終わったら送信を待たずに入力欄の値を空にする。`console.log` に入力値や導出結果を出さない。JS の文字列は消せないので、同じ限界を README に書く。
- Web の接続では、upload token は要求本文として 127.0.0.1 の Bunri サーバーへ渡り、`save_config` で設定ファイルへ書かれる。応答、HTML、ログには出さない。

#### 11. 公開テスト値の使い方

bunri-pocket v0.4.0 の `fixtures/auth-v1/vectors.json` の 2 件を `tests/test_pocket_password.py` に直接書く(password、salt、iterations、dk_len、derived_key。出典として upstream の `fixtures/auth-v1/vectors.json` と版 0.4.0 をコメントに残す)。2 件目で「同じ password と salt から同じ derived key が出る」こと、1 件目で「別の salt では別の derived key になる」ことを確かめる。実ブラウザのテストでも同じ 2 件を使う(13 と 15 を参照)。契約スナップショットへの取り込みは行わない。理由: `sync_pocket_protocol_snapshot.py` の `MIRRORED` に `fixtures/auth-v1` を足すとスナップショット全体を 0.4.0 commit で再生成することになり(Node 22.18 とローカル checkout が必要、他の mirrored file も変わり得る)、この範囲を超える。次回更新時に `MIRRORED` へ追加し、参照へ切り替えることを後続作業とする。

#### 12. Web: 鍵の導出をブラウザで行う

- 導出はブラウザの中で行う。bunri-pocket v0.4.0 の `public/crypto.js` と同じ手順(`TextEncoder` → `importKey("raw", ..., "PBKDF2")` → `deriveBits({name:"PBKDF2", hash:"SHA-256", salt, iterations: 600000}, 256)`)をテンプレートの JS に移植し、`encodeBase64Url` / `decodeBase64Url` も同じ実装にする。salt はブラウザが `crypto.getRandomValues(new Uint8Array(16))` で生成する。
- Bunri サーバーが受け取るのは契約と同じ形の JSON(`kdf` の 4 キーと `derived_key`)だけで、平文のパスワードはブラウザの外へ出ない。サーバーは `parse_reset_request` で形式(正準な base64url、16 byte / 32 byte、KDF の固定値)を検査し、受け取った文字列を転送するのではなく `build_reset_body` で本文を組み立て直してから音源ポケットへ送る。
- `http://127.0.0.1`、`http://localhost`、`http://[::1]` は secure context なので `crypto.subtle` が使える。無い場合(想定外の開き方)は送信せず「この接続では鍵の導出ができません。http://127.0.0.1 で開き直してください。」を表示する。
- 12 コードポイントの検査と 2 回の一致はブラウザが行う。サーバーは平文を受け取らないため長さを検査できない(契約の手順 2 と同じ制約)。
- 選ばなかった案(サーバーで導出): パスワードが TLS の無い loopback HTTP を通り、Bunri サーバーのプロセスにも平文が入る。テストは TestClient だけで済むが、秘密が通る範囲が広がる。ブラウザ導出の欠点である「KDF の実装が JS と Python の 2 つになる」ことは、両方を同じ公開テスト値で固定することで補う。

#### 13. Web: 再設定の入口

- `POST /api/pocket/password-reset?pocket_fingerprint=<fp>`。本文は `application/json` で、契約と同じ `{"kdf": {...}, "derived_key": "..."}`。`pocket_fingerprint` は既存の `sync` / `delete` と同じ query 引数で、ページが掴んでいる接続先と現在の設定が一致するときだけ進む(CLI で別の音源ポケットに繋ぎ直された直後の誤送信を防ぐ)。
- 処理順: Origin と Content-Type の守り(14)→ `pocket_fingerprint` 欠落は 409「Pocket の接続先を確認できないため再設定を中止しました。状態を再読込して確認し直してください。」→ 設定読込(`_pocket_config_or_409` と同じ 409 文言)→ fingerprint 不一致は 409「Pocket の接続先が変更されたため再設定を中止しました。状態を再読込して確認し直してください。」→ 本文の byte 数が 4096 を超えれば 413 → JSON として読めない・形式不正は 400「要求の形式が不正です。画面を再読込してやり直してください。」(値を echo しない)→ `check_password_reset_support(client.capabilities())`(未対応は 409、取得失敗・判定不能は 502。`detail` は `safe_probe_error` の文言)→ `run_reset(client, salt, derived_key)` → 200。
- 200 の応答: `{"state": "<completed|not_applied|unknown|retry_later>", "via": "<response|auth_params>", "messages": [<outcome_messages の各行>]}`。音源ポケットが 4xx で拒否した場合も state で伝え、HTTP 200 で返す(Bunri サーバーは結果を確定して報告しており、画面は state と messages だけで描ける)。4xx の `detail` は「要求を音源ポケットへ送る前に止めた」場合に限る。
- `SyncLock` は取らない(9)。ジョブの記録も作らない(ジョブは同期・削除のためのもので、記録に残す値も無い)。
- 応答には `Cache-Control: no-store` を付ける。

#### 14. Web: 入口の守り

脅威ごとの整理:

| 脅威 | 既存の守り | 不足 | 決めたこと |
|---|---|---|---|
| 悪意あるページからの cross-site の要求(form、fetch、sendBeacon) | 書き込み系で Origin の不一致を 403。cross-origin の POST にはブラウザが必ず `Origin` を付ける(sandbox や data: URL では `null` になり、これも不一致) | Origin 欠落を通している。古いブラウザや拡張で欠落する余地が理屈上残る | 接続・再設定・接続解除の 3 入口では Origin の存在と正確な一致を必須にする。本文のある 2 つの POST(接続・再設定)は `Content-Type` に `application/json`(parameter を除いた media type)を必須にする。form は JSON を送れず、fetch で JSON を送ると CORS の preflight になり CORS 無効のため届かない。cross-origin の DELETE も CORS の preflight になり、CORS が無効なので届かない。届いた場合も Origin の不一致で 403 になる |
| DNS rebinding(攻撃者ドメインを 127.0.0.1 に向ける) | `TrustedHostMiddleware` が Host を 127.0.0.1 / localhost / ::1 / testserver に固定し、他は 400 | なし | 既存のまま。Origin も攻撃者ドメインになるため二重に止まる |
| 同じマシンの別プロセス(curl など) | なし | Origin を付けて送れるため、どの守りも効かない | 守らない。同じ利用者のプロセスは `.pocket/config.json` の token を直接読んで音源ポケットに同じ操作ができるため、新しい露出ではない。Bunri Web が認証を持たない前提のとおり |

- 実装は `src/bunri/web/pocket_settings.py` に置き、守りの関数を 2 つに分ける。
  - `_require_browser_origin(request)`(3 つの入口すべて): Origin が無い、または request URL の origin(scheme / hostname / 既定ポートを補ったポート)と一致しなければ 403「この操作はブラウザの Bunri 画面からだけ実行できます。」。
  - `_require_json(request)`(本文のある 2 つの POST だけ): Content-Type が `application/json` でなければ 415「Content-Type は application/json にしてください。」。
  - `DELETE /api/pocket/connection` は本文が無いので Content-Type の検査は行わない。
- 既存の middleware は変えない(他の入口の挙動を変えないため)。
- Origin 必須の代償: この 3 つの Web API を curl から呼べない。CLI が接続と再設定を提供しており、解除は `.pocket` の削除でもできるので問題ない。ブラウザの same-origin な `fetch` の POST / DELETE は Fetch 標準どおり `Origin` を付ける(Chromium、Firefox、Safari の現行版)。これは実ブラウザのテストで確かめる。
- 選ばなかった案: `Sec-Fetch-Site: same-origin` の要求(対応ブラウザが限られ、Origin の一致で十分)。CSRF token(単一利用者のローカル画面で、Origin で足りる)。既存 middleware を全入口で Origin 必須にする(ジョブ投入などの既存入口の互換性を変える)。

#### 15. Web: 再設定の画面

- 置き場所: ヘッダーの「音源ポケットの設定」(17)から開く設定のダイアログの中のボタン「閲覧パスワードを再設定」で開く。曲一覧の操作列(全曲アップロード / 状態を再読込)には足さない。再設定は曲の操作ではなく音源ポケットの設定だから。既存の `#sw-pocket-banner` の要素と文言はそのまま残す(既存テストが参照している)。
- ダイアログ `<dialog id="sw-reset-dialog">` は削除ダイアログと同じ作り(`showModal`、`cancel` イベントの抑止、送信中はボタンを無効化、`role="alert"` のエラー行)。詳細は後述の「画面の案」。
- 説明の文言: 「音源ポケットの閲覧パスワードを新しく決めます。成功すると、音源ポケットにログインしていたすべての端末がログアウトされます。曲と upload token はそのままです。」
- 入力欄の下の注記: 「12 文字以上。入力した文字をそのまま使います(大文字と小文字、空白、記号を区別します)。パスワードはこのブラウザの中で鍵に変換され、そのままの形では送信されません。」
- 送信前の検査: 「再設定する」を押したとき、12 コードポイント未満なら「閲覧パスワードは 12 文字以上にしてください。」、2 回が一致しなければ「2 回の入力が一致しません。」をエラー行に出して送らない。入力が変わったらエラー行を消す。
- 送信中: ボタンを「再設定中…」にし、入力欄とキャンセルを無効にし、「音源ポケットの応答を待っています。応答が返らない場合は結果を問い合わせるため、2 分ほどかかることがあります。」を表示する。導出(1 秒前後)の間も同じ表示。
- 結果: 入力部を隠し、`messages` を 1 行ずつ `<p>` で表示する。`state` が completed なら緑、not_applied と retry_later なら赤、unknown なら黄の装飾。ボタンは「閉じる」だけ。via が auth_params のときは messages の先頭行がその旨を含むので、画面側で別の表示は足さない。
- 4xx(`detail`)のときは入力部を残し、エラー行に `detail` を出して再試行できるようにする。409 を受けたら(原因を問わず)、エラー行に `detail` を出し、`refreshPocketJob()` と `refreshPocketStatus()` を呼んで画面の接続状態を合わせる。接続が変わっていなければ表示は変わらない。
- 成功後の状態: Bunri の接続設定と曲の状態は変わらないので、状態の再確認はしない。`pocketStatuses` と `pocketConnectionFingerprint` はそのまま。
- `<form>` 要素は使わない(ブラウザの送信動作とパスワード保存の誘いを抑える)。入力欄は `type="password"`、`autocomplete="new-password"`。ブラウザがパスワード保存を勧める可能性は残る(保存先が Bunri の origin になり紛らわしい)ため、README に断る旨を書く。
- テスト用の入口として `window.__bunriWeb.deriveKey(password, saltBase64Url)`(Promise<string>)を既存の `window.__bunriWeb` に加える。公開テスト値との一致を実ブラウザで確かめるために使う。

#### 16. Web: 接続の入口

- `POST /api/pocket/connect`。本文は `application/json` で `{"url": "<文字列>", "token": "<文字列>"}` の 2 キーだけ。4096 byte を上限とする。
- 処理順: 守り(14)→ 本文の形式検査(400「要求の形式が不正です。」)→ `SyncLock(out_dir).acquire()`(取れなければ 409)→ 現在の設定を読む(`read_config`。壊れていても置き換えるので例外は None 扱い)→ `connect_pocket(out_dir, url, token)` → ロック解放 → 200。
- `connect_pocket` は新規モジュール `src/bunri/pocket/connect.py` に置く。中身は CLI の `connect` と同じ順(`validate_base_url` → `validate_token` → `PocketHTTPClient` → `validate_capabilities(client.capabilities())` → `save_config`)。`ConnectError(kind="input" | "pocket" | "save", message)` を投げ、`safe_connect_error` が固定文言を返す。CLI の `connect` は `connect_pocket` へ切り替えない(19。差分を最小にする)。
- 状態コードと文言:
  - `input`(URL・token の形式): 400。`validate_base_url` / `validate_token` の `ValueError` 文言をそのまま使う(入力値を含まない)。
  - `pocket`(通信失敗、HTTP エラー、capabilities の非互換): 502。401 は「upload token が違います。音源ポケットの設定画面で確認してください。」、404 は「接続先に音源ポケットの API が見つかりません。URL を確認してください。」、`validate_capabilities` の `ValueError` はその文言、`OSError` は「音源ポケットに接続できませんでした。URL を確認してください。」、その他は「音源ポケットとの通信に失敗しました(HTTP {status})。」。`str(OSError)` は使わない(host 名を含み得る)。
  - `save`(`save_config` の `OSError`): 500「接続設定を保存できませんでした。」。
- 200 の応答: `{"connection_fingerprint": "<sha256>", "replaced": <bool>, "warnings": [<save_config の警告>]}`。URL と token は含めない。`replaced` は、保存の直前に `read_config` が設定を返したとき(有効な接続設定があったとき)だけ true。設定が無かった場合と、あっても読めない・壊れていた場合は false。
- 既に接続済みのときの扱い: 画面から接続先の変更(つなぎ直し)を許す。CLI の `connect` も黙って上書きしており、token の再発行や URL の変更で必要になる。ただし画面では「現在の接続を置き換えます」と明示し、ジョブ実行中は `SyncLock` により 409 で拒む。接続の解除(設定の削除)は別の入口(20)と確認ダイアログ(21)で行う。
- 成功後: `setPocketConnected(true, body.connection_fingerprint)`、`pocketStatuses = Object.create(null)`、`refreshPocketStatus()` を呼ぶ。`warnings` があれば `#sw-pocket-notice` に表示する。これで帯と曲一覧の操作列が現れ、曲ごとの状態確認が始まる。接続先の変更でも同じ(fingerprint が変わるので古い状態を捨てる)。

#### 17. Web: ヘッダーの表示、「音源ポケットの設定」、接続の画面

ヘッダー:

- 未接続のとき: 「音源ポケットに接続していません」という文と、ボタン「音源ポケットに接続」(`#sw-pocket-connect`)を出す。曲一覧側の Pocket 関連表示は従来どおり隠したまま。
- 接続済みのとき: 既存の帯「Bunri Pocket 連携中」(`#sw-pocket-banner`。要素と文言は変えない)と、その右に下線つきの小さな文字ボタン「音源ポケットの設定」(`#sw-pocket-settings`)を 1 つだけ置く。狭い画面では帯の下に折り返す。
- `/api/pocket/job` の最初の応答が来るまでは、未接続の表示も接続済みの表示も出さない(いまと同じ)。`setPocketConnected` にこの切り替えを足す。
- 曲一覧の操作列(全曲アップロード / 状態を再読込)には何も足さない。

設定のダイアログ:

- 「音源ポケットの設定」を押すと、設定のダイアログ `<dialog id="sw-pocket-settings-dialog">` が開く。見出し「音源ポケットの設定」。3 つのボタンを縦に並べ、それぞれに短い説明を付ける。
  - 「接続先を変更」— 「URL や upload token を入れ直す」
  - 「閲覧パスワードを再設定」— 「忘れたときに、新しいパスワードを決め直す」
  - 「接続を解除」— 「この出力先に保存した接続設定を消す」
  - 下に「閉じる」ボタン。
- 3 つのボタンのどれかを押すと、設定のダイアログを閉じてから、対応するダイアログ(接続 `#sw-connect-dialog`、再設定 `#sw-reset-dialog`、解除の確認 `#sw-disconnect-dialog`)を開く。

接続のダイアログ:

- ダイアログ `<dialog id="sw-connect-dialog">`。入力欄は「音源ポケットの URL」(`type="url"`、`autocomplete="off"`、`spellcheck="false"`、placeholder `https://your-pocket.example`)と「upload token」(`type="password"`、`autocomplete="off"`)。送信前の検査は空欄だけ(「URL と upload token を入力してください。」)。形式はサーバーの `detail` で伝える。token は送信直後に欄を空にする。
- `type="password"` にする理由: 画面共有や覗き見に出さない。ブラウザが保存を勧める可能性はあるが、`<form>` を使わないことで多くの場合は出ない。README に「保存しない」を勧める 1 行を書く。
- 注記は「接続すると、この出力先に設定が保存されます(コマンドの bunri pocket connect と同じ設定です)。」の 1 文だけにする。「接続を解除するには、出力先の .pocket ディレクトリを削除してください。」の注記は置かない(解除は画面からできる)。
- 接続済みのとき(設定のダイアログの「接続先を変更」から開く)の文言: 見出しを「接続先を変更」、説明に「現在の接続を置き換えます。保存済みの upload token は新しいものに置き換わります。曲の状態は接続後に確認し直します。」、ボタンを「置き換える」にする。

#### 18. Web の入口の置き場所

- `src/bunri/web/pocket_settings.py`(新規)に `register_pocket_settings_routes(app: FastAPI, out_dir: Path) -> None` を置き、`create_app` が middleware 登録の後に呼ぶ。`PocketHTTPClient` はモジュール属性として import し、テストが `monkeypatch.setattr(pocket_settings, "PocketHTTPClient", ...)` で差し替える(既存の `app_module.PocketHTTPClient` の差し替えと同じ流儀)。
- 分ける理由: `app.py` は既に 800 行を超え、新しい 3 入口は `JobStore` に依存せず、Origin 必須の守りを局所に閉じ込めたい。
- `app.py` の変更は、`register_pocket_settings_routes` の呼び出し 1 箇所だけ。

#### 19. CLI: `connect` の upload token は端末からの入力を必須にする

- `src/bunri/pocket/cli.py` の `connect` から `--token-stdin` オプション(引数 `token_stdin`)を削除する。
- `connect` は、最初に stdin が TTY かどうかを `_stdin_is_tty()` で調べる。TTY でなければ、stdin を読まず、URL の検査も `PocketHTTPClient` の生成もせずに、「upload token の入力には TTY が必要です。端末から実行するか、Web UI から接続してください。」で終了コード 1。
- TTY のときの動きは今と同じ(`validate_base_url` → `typer.prompt("Pocket upload token", hide_input=True)` → `validate_token` → `PocketHTTPClient` → `validate_capabilities(client.capabilities())` → `save_config` → 表示)。CLI の `connect` は新設の `connect_pocket` へ切り替えない(差分を最小にする)。
- 既存テストの書き換え:
  - `tests/test_cli.py` の `test_pocket_connect_has_no_token_argv_option`: `connect --help` に `--token-stdin` も `--token ` も現れないことを確かめる内容にする(テスト名は内容に合わせて変えてよい)。
  - `tests/test_cli.py` の `test_pocket_connect_reads_token_from_stdin_without_displaying_it`: `_stdin_is_tty` を True に差し替え、プロンプトへの入力として token を与えて接続が成功し、出力に token が現れないことを確かめる内容にする。
  - `tests/test_pocket_security_review_poc.py` の `test_poc_cli_transport_garbage_is_reported_without_traceback` の `connect` の場合: 子プロセスでは TTY を用意できないので、`connect` の場合だけ `CliRunner` で `pocket_cli.app` をプロセス内で起動し(`_stdin_is_tty` を True に差し替え、プロンプトへの入力として token を与える)、同じ偽サーバーに接続させる。確かめる内容は変えない(出力に token が無い、`Traceback` が無い、`error:` がある)。加えて終了コードが 1 で、`result.exception` が `SystemExit` 以外の例外でないことを確かめる。`delete` の場合は変えない。
- 追加するテスト(CLI): stdin が TTY でないとき、`connect` は stdin を読まず、`PocketHTTPClient` を作らず、設定ファイルを作らず、上の文言で終了コード 1。
- `docs/plans/` と `docs/reviews/` にある過去の文書の `--token-stdin` の記述は、当時の記録なので変更しない。

#### 20. Web: 接続の解除の入口

- `src/bunri/pocket/config.py` に `delete_config(out_dir: Path) -> bool` を追加する(`config.py` の既存の関数は変えない)。
  - `.pocket` が無ければ False。`.pocket` が symlink またはディレクトリでなければ `ValueError`(`read_config` と同じ文言)。
  - `config.json` が無ければ(symlink でもなければ)False。`is_real_file_in(path, directory.resolve())` を満たさなければ `ValueError`(`read_config` と同じ文言)。
  - `config.json` だけを `unlink` し、ディレクトリを fsync(失敗は無視)して True を返す。`.pocket` ディレクトリと `sync.lock` は消さない。
- `DELETE /api/pocket/connection?pocket_fingerprint=<fp>` を `src/bunri/web/pocket_settings.py` に置く。本文は無い。
- 守り: Origin の存在と正確な一致を必須にする(接続・再設定の入口と同じ検査。14)。本文が無いので Content-Type の検査は行わない。
- 処理順: 守り → `pocket_fingerprint` 欠落は 409「Pocket の接続先を確認できないため解除を中止しました。状態を再読込して確認し直してください。」→ `SyncLock(out_dir).acquire()`(取れなければ 409「別の Pocket 操作が実行中です。完了後に再実行してください。」)→ 設定読込(読めなければ 409「Pocket の接続設定を確認できません。」、無ければ 409「Pocket の接続設定がありません。」)→ fingerprint 不一致は 409「Pocket の接続先が変更されたため解除を中止しました。状態を再読込して確認し直してください。」→ `delete_config(out_dir)`(`OSError` / `ValueError` は 500「接続設定を削除できませんでした。」)→ ロック解放 → 204(本文なし)。
- 応答に URL と token を含めない。`SyncLock` を取るのは、実行中の同期・削除ジョブの接続先を足元から消さないため。

#### 21. Web: 接続の解除の画面

- 解除の確認ダイアログ `<dialog id="sw-disconnect-dialog">`。見出し「音源ポケットとの接続を解除しますか？」。箇条書き 3 行: 「この出力先に保存した接続先と upload token を消します。」「音源ポケットにある曲と、手元の曲は消えません。」「もう一度使うには、接続し直してください。」。ボタンは「キャンセル」と「解除する」(削除ダイアログの危険操作と同じ見た目)。
- 送信中は「解除中…」にしてボタンを無効にする。失敗(409 / 500 / 403)はエラー行に `detail` を出す。409 を受けたら(原因を問わず)、エラー行に `detail` を出し、`refreshPocketJob()` と `refreshPocketStatus()` を呼んで画面の接続状態を合わせる。500 と 403 はエラー行に `detail` を出すだけ。
- 成功したら: ダイアログを閉じ、`setPocketConnected(false, null)` 相当で未接続の表示に切り替え、`pocketStatuses` を空にし、音源ポケットにしかない曲の一覧(`remoteOnlyTracks`)を空にして描き直す。曲一覧の Pocket 関連の表示は隠れる。

#### 22. コードの置き場所(まとめ)

- `src/bunri/pocket/protocol.py`: `password_reset_supported`。
- `src/bunri/pocket/password.py`(新規): 6〜8 の関数。
- `src/bunri/pocket/connect.py`(新規): `connect_pocket`、`ConnectError`、`safe_connect_error`。
- `src/bunri/pocket/config.py`: `delete_config` の追加だけ。
- `src/bunri/pocket/http.py`: `_request(authenticated=)`、`reset_password`、`auth_params`。
- `src/bunri/pocket/cli.py`: `@app.command("reset-password")` の追加。既存の `_stdin_is_tty`、`_fail` を使う。`connect` からの `--token-stdin` の削除と TTY 検査の追加。
- `src/bunri/web/pocket_settings.py`(新規): 守り(`_require_browser_origin`、`_require_json`)、`POST /api/pocket/connect`、`POST /api/pocket/password-reset`、`DELETE /api/pocket/connection`。
- `src/bunri/web/app.py`: 登録呼び出しの追加のみ。
- `src/bunri/web/templates/index.html.j2`: ヘッダーの要素(`#sw-pocket-connect`、`#sw-pocket-settings`)、4 つのダイアログ(設定 `#sw-pocket-settings-dialog`、接続 `#sw-connect-dialog`、再設定 `#sw-reset-dialog`、解除の確認 `#sw-disconnect-dialog`)、CSS、JS(KDF、ダイアログ制御、`__bunriWeb.deriveKey`)。
- テスト: `tests/test_pocket_password.py`(新規)、`tests/test_pocket_http.py`(追記)、`tests/test_pocket_config.py`(`delete_config` の単体テスト)、`tests/test_cli.py`(追記と、`connect` の既存 2 件の書き換え)、`tests/test_pocket_security_review_poc.py`(`connect` の場合の書き換え)、`tests/test_web_api.py`(追記)、`tests/test_web_page.py`(追記)。偽の音源ポケットは `tests/test_pocket_http.py` の `Handler` / `server` fixture の流儀で、capabilities・`password/reset`・`auth/params` を返す小さなものを `tests/pocket_fake.py`(新規、共有)に置く。

#### 23. README の追記

「Web UI で使う」節の箇条書きの末尾に 2 項目:

- 音源ポケットに未接続のときはヘッダーに「音源ポケットに接続」が出る。音源ポケットの URL と upload token を入力すると、出力先の `.pocket/config.json` に保存される(CLI の `bunri pocket connect` と同じ設定を共有する)。接続済みのときはヘッダーの「音源ポケットの設定」から「接続先を変更」で置き換えられる。同期・削除の実行中は変更できない。接続の解除は「音源ポケットの設定」の「接続を解除」で行える。解除しても音源ポケットにある曲は消えない。ブラウザが token の保存を勧めても保存しないことを勧める(保存先が Bunri の画面になり、音源ポケットのものとして扱えない)。
- 「音源ポケットの設定」の「閲覧パスワードを再設定」で、upload token の権限により音源ポケットの閲覧パスワードを決め直せる(音源ポケット 0.4.0 以降)。パスワードはブラウザの中で鍵に変換され、Bunri のサーバーにも音源ポケットにも平文は送られない。成功すると全端末がログアウトされる。応答が返らない場合は Bunri が音源ポケットに結果を問い合わせて、完了したかどうかを表示する。

「Bunri Pocket へ同期する」節:

- `connect` について、upload token は端末で入力する(パイプなどの標準入力からは渡せない)こと、Web UI からも接続できることを書く。
- 既存の「接続情報を消すには対象の `out/.pocket` を削除してください。」は残し、Web UI からも解除できることを添える。
- コマンド例に `bunri pocket reset-password -o out` を足し、段落を 1 つ足す(要旨):
  - 閲覧パスワードを忘れたとき、`out/.pocket/config.json` の upload token で認証して閲覧パスワードだけを決め直す。音源ポケット 0.4.0 以降が必要で、未対応なら更新を案内して何も送らない。
  - 新しいパスワードは 12 文字(Unicode コードポイント)以上。入力した文字列をそのまま UTF-8 にして導出し、正規化しない。見た目が同じでも入力方法で別の文字列になることがあるため、迷う場合は英数字と記号を勧める。平文のパスワードは送らず、PBKDF2 の derived key だけを送る。
  - 成功すると全端末がログアウトされ、曲と upload token は残る。直前にログインに失敗していた場合は最大 60 秒待つことがある。
  - 既定で最終確認を行う(`--yes` で省略)。パスワードは端末から対話で入力する。
  - 通信失敗や timeout で応答を確認できない場合、Bunri は音源ポケットに結果を問い合わせて「完了した / 反映されていない / 確認できなかった」を表示する。反映されていない・確認できなかった場合は同じパスワードで再実行する。
  - この操作は Pocket mutation lock を取らない。同期や削除の最中でも実行できる。
  - 秘密はメモリから確実には消せないので、信頼できる端末で実行する。

### 画面の案

#### ヘッダー(接続状態)

既存の `<h1>Bunri</h1>` と副題の下、帯の位置。

- 読み込み中(最初の `/api/pocket/job` の応答まで): 何も表示しない(いまと同じ)。
- 未接続:
  ```
  音源ポケットに接続していません   [音源ポケットに接続]
  ```
  曲一覧の「全曲アップロード」「状態を再読込」などは従来どおり隠す。
- 接続済み:
  ```
  (帯)Bunri Pocket 連携中   音源ポケットの設定
  ```
  帯は既存の緑のピル。右の「音源ポケットの設定」は小さな文字ボタン(下線付き)1 つだけ。狭い画面では帯の下に折り返す。

#### 設定のダイアログ(`#sw-pocket-settings-dialog`)

```
音源ポケットの設定

[接続先を変更]
  URL や upload token を入れ直す
[閲覧パスワードを再設定]
  忘れたときに、新しいパスワードを決め直す
[接続を解除]
  この出力先に保存した接続設定を消す

                                              [閉じる]
```

3 つのボタンのどれかを押すと、このダイアログを閉じてから、対応するダイアログを開く。「閉じる」は何もせずに閉じる。

#### 接続ダイアログ(`#sw-connect-dialog`)

未接続のとき:

```
音源ポケットに接続

音源ポケットの URL と upload token を入力してください。
upload token は音源ポケットの設定画面で確認できます。

音源ポケットの URL   [https://your-pocket.example            ]
upload token         [••••••••••••••••••••••••••••••••••••••]

接続すると、この出力先に設定が保存されます
(コマンドの bunri pocket connect と同じ設定です)。

                                   [キャンセル] [接続する]
(エラー行)
```

接続済みのとき(設定のダイアログの「接続先を変更」から開く): 見出しを「接続先を変更」、説明を「現在の接続を置き換えます。保存済みの upload token は新しいものに置き換わります。曲の状態は接続後に確認し直します。」、ボタンを「置き換える」にする。

状態ごとの表示:

- 送信中: ボタン「接続中…」、入力欄とキャンセルを無効化。token 欄は送信直後に空にする。
- 成功: ダイアログを閉じ、帯と操作列が現れ、曲ごとの状態確認が始まる。`warnings` があれば曲一覧の通知欄(`#sw-pocket-notice`)に「警告: .pocket directory の権限を保証できないため…」をそのまま表示する。
- 空欄: 「URL と upload token を入力してください。」(送らない)
- 400: 「Pocket URL には https と host が必要です」「upload token の形式が不正です」など、サーバーの `detail` をそのまま表示。入力部は残す(token 欄は空になっているので入れ直す)。
- 502: 「音源ポケットに接続できませんでした。URL を確認してください。」「upload token が違います。音源ポケットの設定画面で確認してください。」など。
- 409: 「別の Pocket 操作が実行中です。完了後に再実行してください。」
- 403: 「この操作はブラウザの Bunri 画面からだけ実行できます。」(通常は見えない)

#### 再設定ダイアログ(`#sw-reset-dialog`)

入力:

```
閲覧パスワードを再設定

音源ポケットの閲覧パスワードを新しく決めます。
成功すると、音源ポケットにログインしていたすべての端末がログアウトされます。
曲と upload token はそのままです。

新しい閲覧パスワード          [••••••••••••••••]
新しい閲覧パスワード(確認)   [••••••••••••••••]

12 文字以上。入力した文字をそのまま使います(大文字と小文字、空白、記号を区別します)。
パスワードはこのブラウザの中で鍵に変換され、そのままの形では送信されません。

                                   [キャンセル] [再設定する]
(エラー行)
```

送信前のエラー(送らない): 「閲覧パスワードは 12 文字以上にしてください。」「2 回の入力が一致しません。」「この接続では鍵の導出ができません。http://127.0.0.1 で開き直してください。」

送信中: ボタン「再設定中…」、入力欄とキャンセルを無効化、入力欄は導出後すぐ空にする。注記「音源ポケットの応答を待っています。応答が返らない場合は結果を問い合わせるため、2 分ほどかかることがあります。」

結果(入力部を隠し、`messages` を 1 行ずつ表示。ボタンは「閉じる」):

- completed / response(緑):
  ```
  閲覧パスワードを決め直しました。
  音源ポケットにログインしていたすべての端末はログアウトされました。新しいパスワードで入り直してください。
  直前にログインに失敗していた場合は、待ち時間(最大 60 秒)が明けるまでログインできないことがあります。
  曲と upload token、Bunri の接続設定はそのままです。
  ```
- completed / auth_params(緑): 先頭に「音源ポケットからの応答を確認できなかったため、結果を問い合わせました。再設定は完了していました。」、続けて上の 2〜4 行目。
- not_applied / response(赤): 例「upload token が無効です。音源ポケットで token を再発行した場合は接続し直してください。」「閲覧パスワードは変わっていません。」
- not_applied / auth_params(赤): 「音源ポケットからの応答を確認できなかったため、結果を問い合わせました。再設定は反映されていません。閲覧パスワードは以前のままです。」「同じ操作をもう一度実行してください。再実行では今回と同じパスワードを使ってください(遅れて届いた前回の要求が後から反映されても結果が変わらないようにするため)。」
- unknown(黄): 「音源ポケットからの応答を確認できなかったため、結果を問い合わせましたが、確認できませんでした。」「再設定が完了したかどうかは分かりません。新しいパスワードでログインできれば完了しています。古いパスワードで入れる場合は完了していません。同じ操作をもう一度実行しても安全です。再実行では今回と同じパスワードを使ってください。」
- retry_later(赤): 「音源ポケットの認証情報または保存領域を利用できませんでした。時間をおいて同じ操作を再実行してください。」

送る前に止まった場合(入力部を残し、エラー行に表示): 409「この音源ポケットは閲覧パスワードの再設定に未対応です。音源ポケットを 0.4.0 以降へ更新してください。閲覧パスワードは変わっていません。」、409「Pocket の接続先が変更されたため再設定を中止しました。状態を再読込して確認し直してください。」、502「音源ポケットに接続できませんでした。閲覧パスワードは変わっていません。」など。409 のときは原因を問わず、エラー行の表示に加えて接続状態を再読込する。

#### 解除の確認ダイアログ(`#sw-disconnect-dialog`)

```
音源ポケットとの接続を解除しますか？

・この出力先に保存した接続先と upload token を消します。
・音源ポケットにある曲と、手元の曲は消えません。
・もう一度使うには、接続し直してください。

                                   [キャンセル] [解除する]
(エラー行)
```

- 「解除する」は削除ダイアログの危険操作と同じ見た目。
- 送信中: ボタン「解除中…」、ボタンを無効化。
- 成功: ダイアログを閉じ、ヘッダーが未接続の表示になり、曲一覧の Pocket 関連の表示が隠れる。
- 失敗(エラー行に `detail` を表示): 409「別の Pocket 操作が実行中です。完了後に再実行してください。」、409「Pocket の接続先が変更されたため解除を中止しました。状態を再読込して確認し直してください。」、409「Pocket の接続設定がありません。」、500「接続設定を削除できませんでした。」、403「この操作はブラウザの Bunri 画面からだけ実行できます。」(通常は見えない)など。409 のときは原因を問わず、エラー行の表示に加えて接続状態を再読込する。
- 「キャンセル」: 何も変えずに閉じる。

### 契約・現行コードとの関係(補足)

- `GET /api/v1/auth/params` の応答形は、契約文書(bunri-pocket v0.4.0 の `docs/protocol-v1.md`「認証」節と「API」節)に明記されておらず、音源ポケットの Worker のテストで確かめられている形(7 の JSON の例)に頼っている。本計画は形が違う応答を「確認できなかった」に落とすが、音源ポケット側で契約文書に応答形を追記してもらうのが望ましい。
- `PocketHTTPClient._request` は常に `Authorization: Bearer` を付ける。`auth/params` は認証なしの経路なので、`authenticated` 引数を加えて付けないようにする(認証なしの経路に秘密を送らない)。
- 契約のクライアント手順 7 は「404 は音源ポケットが 0.3.x 以前」とするが、Bunri は手順 1 で `features.password_reset` を先に見るため、POST で 404 が返るのは「capabilities は対応と言ったのに route が無い」場合になる。文言は URL や経路の問題も含めた。
- 契約の検査順では 503 AUTH_UNAVAILABLE が 401 より前に評価される。503 を受けても token が有効とは限らないため、503 の文言で token に言及しない。
- 契約の手順 4 は `name` / `iterations` / `dk_len` を `auth/params` と突き合わせてよいとするが、本計画では固定値をそのまま送る。不一致は 422 UNSUPPORTED_KDF で「Bunri を更新」と案内できるため、事前照合の価値が無い。
- `PocketHTTPError.user_message()` の 422 文言「文書または media の内容不一致」は UNSUPPORTED_KDF を誤って説明する。再設定の案内は status と code の組で決める。
- 既存 `delete_track` は timeout に定数を直接使い `metadata_timeout` を無視している。新規メソッドでは属性を使う。既存の修正はしない。
- 既存 CLI `connect` は `OSError` を `str(exc)` で表示しており、`URLError` の文言に host 名が入る可能性がある。Web の接続では固定文言にした。CLI の `connect` が `OSError` を `str(exc)` で表示する点は、この計画では変えない。
- 既存の同一オリジン強制は Origin 欠落を通す。接続・再設定・接続解除の 3 入口だけ Origin を必須にするため、同じアプリの中で守りの強さが入口ごとに異なる。意図的であり、計画と `pocket_settings.py` のコメントに理由を書く。
- `TrustedHostMiddleware` は `::1` も許すが、`http://[::1]:8330/` で開いた場合も secure context なので WebCrypto は使える。
- 既存テスト `test_pocket_status_is_hidden_when_not_connected` は HTML に「Bunri Pocket 連携中」が含まれることを見ている。帯の要素と文言は変えず、隣に要素を足す。
- 既存テスト `test_pocket_sync_template_binds_requests_and_refreshes_changed_connection` は `postPocket` と `refreshPocketJob` の正規表現を見ている。この 2 関数は変えない。
- `password_reset_supported` は `src/bunri/pocket/protocol.py` の `pan_split_supported` に合わせて `api.major == 1` も条件にする。契約は `features` のキーだけを定めているが、`api.major` が 1 でない capabilities は接続判定でも拒否されるため、条件を足しても対応状況の判定を変えない。
- 契約の「derived key の buffer は MAC の計算後にゼロで上書きします」は Worker 側の約束であり、Python クライアントとブラウザの JS では同等の保証ができない。契約の手順 8「できる限り破棄」の範囲で、参照の早期解放と入力欄のクリアにとどめる。

### 代替案を選ばなかった理由

- 標準入力からパスワードを受け取る入口(`--password-stdin`)を設ける: 秘密の入口が増える。Web 画面が非対話の入口の代わりになり、復旧はまれな操作なので、端末の非表示入力だけにする。
- `connect --token-stdin` を残す: token は標準入力から渡せるのにパスワードは渡せない、という非対称が残る。Web からの接続が非対話の入口の代わりになるので、`connect` も端末入力だけにそろえる。
- CLI の `connect` を `connect_pocket` に切り替える: 重複は減るが、既存の表示と失敗時の動きに差分が広がる。オプションの削除と端末入力の検査だけにとどめる。
- 接続の解除を画面に置かず、`.pocket` を手で消す案内だけにする: 画面だけで接続の管理が完結しない。解除の入口と確認ダイアログを置く。
- 接続の解除で `SyncLock` を取らない: 実行中の同期・削除ジョブの接続先を足元から消してしまう。
- ヘッダーに「接続先を変更」「閲覧パスワードを再設定」「接続を解除」を並べる: まれにしか使わない操作でヘッダーが込み合う。「音源ポケットの設定」1 つにまとめ、ダイアログの中で選ぶ。
- 確認をパスワード入力の後に置く: 入力後に取り消すと秘密をメモリに置いた上で捨てることになる。
- `validate_capabilities` を再設定でも通す: upload 契約の変化で復旧操作が止まる。
- 再設定で `SyncLock` を取る: 守る対象が無く、長い同期中に復旧できなくなる。
- Web 接続で `SyncLock` を取らない: 実行中ジョブの接続先を足元から変えてしまう。
- 成功(204)の後も `auth/params` で照合する: 契約が 204 を成功と定めており冗長。
- 503 でも照合する: 保存系障害時は GET も失敗しやすく、待ちが増えるだけ。契約どおり再実行を案内する。
- `auth/params` に Bearer を付けて送る: 認証なしの経路に秘密を送る理由が無い。
- 鍵の導出を Bunri サーバー(Python)で行う: 平文が loopback HTTP とサーバープロセスを通る(12)。
- 再設定の前に別の GET 入口で対応状況を調べる: 入口が増える。導出のコスト(1 秒前後)で未対応を知らせる遅れは許容する。
- 再設定の結果を 4xx の HTTP ステータスで区別して返す: 画面は state と messages だけで描けるため、ステータスの表を増やさない。
- Origin 必須を既存 middleware に広げる: 既存入口の互換性を変える。新しい 3 入口だけに閉じる。
- 再設定・接続のボタンを曲一覧の操作列に置く: 曲の操作と音源ポケットの設定が混ざる。
- 公開テスト値をスナップショットへ取り込む: 再生成の範囲がこの作業を超える(11)。
- `bytearray` によるゼロ消去: 複数の不変コピーが残り、保証にならない。

## タスク分解

| # | タスク | 依存 |
|---|---|---|
| 1 | `protocol.py` に `password_reset_supported` を追加し、単体テスト | なし |
| 2 | `password.py` を新設。定数、`PasswordResetError`、`validate_new_password`、`generate_salt`、`derive_key`、`b64url`、`decode_b64url`、`build_reset_body`、`parse_reset_request`、`check_password_reset_support`、`safe_probe_error`、`outcome_messages`。公開テスト値を含む単体テスト | 1 |
| 3 | `http.py` に `_request(authenticated=)`、`reset_password`、`auth_params` を追加。偽サーバーで要求形式・204 判定・エラー envelope・redirect 拒否・timeout・Bearer の有無を検証 | なし |
| 4 | `password.py` に `confirm_reset`、`run_reset`、`reset_with_password` を実装。偽クライアントで分類と照合(一致 / 不一致 / 読めない / 回数 / 待ち)を検証 | 2, 3 |
| 5 | `cli.py` に `reset-password` を追加し、CLI 経由のテスト(TTY 検査、未対応・判定不能、確認、対話入力、文言、照合の表示、終了コード、秘密の非表示、ロック中でも実行可) | 4 |
| 6 | `cli.py` の `connect` から `--token-stdin` を削除し、TTY 検査を追加。`tests/test_cli.py` の 2 件と `tests/test_pocket_security_review_poc.py` の `connect` の場合を書き換え、非 TTY のテストを追加 | なし |
| 7 | `connect.py` を新設し、`web/pocket_settings.py` に守り(`_require_browser_origin`、`_require_json`)と `POST /api/pocket/connect` を実装。`app.py` から登録。Web API テスト(Origin / Content-Type / 形式 / ロック / 成功 / 置き換え / 失敗の文言 / 秘密の非表示) | 3 |
| 8 | `config.py` に `delete_config` を追加し、単体テスト。`web/pocket_settings.py` に `DELETE /api/pocket/connection` を実装。Web API テスト(守り / fingerprint / 設定無し / ロック / 成功 / 秘密の非表示) | 7 |
| 9 | テンプレートに未接続表示・「音源ポケットの設定」ボタンと設定のダイアログ・接続ダイアログ・解除の確認ダイアログ・JS を追加。実ブラウザのテスト(偽の音源ポケットへ接続して設定が保存され、token が HTML に出ない。設定のダイアログから各ダイアログが開く。解除で未接続の表示に戻り設定が消える) | 7, 8 |
| 10 | `web/pocket_settings.py` に `POST /api/pocket/password-reset` を実装。Web API テスト(守り / fingerprint / 形式 / 未対応 / 各 state / 照合 / 秘密の非表示 / ロック中でも実行可) | 4, 7 |
| 11 | テンプレートに再設定ダイアログ・KDF(WebCrypto)・`__bunriWeb.deriveKey` を追加。実ブラウザのテスト(公開テスト値との一致、送信前検査、偽の音源ポケットへの要求内容、結果表示) | 9, 10 |
| 12 | README「Web UI で使う」「Bunri Pocket へ同期する」節へ追記 | 5, 6, 9, 11 |
| 13 | `uv run pytest -q -n auto` を全件通し、作業ブランチへ commit(push はしない) | 1〜12 |
| 14 | レビューと共有内容の検査を済ませた `docs/plans/2026-10-12-pocket-reset-password/plan.md` と `request.md` を、実装の commit とは別の commit で同じブランチへ commit | 13 |

commit を分けられる単位: 1〜4(共通処理)、5(CLI の再設定)、6(CLI の `connect` の端末入力必須化)、7〜9(Web の接続・解除・設定のダイアログ)、10〜11(Web の再設定)、12(文書)、14(計画書)。

## 完了条件・受け入れ基準

経路の凡例: 単体 = 関数を直接呼ぶ / 偽サーバー = loopback の `http.server` に本物の `PocketHTTPClient` で接続 / CLI = `CliRunner` で `pocket_app` を起動し `PocketHTTPClient` を偽クライアントに差し替える / Web API = `TestClient` で `create_app` を起動し `pocket_settings.PocketHTTPClient` を差し替える(必要なら `Origin: http://testserver` を付ける)/ 実ブラウザ = Playwright + Chromium で uvicorn と偽の音源ポケットを起動 / レビュー = 差分の目視。

公開テスト値は、bunri-pocket v0.4.0 の `fixtures/auth-v1/vectors.json` の 2 件を指す。

導出と本文:

- [ ] 公開テスト値の 2 件目(password `bunri-pocket-reset-passphrase`、salt `EBESExQVFhcYGRobHB0eHw`)から derived key `ZDdrEqwT6KETy2izxW2uK25FVChkKDdx38yVDdttiOI` が得られる。(単体)
- [ ] 公開テスト値の 1 件目(password `bunri-pocket-test-passphrase`、salt `AAECAwQFBgcICQoLDA0ODw`)から `amWJtD1HdCn6v6NI81RE6WC_ysbPDCbdF5mMBCbC-Ts` が得られ、2 件目と異なる。(単体)
- [ ] `build_reset_body` の出力は top-level が `kdf` と `derived_key` の 2 キー、`kdf` が `name` / `iterations` / `salt` / `dk_len` の 4 キーだけで、値が `PBKDF2-HMAC-SHA256` / 600000 / 32 と padding なし base64url。2 件目の salt と derived key を渡すと、bunri-pocket v0.4.0 の `docs/protocol-v1.md`「認証」節の要求本文の例と同じ値になる。ASCII のみで 4096 byte 以下。(単体)
- [ ] `generate_salt` は 16 byte を返し、連続 2 回の値が異なる。(単体)
- [ ] `b64url` の出力に `=`、`+`、`/` が含まれない。`decode_b64url` は非正準(末尾の余り bit が 0 でない)、padding 付き、長さ違い、`+` / `/` を含む入力を `invalid_request` で拒否し、正準な入力は decode する。(単体)
- [ ] `parse_reset_request` は契約の例を受理し、キーの過不足、型違い、固定値の不一致、salt 15 byte、derived_key 31 byte を `invalid_request` で拒否する。メッセージに入力値が含まれない。(単体)

パスワードの検査:

- [ ] 11 コードポイントは拒否、12 は受理。非 BMP 文字 12 個は受理。(単体)
- [ ] NFC と NFD の同じ見た目の文字列は別の UTF-8 bytes になり derived key も異なる。(単体)
- [ ] 孤立 surrogate を含む入力は固定文言の `PasswordResetError(kind="not_utf8")` になり、メッセージにその文字が含まれない。(単体)
- [ ] 前後の空白はそのまま UTF-8 化される。(単体)

capabilities の判定:

- [ ] `api.major == 1` かつ `features.password_reset is True` のときだけ対応。`false`、`1`、`"true"`、キー無し、`features` 無し、`api` 無しは未対応。(単体)
- [ ] capabilities が object でない(配列、文字列)ときは `undetermined` で、未対応の文言を出さない。(単体、CLI、Web API)

HTTP:

- [ ] `reset_password` は `POST /api/v1/password/reset` に `Authorization: Bearer <token>`、`User-Agent: bunri/<version>`、`Content-Type: application/json`、`Content-Length` = 本文長を送り、`Origin` と `Cookie` を送らない。本文は渡した bytes と一致する。204 で `None` を返す。(偽サーバー)
- [ ] 200 空本文、200 本文あり、201 は `PocketHTTPError(code="UNEXPECTED_RESPONSE")`。(偽サーバー)
- [ ] `reset_password` に対して、上限を超える本文を持つ 200 の応答を返すと `PocketHTTPError(status=413, code="RESPONSE_TOO_LARGE")` になる。(偽サーバー。`monkeypatch` で `bunri.pocket.http.JSON_LIMIT_BYTES` を小さな値に差し替え、それを超える本文を返す。`reset_password` は呼び出しのたびにこの定数を `limit` として渡す)
- [ ] 401 / 403 ORIGIN_MISMATCH / 404 / 422 UNSUPPORTED_KDF / 503 AUTH_UNAVAILABLE / 429 Retry-After の envelope が status、code、retry_after に写る。(偽サーバー)
- [ ] 302 は追従せずエラーになる。(偽サーバー)
- [ ] 応答の timeout は `OSError` になり `PocketHTTPError` にならない。(偽サーバー、`metadata_timeout` を短縮)
- [ ] 4097 byte 以上の本文は送信前に 413 で拒否される。(単体)
- [ ] `auth_params` は `GET /api/v1/auth/params` に `User-Agent` を付け、`Authorization` を付けない。200 の JSON を返し、405 / 503 は `PocketHTTPError`、JSON でない本文は `PocketHTTPError(code="INVALID_DOCUMENT")`。(偽サーバー)
- [ ] 既存の `capabilities` などは引き続き `Authorization` を付ける。(偽サーバー、既存テストが通る)

結果の分類と照合:

- [ ] 204 → `ResetOutcome(state="completed", via="response")`。音源ポケットが返した 4xx(code が `UNEXPECTED_RESPONSE` でも `RESPONSE_TOO_LARGE` でもない `PocketHTTPError`。401 / 403 / 404 / 422 / 429 で確かめる)→ `not_applied` / `response`。503 → `retry_later`。(単体、偽クライアント)
- [ ] `OSError`、502、`UNEXPECTED_RESPONSE`、`PocketHTTPError(413, code="RESPONSE_TOO_LARGE")` → `confirm_reset` が呼ばれ、結果の state と `via="auth_params"` になる。(単体、偽クライアント)
- [ ] 音源ポケットが返した 413(code が `PAYLOAD_TOO_LARGE`)は `not_applied` / `response` で、`confirm_reset` は呼ばれない。(単体、偽クライアント)
- [ ] `confirm_reset`: 1 回目で salt 一致 → `completed`、`sleep` は呼ばれない。(単体)
- [ ] 1 回目・2 回目が不一致、3 回目が一致 → `completed`、`sleep` が 2 回。(単体)
- [ ] 3 回とも不一致 → `not_applied`、`auth_params` が 3 回呼ばれ、`sleep` が 2 回。(単体)
- [ ] 3 回とも `OSError` → `unknown`。不一致・不一致・`OSError` → `unknown`。`OSError`・`OSError`・不一致 → `not_applied`。(単体)
- [ ] 応答が object でない、`kdf` が無い、salt が非正準、503 → その回は「読めない」扱い。(単体)
- [ ] 照合は bytes で比較する(同じ salt の正準表現が一致する)。(単体)
- [ ] `outcome_messages` の各 state・via の組が本計画の文言と一致し、どの組にも「変わっていません」と「決め直しました」が同時に現れない。(単体)

CLI(`reset-password`):

- [ ] `reset-password --help` に `--yes`、`--output` があり、`--password` と `--password-stdin` が無い。(CLI)
- [ ] 接続設定が無いと「Pocket の接続設定がありません。」で、`PocketHTTPClient` を作らず終了コード 1。読めない・壊れていると「Pocket の接続設定を確認できません。」で、`PocketHTTPClient` を作らず終了コード 1。(CLI)
- [ ] stdin が TTY でないと(`--yes` の有無に関わらず)stdin を読まず HTTP を始めずに、「閲覧パスワードの入力には TTY が必要です。端末から実行するか、Web UI から再設定してください。」で終了コード 1。(CLI、`_stdin_is_tty` を monkeypatch)
- [ ] capabilities に `password_reset: true` が無いと 0.4.0 への更新を案内し、`reset_password` を呼ばず終了コード 1。(CLI)
- [ ] capabilities 取得で 401 / `OSError` になると「閲覧パスワードは変わっていません。」を含む案内で終了コード 1。(CLI)
- [ ] 確認で `n` を答えると「取り消しました」で終了コード 0、パスワードの入力を求めない。(CLI、TTY を monkeypatch)
- [ ] 対話入力で 2 回一致すると、`reset_password` に渡った本文の salt が 16 byte で derived key が同じ salt からの導出結果と一致し、成功文言(全端末ログアウト、新しいパスワードで入り直す、最大 60 秒)が表示され、終了コード 0。(CLI)
- [ ] 対話入力の不一致、11 コードポイントはそれぞれ固定文言で終了コード 1、`reset_password` は呼ばれない。(CLI)
- [ ] `reset_password` が 401 / 403 ORIGIN_MISMATCH / 404 / 422 UNSUPPORTED_KDF / 400 / 429 を投げると表の文言と「閲覧パスワードは変わっていません。」が表示され終了コード 1、`auth_params` は呼ばれない。(CLI、parametrize)
- [ ] 503 では再実行の案内が表示され、「変わっていません」とも「決め直しました」とも表示されず、`auth_params` は呼ばれない。(CLI)
- [ ] `OSError` / 502 の後に `auth_params` の salt が一致すると「問い合わせました。再設定は完了していました。」と成功文言で終了コード 0。`PocketHTTPError(413, code="RESPONSE_TOO_LARGE")` の場合も同じ表示になる。(CLI)
- [ ] 不一致が続くと「反映されていません」「同じパスワードを使ってください」で終了コード 1。(CLI、`CONFIRM_INTERVAL_SECONDS` を 0)
- [ ] 照合の GET も失敗すると「確認できませんでした」「再実行しても安全」で終了コード 1。(CLI)
- [ ] 上のすべての CLI テストで、出力にパスワード、UTF-8 化したパスワードの base64、derived key、upload token が含まれない。(CLI、共通の assert)
- [ ] 確認の表示に接続先 URL と「すべての端末がログアウトされる」旨が含まれる。(CLI)
- [ ] `SyncLock` を別途取得した状態でも再設定が成功する。(CLI)

CLI(`connect`):

- [ ] `connect --help` に `--token-stdin` も `--token ` も現れない。(CLI、`tests/test_cli.py` の `test_pocket_connect_has_no_token_argv_option` を書き換える。テスト名は内容に合わせて変えてよい)
- [ ] `_stdin_is_tty` を True に差し替え、プロンプトへの入力として token を与えると接続が成功し、出力に token が現れない。(CLI、`tests/test_cli.py` の `test_pocket_connect_reads_token_from_stdin_without_displaying_it` を書き換える)
- [ ] stdin が TTY でないとき、`connect` は stdin を読まず、`PocketHTTPClient` を作らず、設定ファイルを作らず、「upload token の入力には TTY が必要です。端末から実行するか、Web UI から接続してください。」で終了コード 1。(CLI、`_stdin_is_tty` を monkeypatch)
- [ ] `tests/test_pocket_security_review_poc.py` の `test_poc_cli_transport_garbage_is_reported_without_traceback` の `connect` の場合は、`CliRunner` で `pocket_cli.app` をプロセス内で起動し(`_stdin_is_tty` を True に差し替え、プロンプトへの入力として token を与える)、同じ偽サーバーに接続させる。出力に token が無く、`Traceback` が無く、`error:` があり、終了コードが 1 で、`result.exception` が `SystemExit` 以外の例外でない。`delete` の場合は変えない。(CLI + 偽サーバー)

設定の削除(`delete_config`):

- [ ] 設定が無ければ False で何も起きない。(単体、`tests/test_pocket_config.py`)
- [ ] 設定があれば True を返し、`config.json` が消え、`.pocket` ディレクトリと `sync.lock` は残り、`read_config` が None を返す。(単体、`tests/test_pocket_config.py`)
- [ ] `.pocket` が symlink のとき、`config.json` が symlink のときは `ValueError` で、リンク先は消えない。(単体、`tests/test_pocket_config.py`)

Web API: 守り(接続・再設定・接続解除の 3 入口で共通、parametrize):

- [ ] `Origin` が無い要求(2 つの POST と `DELETE /api/pocket/connection`)は 403 で、`PocketHTTPClient` が作られず設定ファイルも変わらない。(Web API)
- [ ] `Origin` が `http://evil.example.com`、`http://testserver:9999`、`http://testserver:not-a-port`、`null` のいずれでも 403 で、設定ファイルは変わらない(3 入口)。(Web API)
- [ ] `Origin: http://testserver` かつ `Content-Type: text/plain` または multipart は 415(本文のある 2 つの POST)。(Web API)
- [ ] `Origin: http://testserver` かつ `Content-Type: application/json` は守りを通過する。`application/json; charset=utf-8` も通る(本文のある 2 つの POST)。`DELETE /api/pocket/connection` は Content-Type が無くても守りを通過する。(Web API)
- [ ] 本文が 4097 byte 以上なら 413(本文のある 2 つの POST)。(Web API)
- [ ] 既存の `/api/jobs` などの入口は Origin 欠落でも従来どおり通る(既存テストが通る)。(Web API)

Web API: 接続:

- [ ] 正しい URL と token で 200。応答は `connection_fingerprint`(URL の SHA-256)、`replaced: false`、`warnings` のみで、URL と token を含まない。`.pocket/config.json` が `read_config` で読め、権限が 0o700 / 0o600。(Web API)
- [ ] 接続済みで別 URL を送ると 200、`replaced: true`、設定が置き換わる。(Web API)
- [ ] 壊れた `config.json`(JSON として読めない内容)がある状態で接続すると 200、`replaced: false` で、設定が正しい内容に置き換わる。(Web API)
- [ ] 本文が `url` / `token` 以外のキーを含む、型が違う、JSON でない → 400 で設定を作らない。(Web API)
- [ ] URL が `ftp://`、userinfo 付き、`http://example.com`(非 loopback の http) → 400 で `validate_base_url` の文言。token が短い・不正 → 400 で `validate_token` の文言。`PocketHTTPClient` は作られない。(Web API)
- [ ] capabilities が 401 → 502「upload token が違います」。`OSError` → 502「接続できませんでした」。`validate_capabilities` 不合格 → 502 でその文言。設定は作られない。(Web API)
- [ ] `SyncLock` を別途取得した状態では 409 で設定は変わらない。(Web API)
- [ ] 本物の偽サーバー(`http://127.0.0.1:<port>`)を URL に指定すると接続できる(loopback の http を受理)。(Web API + 偽サーバー)
- [ ] 失敗を含むすべての応答本文に token と URL が含まれない。(Web API、共通の assert)

Web API: 接続の解除:

- [ ] `pocket_fingerprint` 無し → 409 で、`detail` が「Pocket の接続先を確認できないため解除を中止しました。状態を再読込して確認し直してください。」。設定無し → 409。fingerprint 不一致 → 409 で設定は残る。(Web API)
- [ ] `SyncLock` を別途取得した状態では 409 で設定は残る。(Web API)
- [ ] 正しい fingerprint と Origin で 204・本文なし。`.pocket/config.json` が消え、`GET /api/pocket/job` の `connected` が false になる。応答に token と URL が含まれない。(Web API)

Web API: 再設定:

- [ ] `pocket_fingerprint` 無し → 409 で、`detail` が「Pocket の接続先を確認できないため再設定を中止しました。状態を再読込して確認し直してください。」。設定無し → 409。fingerprint 不一致 → 409「接続先が変更されたため再設定を中止」。いずれも `PocketHTTPClient` は作られない。(Web API)
- [ ] 本文が契約の例の形で、capabilities が対応 → `reset_password` に渡った本文が `build_reset_body(salt, derived_key)` と byte 単位で一致する(転送ではなく組み立て直し)。応答 200、`state: "completed"`、`via: "response"`、`messages` に成功文言。(Web API)
- [ ] 本文のキー過不足、salt 15 byte、非正準 base64url、`iterations: 1000` → 400 で `reset_password` は呼ばれない。`detail` に入力値が含まれない。(Web API)
- [ ] capabilities 未対応 → 409 で 0.4.0 への更新を案内。取得失敗(`OSError`)→ 502。(Web API)
- [ ] `reset_password` が 401 / 404 / 422 UNSUPPORTED_KDF → 200、`state: "not_applied"`、`via: "response"`、表の文言と「変わっていません」。(Web API)
- [ ] 503 → 200、`state: "retry_later"`。(Web API)
- [ ] `OSError` の後に `auth_params` の salt が一致 → `state: "completed"`、`via: "auth_params"`。不一致が続く → `not_applied` / `auth_params`。GET も失敗 → `unknown`。(Web API、`CONFIRM_INTERVAL_SECONDS` を 0)
- [ ] `SyncLock` を別途取得した状態でも 200 で再設定が進む。(Web API)
- [ ] 応答に `Cache-Control: no-store` がある。(Web API)
- [ ] すべての応答本文に derived key、salt、token、URL が含まれない。ジョブの記録(`/api/jobs`)が増えない。(Web API)

実ブラウザ:

- [ ] `window.__bunriWeb.deriveKey("bunri-pocket-reset-passphrase", "EBESExQVFhcYGRobHB0eHw")` が `ZDdrEqwT6KETy2izxW2uK25FVChkKDdx38yVDdttiOI` を返し、1 件目の組も `amWJtD1HdCn6v6NI81RE6WC_ysbPDCbdF5mMBCbC-Ts` を返す。(実ブラウザ)
- [ ] 未接続で開くとヘッダーに「音源ポケットに接続」が見え、「音源ポケットの設定」は見えず、Pocket の操作列は隠れている。ダイアログで偽の音源ポケットの URL と token を入れて接続すると、`.pocket/config.json` が作られ、帯「Bunri Pocket 連携中」と「音源ポケットの設定」が現れ、`page.content()` に token が含まれない。(実ブラウザ。Origin を要求するサーバーに対して成功することが、ブラウザが same-origin POST に Origin を付ける確認を兼ねる)
- [ ] 接続済みで「音源ポケットの設定」を押すと設定のダイアログが開き、「接続先を変更」「閲覧パスワードを再設定」「接続を解除」のそれぞれで、設定のダイアログが閉じて対応するダイアログ(`#sw-connect-dialog`、`#sw-reset-dialog`、`#sw-disconnect-dialog`)が開く。「接続先を変更」から開いた接続ダイアログは見出しが「接続先を変更」になる。「閉じる」では何も変わらない。(実ブラウザ)
- [ ] 再設定ダイアログ(「音源ポケットの設定」→「閲覧パスワードを再設定」で開く)で 11 文字を入れると「12 文字以上」のエラーが出て偽の音源ポケットに要求が届かない。2 回が不一致でも同様。(実ブラウザ)
- [ ] 12 文字以上を 2 回一致させて送ると、偽の音源ポケットが `POST /api/v1/password/reset` を 1 回受け、`Authorization: Bearer <保存した token>`、`Content-Type: application/json` があり、`Cookie` が無い。本文の `kdf.salt` は 16 byte で、`derived_key` は Python の `derive_key(password, salt)` と一致する。ダイアログに「閲覧パスワードを決め直しました」が出る。送信後、入力欄の値は空。(実ブラウザ)
- [ ] 偽の音源ポケットが再設定に 401 を返すと、ダイアログに「upload token が無効です」と「変わっていません」が出る。(実ブラウザ)
- [ ] 接続済みで開き、「音源ポケットの設定」→「接続を解除」→「解除する」で、ヘッダーが未接続の表示になり、曲一覧の Pocket の操作列が隠れ、`.pocket/config.json` が消える。「キャンセル」では何も変わらない。(実ブラウザ)
- [ ] 接続済みで開いた後、テスト側で `.pocket/config.json` を消してから「音源ポケットの設定」→「接続を解除」→「解除する」を押すと、エラー行に 409 の `detail`(「Pocket の接続設定がありません。」)が出て、ダイアログを閉じるとヘッダーが未接続の表示になっている(409 で接続状態を再読込することの確認)。(実ブラウザ)
- [ ] 既存の `test_pocket_sync_template_binds_requests_and_refreshes_changed_connection` と Pocket 関連の既存テストが通る。(実ブラウザ / Web API)

全体:

- [ ] `uv run pytest -q -n auto` が全件通る。(CI と同じコマンド)
- [ ] `service.py`、`sync.py`、契約スナップショット、既存の middleware に差分が無い。`config.py` の差分は `delete_config` の追加だけ。`app.py` の差分は登録呼び出しだけ。`cli.py` の `connect` の差分は `--token-stdin` の削除と TTY 検査の追加だけ。`docs/plans/` と `docs/reviews/` の過去の文書に差分が無い。(レビュー)
- [ ] テンプレートが外部の資源(script / link / img の http(s) URL)を参照していない。依存パッケージが増えていない(`pyproject.toml`、`uv.lock` に差分なし)。(レビュー)
- [ ] 接続ダイアログに「接続を解除するには、出力先の .pocket ディレクトリを削除してください。」の注記が無い。曲一覧の操作列に新しいボタンが増えていない。(レビュー)
- [ ] README の追記が「確定した設計」の 23 と一致する。(レビュー)

## 未確定事項・リスクと判断の委ね方

| 項目 | 内容 | 実装時の扱い |
|---|---|---|
| `auth/params` の応答形 | 契約文書に明記が無く、音源ポケットの Worker のテストで確かめられている形に頼る | 形が違う応答は「読めない」として `unknown` に落とす(安全側)。契約文書への追記を音源ポケット側へ提案する |
| 照合の回数と間隔 | 3 回・5 秒は推定 | 定数にして変えやすくする。実機で「不一致のまま後から反映」が観測されたら増やす |
| 遅れて届く要求 | 照合後に前回の要求が反映される可能性が理屈上残る | 再実行で同じパスワードを使う案内を文言と README に入れる |
| 503 の表示 | 契約は「auth を変えずに 503」だが保存系障害を含む | 断定せず再実行を案内する。照合もしない |
| ブラウザのパスワード保存の誘い | `type="password"` の欄は保存を勧められることがある | `<form>` を使わず、送信後に欄を空にする。README に「保存しない」を勧める 1 行 |
| 同一オリジンの要求に Origin が付く前提 | 古いブラウザで欠落すると、接続・再設定・接続解除の 3 入口が 403 になる | 実ブラウザのテスト(Chromium)で確認。403 の文言で理由が分かるようにする |
| 入口ごとに守りの強さが異なる | 既存の入口は Origin 欠落を通し、新しい 3 入口だけ Origin を必須にする | 意図的。`pocket_settings.py` のコメントに理由を書く。既存の middleware は変えない |
| `--token-stdin` の削除による互換性 | 標準入力から upload token を渡して `connect` を自動化していた使い方は止まる | TTY でないときの文言で端末と Web UI を案内する。README に端末入力が必須であることを書く。リリースの変更点への記載は、版数の書き換えの作業で行う |
| 公開テスト値の置き場所 | テストに直接記す | 次回のスナップショット更新時に `MIRRORED` へ `fixtures/auth-v1` を追加し、参照に切り替える(後続作業) |
| PBKDF2 のテスト時間 | 600,000 iterations を Python で数回、ブラウザで数回 | 1 回 1 秒未満の想定。CLI と Web API のテストは偽クライアントで導出を抑え、実ブラウザのテストは 1〜2 ファイルに集約する |
| 偽の音源ポケットの共有 | 既存 `tests/test_pocket_http.py` の `Handler` は capabilities / reset / auth_params を返さない | `tests/pocket_fake.py` に新設し、HTTP・Web API・実ブラウザのテストで共用する |
| 正規化しないことの利用者影響 | 入力方法で同じ見た目が別の文字列になる | README に注意を書く。Bunri 側で救済はしない |
| 秘密のメモリ残留 | Python と JS では確実に消せない | 参照の早期解放と入力欄のクリアまでを実装上の配慮とし、完了条件にはしない。README に限界を書く |
| CLI `connect` の内部 | Web と同じ `connect_pocket` を使う方が重複が無い。CLI は `OSError` を `str(exc)` で表示しており host 名が入り得る | この計画では CLI を `connect_pocket` へ切り替えず、`OSError` の表示も変えない。`--token-stdin` の削除と TTY 検査だけを行う |
| 過去の文書の `--token-stdin` | `docs/plans/` と `docs/reviews/` に記述が残る | 当時の記録なので変更しない |

## 公開経路

- 実装担当は作業ブランチ `plan/2026-10-12-pocket-reset-password`(ベース main、SHA `3f628a3ec5ccc03fd55278850b9e41aa1e823a31`)に commit するところまで。push、PR の作成、リリースは行わない。
- 計画書(`docs/plans/2026-10-12-pocket-reset-password/plan.md`、`request.md`)は、レビューと共有内容の検査の後、実装と同じ作業ブランチに、実装の commit とは別の commit で含める。
- push と PR の作成は、結果の報告と了解の後に別途行う。CI(GitHub Actions の `CI` workflow の `test` job)が PR と main への push で `uv sync --frozen --extra web`、Playwright の Chromium の導入、`uv run pytest -q -n auto` を実行する。
- リリース(v0.9.0 の版数の書き換えとタグ)は、同じく結果の報告と了解の後に、別の作業として行う。
- 本物の音源ポケット(0.4.0 以降)に対する実行は、了解がある場合だけ行う。テストと CI では実行しない。
