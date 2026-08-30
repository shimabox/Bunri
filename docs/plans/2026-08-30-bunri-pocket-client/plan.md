# Bunri Pocket クライアント（`bunri pocket connect / sync`）実装計画

- 日付: 2026-08-30
- ブランチ: `plan/2026-08-30-bunri-pocket-client`
- 実装担当(駒): Sol medium（Typer 互換、ファイルシステム安全性、標準ライブラリ HTTP、protocol v1 の byte 互換という正確さが要る境界が多く、定型的な実装ではないため）
- 契約の正本: `github.com/shimabox/bunri-pocket` commit `a8efc3e20a5009c72c1d5bcdd07db6e046c84ceb`

## 環境情報

- リポジトリ: `github.com/shimabox/Bunri`
- ベースブランチ: `main`
- ベース SHA: `4752e02bb2f8e7b0edcc8596858f90f78764bee3`
- 現行バージョン: `0.5.0`
- Python: `>=3.13`
- CLI: Typer `0.26.8`、Click `8.4.2`
- 対象テスト: `uv run pytest -q -n auto tests/test_cli.py tests/test_package.py tests/test_package_metadata.py tests/test_pocket_*.py tests/test_web_api.py tests/test_web_jobs.py`
- 全テスト: `uv run pytest -q -n auto`（`make test` も同じコマンドを実行する）
- lock/build 検証: `uv lock --check`、`uv build`
- CI: `.github/workflows/ci.yml` で Ubuntu、ffmpeg、`uv sync --frozen --extra web`、Playwright Chromium、`uv run pytest -q -n auto` を使用する
- lint: 専用 lint/format コマンドはないため、既存テストと `git diff --check` を検証ゲートにする

## 背景・目的

Bunri がローカルで分離した練習用 MP3 を、利用者が所有する Cloudflare Workers + R2 の棚「Bunri Pocket」へ明示的に同期できるようにする。入力音源を分離処理のために外部へ送信しない既存の性質は維持し、利用者が `bunri pocket connect / sync` を実行した場合にだけ、完成したパッケージの MP3 と protocol v1 の manifest/library を送るオプトイン機能とする。

同期元は Bunri が生成したサイドカーと検証済み実ファイルだけに限定する。ローカルの全検査、リモート identity の事前確認、media → manifest → library の順序、条件付き JSON 更新、再実行時の no-op 判定により、途中失敗後も削除や rollback をせず収束できる経路を作る。

## スコープ

### やること

- 既存の単一コマンドを維持したまま `bunri pocket connect / sync` を追加する。
- 入力 SHA-1 の完全な40桁 digest と既存12桁 cache key を1回の読み取りで取得し、既存 cache directory 名を維持する。
- `out/<safe>/.bunri-package.json` を生成成功のサイドカーとして、symlink 非追従かつ原子的に管理する。
- サイドカーの対象 target を生成前に無効化し、音声と player の成功後に今回成功した形式だけで再追加する。
- `out/.pocket/config.json` に出力先ごとの接続先と upload token を権限付きで保存する。
- package directory、サイドカー、MP3、source identity を HTTP 開始前に全件検査する。
- protocol v1 の manifest/library を検証・生成・未知フィールド保持マージし、上流と同一 byte の stable JSON を出力する。
- redirect を追従しない Python 標準ライブラリ HTTP client を実装し、media を file object のまま送る。
- media → manifest → library の順で冪等同期し、manifest と library の競合を契約に応じて処理する。
- `github.com/shimabox/bunri-pocket` の指定 commit から schema、valid/invalid fixture、media、stable JSON golden、出所情報を snapshot する。
- README の英語冒頭と日本語本文で、分離処理の不送信と Pocket への明示的なオプトイン送信を書き分ける。
- loopback 偽サーバーを含む自動テストと、所有者による実棚確認の検収ゲートを設ける。

### やらないこと

- `bunri pocket sync --all`
- リモート上の曲、asset、instrument の削除
- ローカルに存在しないリモート曲、target、未知フィールドの削除
- Web UI への「棚へ送る」ボタン
- 自動同期、バックグラウンド同期、同一棚に対する複数 sync process の協調
- Job JSON、`/api/songs[].downloads`、`_download_files()` を同期元にすること
- サイドカーの手作業生成、旧パッケージの推測移行
- Pocket Worker/PWA の変更、R2 S3 API の直接利用、他 storage 対応
- 外部 HTTP client や JSON Schema validator の runtime dependency 追加
- Bunri の version、`src/bunri/__init__.py`、`tests/test_smoke.py`、`uv.lock`、タグの更新
- 実装担当へ本番 upload token を渡すこと、実装担当による実棚書き込み

## 方針

### 1. モジュール境界

Pocket 機能を生成処理、protocol、HTTP、CLI に分離する。

- `src/bunri/package_metadata.py`: サイドカーの型、検証、原子的読み書き、target の無効化と成功時マージ
- `src/bunri/pocket/cli.py`: `connect`、`sync` の Typer command と利用者向け表示
- `src/bunri/pocket/config.py`: URL/token 検証、hidden input、capabilities 確認、設定保存
- `src/bunri/pocket/protocol.py`: protocol validator、stable JSON、manifest/library の生成と未知フィールド保持マージ
- `src/bunri/pocket/local.py`: package preflight、symlink 非追従検査、asset の SHA-256/size 算出
- `src/bunri/pocket/http.py`: redirect 非追従 opener、GET/HEAD/PUT、上限付き response、secret を持たない error
- `src/bunri/pocket/sync.py`: remote preflight、同期順序、412 再取得・再マージ、集計

`src/bunri/package.py` はサイドカー層（`package_metadata.py`）だけに依存し、HTTP 層や Pocket CLI を import しない。`pocket/protocol.py` も CLI や HTTP を import せず、clock と serializer を注入可能な純粋処理を中心にする。

### 2. 既存 CLI を維持する dispatcher

既存 Typer `app` は単一 command のまま残す。トップレベルへ通常の subcommand を足すと Typer が group 表示へ切り替わり、`bunri song.mp3`、help、validation error の互換性が崩れるため、entry-point dispatcher を使う。

- `pyproject.toml` の entry point を `bunri = "bunri.cli:dispatch"` に変更する。
- `dispatch()` は argv の先頭 token が文字どおり `pocket` と一致するときだけ、その token を除いて Pocket app を `prog_name="bunri pocket"` で呼ぶ。
- それ以外は既存 `app` を `prog_name="bunri"` で呼び、引数、表示、exit code を変えない。
- `python -m bunri.cli` も `dispatch()` を通す。
- `src/bunri/web/jobs.py` が起動する `python -m bunri.cli <input> ...` は従来の生成経路へ到達する。
- 実在する入力ファイル名 `pocket` は `./pocket` と指定すれば既存 CLI へ渡せる。これを回帰テストと README に固定する。

変更前後で、`bunri --help`、実在入力、存在しない入力、未知 target、未知 device、未知 option、引数なし、`python -m bunri.cli --help`、Web runner の argv の出力と exit code を、既存 `_STABLE_TERMINAL` と `_plain()` を使って比較する。

### 3. 入力 digest とサイドカー

`src/bunri/cache.py` に `InputDigest(full_sha1, cache_key)` 相当の内部値と、入力を1回だけ読む API を追加する。`cache_key == full_sha1[:12]` とし、既存 `file_digest()` は12桁を返す互換 wrapper として残す。cache directory は従来どおり `out/.cache/<cache_key>/` とし、完全 digest の identity を記録・検査して、同じ12桁へ異なる完全 digest が到達した場合は既存 artifact を使わず停止する。

サイドカー v1 は次の形を正とする。

```json
{
  "schema_version": 1,
  "title": "曲名",
  "safe_name": "曲名",
  "source": {
    "algorithm": "sha1",
    "digest": "40桁の小文字16進SHA-1",
    "cache_key": "先頭12桁"
  },
  "targets": [
    {
      "target": "guitar",
      "formats": ["mp3", "wav"]
    }
  ]
}
```

- `schema_version` は文字列ではなく整数 `1` とする。
- `title` は player に表示する元の title、`safe_name` は実 directory 名とする。
- `source.algorithm == "sha1"`、digest、cache key、prefix の一致を必須にする。
- target は `REGISTRY` にある `original` 以外の値だけを許可し、重複を拒否して target 名順に保存する。
- `formats` は今回の実行で target/backing の生成に成功した形式だけを、重複なしの規定順 mp3 → wav で持つ。通常実行は `["mp3","wav"]`、`--no-mp3` は `["wav"]` とする。
- 過去の実行で残った MP3 が disk にあっても、今回の `formats` に `mp3` がなければ新しい成果物とみなさない。

更新は必ず二段階にする。

ここで二段階とは、(a) 生成前に現在 target の entry を除いた sidecar を原子的に保存し、(b) 全生成成功後に今回の `formats` で再追加することであり、以下はその手順の内訳である。

1. digest、safe name、package directory の安全性を確定し、既存 sidecar があれば同一 identity として検証する。異なる完全 digest なら artifact へ触れる前に停止する。
2. 現在の target entry を除いた sidecar を、他 target を維持したまま同一 directory の一時ファイルから `os.replace()` で原子的に保存する。
3. normalize、separate、target/backing、original、player を生成する。
4. すべて成功した場合だけ sidecar を直前に再読込し、同じ identity と他 target を再検証して、現在 target を今回成功した `formats` で追加する。
5. sidecar 保存に失敗した場合は現在 target を同期可能として記録しない。

これにより、再生成中の失敗や `--no-mp3` 実行後に古い MP3 が成功済み target として残らない。同一出力 directory への複数 CLI process の同時実行は既存と同じく非対応とし、lock は追加しない。

### 4. sync 前のローカル preflight

`sync <曲名>` の値は表示 title ではなく、指定した `-o` の直下にある単一 directory 名（safe name）として扱う。

- 空、絶対 path、`.`、`..`、`/`、`\\` を含む値、dot 始まり、`.pocket`、`.cache`、`web` を拒否する。
- package directory 自体が symlink なら拒否する。
- sidecar と対象 MP3 は `lstat` と `is_real_file_in()` 相当で、package directory 直下の通常 file であることを確認する。
- sidecar の `safe_name` と実 directory 名、source identity、target、formats を検証する。
- 各 target は `formats` に `mp3` を含み、`<safe>.<target>.mp3` と `<safe>.<target>.backing.mp3` が実在することを必須にする。
- `--no-original` がない場合は `<safe>.original.mp3` も必須にする。
- WAV と player は upload 対象でも sync 時の必須 file でもない。
- 未知 target、MP3 形式なし、片側欠損、空 file、symlink、path/identity 不整合を全件収集してから一括表示する。
- 1件でも違反があれば HTTP request を開始しない。

設定がなければ先に `bunri pocket connect` を実行するよう案内して停止する。package が見つからない場合は、`out/` 直下の内部 directory、dot directory、symlink を除く候補を一覧してよい。旧 package に sidecar がない場合は推測や Job JSON からの移行をせず、元入力からの再生成と cache 再利用を案内する。MP3 がない target は target ごとに列挙し、`--mp3` を有効にした再生成を案内する。

### 5. protocol v1 の manifest と library

ローカル file 名と remote asset path を分ける。manifest の既知 field は次から作る。

| manifest field | 値・出所 |
|---|---|
| `schema_version` | 新規文書は文字列 `"1.0"` |
| `song_id` | sidecar の `source.cache_key` |
| `title` | sidecar の `title` |
| `source` | sidecar の `algorithm`、40桁 digest、12桁 cache key |
| `original.path` | `original.mp3` |
| `instruments[].target` | sidecar の target |
| `instruments[].label` | `REGISTRY[target].label_ja` |
| target stem path | `<target>.mp3` |
| backing stem path | `<target>.backing.mp3` |
| `content_type` | `audio/mpeg` |
| `bytes` | 検証済み local MP3 の正の size |
| `sha256` | local MP3 の64桁小文字 SHA-256 |
| `updated_at` | 実質的変更時だけ現在 UTC の RFC3339 `Z` |

remote manifest は全体を検証してから、既知 field だけを更新する。

- protocol `1.x` を受理し、既存 `schema_version` は変更しない。未知 major、形式不正、cross-field 不正は停止する。
- top-level、`source`、instrument、stem/asset の未知 field を維持する。
- instrument は target、stem は role を key にマージし、remote にだけある target は削除しない。
- instruments は target 名順、stems は target → backing の順にする。
- remote `source.digest` が sidecar と異なる場合は12桁衝突として扱う。
- `--no-original` では新規 manifest の `original` を `null` にするが、既存 original は維持する。削除指定にはしない。
- 現在の `updated_at` を保持した candidate の stable byte が既存文書と同じなら PUT しない。実質的変更がある場合だけ時刻を更新する。

library は `song_id` で対象曲をマージする。

- 他曲、対象曲や instrument の未知 field、remote にだけある instrument を維持する。
- 対象曲の title、`tracks/<song-id>/manifest.json`、`has_original`、instrument 一覧は確定した manifest から作る。
- song の `updated_at` は確定 manifest の値とする。
- library top-level の `updated_at` は library 自体に実質的変更がある場合だけ更新する。
- songs は `song_id`、instruments は target 名順にする。
- 変更がなければ library PUT を省略する。

上流 valid fixture の label `"Guitar"` は protocol の受理テストにそのまま使う。Bunri 生成器の golden は別に置き、`REGISTRY["guitar"].label_ja == "ギター"` を固定する。

### 6. stable JSON の JavaScript byte 互換

Python の serializer は、上流 commit の `src/protocol/stable-json.ts` が `sortValue()` → `Object.fromEntries()` → `JSON.stringify()` の結果に `"\n"` を1つ付加して出す byte と一致させる。Python 側も直列化結果に LF を1つ付加し、通常の `json.dumps(sort_keys=True)` や Python float の通常表現だけでは実装しない。

- ECMAScript array-index property key、すなわち 0〜4294967294 の正準10進表記（`"0"`、`"12"`、`"4294967294"`。先頭ゼロ、符号、小数点付きは除外）は数値昇順で object の先頭へ並べる。
- それ以外の key（`"01"`、`"1.0"`、`"-1"`、`"4294967295"`、通常の文字列）は Unicode code point 順にする。
- 全 object に再帰適用し、array 順は保持する。
- JSON number は字句を保持せず、JSON parse 後の ECMAScript Number と `JSON.stringify` の表現を再現する。`1.0` → `1`、`-0` → `0`、10^21 以上は `1e+21` のような指数表記、小さい値は `1e-7` のような表記、それ以外は Number::toString の最短往復表現とする。NaN と ±Infinity は `null` とする。
- client が生成する protocol number は正の整数だけだが、remote の未知 field にある number も同じ Number 規則で parse・再直列化する。
- UTF-8、BOM なし、非 ASCII は `\\uXXXX` 化しない。quote、backslash、control character は JSON 標準の短縮 escape と `\\u00XX` を使う。
- 不要な空白なし、末尾 LF はちょうど1つとする。`process.stdout.write(stableJson(value))` で生成する上流 golden は追加加工なしで LF を含み、Python serializer との byte 比較も LF を含めて行う。

整数風 key の実 byte は上流実装を正とし、code point 順だけへ理想化しない。上流文書の文言修正は `bunri-pocket` 側の独立した指摘とし、この計画へ含めない。

### 7. 設定ファイルと connect

`connect` は token を argv で受け取らない。通常は `getpass`/Typer の hidden prompt、非対話時だけ `--token-stdin` で1行を読む。trim 後に padding なし base64url と decoded 32 byte 以上を検証し、token を成功表示、log、例外、dataclass `repr`、HTTP 診断へ含めない。

base URL は原則 HTTPS とする。hostname `localhost` または loopback IP に限り HTTP を許可し、userinfo、query、fragment を拒否する。末尾 `/` は除き、既存 path prefix は維持して `/api/v1/...` を追加する。redirect は全 status で追従しない。

保存前に Bearer token 付き `GET /api/v1/upload/capabilities` を行い、API major 1、manifest/library major 1 と latest `1.0`、media limit `94371840`、JSON limit `1048576`、`audio/mpeg`、`SHA-256`、conditional JSON PUT を検証する。

`out/.pocket/config.json` は整数 `schema_version: 1`、正規化済み `base_url`、`token` を持つ。`out/.pocket` が symlink でない実 directory であることを確認し、POSIX では directory `0700`、同一 directory の一時 file `0600`、flush、必要な fsync、`os.replace()` の順で保存し、保存後 mode を再検査する。権限を保証できない filesystem では、平文 token を保護できない旨を明示して継続する。成功時は token が平文であること、`out/.pocket` の削除で接続情報を消せること、設定は `-o` ごとに独立することを表示する。

### 8. 標準ライブラリ HTTP

Python 3.13 の `urllib.request` と `http.client` を使い、追加依存を導入しない。

- `HTTPRedirectHandler.redirect_request()` を差し替え、301、302、303、307、308 を含む redirect を追従しない。
- opener、clock、timeout をテストで注入可能にする。
- metadata request の timeout は30秒、media PUT は300秒の定数とする。
- 全 request に `Authorization: Bearer ...` を付けるが、error object は status、server code、secret を除いた URL だけを保持する。
- media PUT は file object を `data` として渡し、`Content-Length`、`Content-Type: audio/mpeg`、`X-Bunri-Content-SHA256` を明示して chunked にしない。
- JSON PUT は stable byte を memory 上で作り、1 MiB 上限を送信前に検査する。
- response body は上限付きで読み、error envelope の `error.code`、`supported_schema_major`、`Retry-After` だけを安全に扱う。
- 401、409、412、413、422、428、429、503 を code 別の利用者向け message に変換する。429 は `Retry-After` を表示するが自動待機しない。
- 409 major 不一致、428、412 retry 上限は client/server 契約差または競合として更新・再実行案内を出す。

標準ライブラリで streaming PUT または redirect 拒否が loopback 結合テストで成立しない場合は、まず `http.client` 直接利用という標準ライブラリ内の代替を検討する。外部 HTTP dependency を追加する必要が生じた場合は、lock、配布、保守への影響を報告して停止する。

### 9. remote preflight、同期順序、競合

local preflight と全 asset の SHA-256/size 算出を、最初の HTTP request より前に完了する。続いて media 書き込み前に remote manifest と library を GET し、schema major、文書全体、route identity、完全 digest を検査する。manifest の完全 digest が異なる場合は media PUT を1件も行わず12桁衝突として停止する。

library に対象 song があるのに manifest が404の場合は、参照が先行した不整合状態として write 前に停止する。manifest があり library に対象 song がない場合は、前回の library 更新失敗からの回復経路として許可する。

書き込み順は次とする。

1. media
   - original（`--no-original` でない場合）、target 名順の target → backing の順
   - HEAD の `X-Bunri-Content-SHA256` と `Content-Length` が両方一致すれば冪等成功として skip する
   - 404、metadata 欠落、不一致なら PUT する。送信時は `Content-Length` と `X-Bunri-Content-SHA256` を明示し、成功応答は `X-Bunri-Content-SHA256` が期待値と一致することを検査する。応答の `Content-Length` は空 body のため0となり、検証には使わない
2. manifest
   - media 完了後に最新を GET して再検証・再マージする
   - 新規は `If-None-Match: *`、既存は strong ETag の `If-Match` を付ける
   - no-op なら PUT しない
   - 412 の場合は最新を再 GET する。`source.digest` が自分の完全 digest と同じなら再マージして、初回に加えて最大3回再試行する。異なれば12桁衝突として即時停止し、preflight 後に media を上書きした可能性を利用者へ報告する
3. library
   - manifest が成功または no-op と確定してから最新を GET・再マージする
   - manifest と同じ条件付き作成/更新を使う
   - 412 は最新を再 GET・再マージし、初回に加えて最大3回再試行する。上限超過は停止する

media 途中失敗では manifest/library を更新しない。manifest 失敗では library を更新しない。library 失敗では media/manifest が残るが、再実行時に一致 asset と no-op manifest を skip して library へ収束する。変更なしの再実行では `updated_at` と ETag を変えない。

同じ棚に対する複数 process の同時 sync は非対応とする。特に、双方が preflight で manifest 不在を確認し、異なる曲の同じ12桁 key へ音声を書いた後、後着 manifest が412で衝突を知る TOCTOU が残る。この場合、後着が先着 media を上書きした可能性があり、client は削除や rollback をせず、明示的な警告とともに停止する。

完了表示は media の uploaded/skipped 数、manifest と library の updated/skipped 数、接続先の棚 URL を示し、token と Authorization は出さない。

### 10. 契約 snapshot と golden

fixture は次の構成とする。

```text
tests/fixtures/bunri_pocket_protocol_v1/
├── UPSTREAM.md
├── schemas/
│   ├── manifest-v1.schema.json
│   └── library-v1.schema.json
├── valid/
│   ├── library-v1.json
│   ├── manifest-v1-no-original.json
│   ├── manifest-v1-unknown-fields.json
│   └── manifest-v1.json
├── invalid/
│   ├── library-duplicate-song-id.json
│   ├── library-invalid-manifest-path.json
│   ├── manifest-bad-cache-key.json
│   ├── manifest-bad-major.json
│   ├── manifest-duplicate-target.json
│   ├── manifest-invalid-path.json
│   ├── manifest-missing-backing.json
│   ├── manifest-reserved-target.json
│   └── manifest-wav-path.json
├── media/
│   └── sample.mp3
├── stable/
│   ├── manifest-v1.stable.json
│   ├── library-v1.stable.json
│   ├── number-forms.stable.json
│   ├── unicode-keys-nested.stable.json
│   └── non-finite.stable.json
└── generated/
    ├── manifest-v1-guitar-ja.json
    └── library-v1-guitar-ja.json
```

snapshot 元の repository-relative path は次で固定する。

```text
schemas/manifest-v1.schema.json
schemas/library-v1.schema.json
fixtures/protocol-v1/valid/library-v1.json
fixtures/protocol-v1/valid/manifest-v1-no-original.json
fixtures/protocol-v1/valid/manifest-v1-unknown-fields.json
fixtures/protocol-v1/valid/manifest-v1.json
fixtures/protocol-v1/invalid/library-duplicate-song-id.json
fixtures/protocol-v1/invalid/library-invalid-manifest-path.json
fixtures/protocol-v1/invalid/manifest-bad-cache-key.json
fixtures/protocol-v1/invalid/manifest-bad-major.json
fixtures/protocol-v1/invalid/manifest-duplicate-target.json
fixtures/protocol-v1/invalid/manifest-invalid-path.json
fixtures/protocol-v1/invalid/manifest-missing-backing.json
fixtures/protocol-v1/invalid/manifest-reserved-target.json
fixtures/protocol-v1/invalid/manifest-wav-path.json
fixtures/protocol-v1/media/sample.mp3
src/protocol/stable-json.ts
```

- schema、valid/invalid、media は上記の repository-relative path から内容を変えず snapshot する。`media/` の元 directory は `fixtures/protocol-v1/` 配下であり、`sample.mp3` の元 path は `fixtures/protocol-v1/media/sample.mp3` とする。
- `media/sample.mp3` は24 byte、SHA-256 `01316c8ec960ebe91747508e865d42eef794073d8d6c17eeb87d6f495bcb760b` として upstream manifest fixture と一致することを確認する。
- `stable/` は指定 commit の `src/protocol/stable-json.ts` にある `stableJson()` を `npx tsx` で呼ぶ一時 script から作る。`process.stdout.write(stableJson(value))` の出力は追加加工なしで末尾 LF を含み、その script はどの repository にも残さない。
- golden は上流 valid manifest/library に加え、`1.0` の整数化、`-0`、10^21 と小数側の指数表記、非 ASCII、補助平面文字、array-index 境界の `"0"`、`"12"`、`"4294967294"`、`"4294967295"`、`"01"`、`"1.0"`、`"-1"`、nested object/array、NaN/Infinity の `null` 化を含める。
- `UPSTREAM.md` に upstream repository、full commit、各元 file の repository-relative path、`npx tsx` による生成手順、更新方法、上流 `"Guitar"` fixture と Bunri の `"ギター"` generator golden の役割の違いを記録する。
- test runtime は隣接 clone を参照せず Bunri 内 snapshot だけを使い、外部 schema validator を追加しない。

### 11. README

README の英語冒頭と日本語本文の両方に、次を矛盾なく書く。

- 分離処理のために入力音源を外部へ送信することはない。この性質は変わらない。
- `bunri pocket` を明示的に実行した場合だけ、分離後の MP3 を利用者所有の Pocket storage へ送るオプトイン機能である。
- `bunri pocket connect <URL> -o out`、`bunri pocket sync '<safe name>' -o out`、`--no-original` の最小例。
- 設定は `out/.pocket/config.json` に平文 token を保存し、`out/.pocket` の削除で消せる。
- bare file 名 `pocket` は command 名と衝突するため、入力としては `./pocket` と指定する。

既存方針に合わせ、通常段落の途中へ手動改行を入れない。

### 12. 公開と検証の境界

本計画は機能、README、自動テスト、lock/build 検証までを対象とする。version 更新と公開は独立したリリース工程とし、次を本実装で変更しない。

- `pyproject.toml` の version
- `src/bunri/__init__.py`
- `tests/test_smoke.py`
- `uv.lock`
- release commit と `vX.Y.Z` tag

本番 token を使う remote 検証は所有者が行う。実装担当は token を受け取らない。

## タスク分解

| # | タスク | 依存 |
|---|---|---|
| 1 | 現行 CLI の help、成功、validation error、module invocation、Web runner argv を回帰テストとして固定する | - |
| 2 | 指定 upstream commit の schema、valid/invalid fixture、media、stable JSON golden、出所情報を test asset へ取り込む | - |
| 3 | protocol validator、JS 互換 stable JSON、timestamp、manifest/library の生成と未知 field 保持 merge を実装する | 2 |
| 4 | `cache.py` を full SHA-1 + 12桁 cache key の単一読み取り API に整理し、cache path と互換 wrapper を維持する | - |
| 5 | sidecar v1 の検証、identity 衝突拒否、target の事前無効化、成功後の形式付き再追加、原子的保存を実装し `build_package()` へ接続する | 4 |
| 6 | Web 経由も同じ生成経路を通ることと、sidecar が `/packages` から404になることをテストする | 5 |
| 7 | package path、symlink、sidecar、formats、MP3、digest を全件検査し、asset hash/size を作る local preflight を実装する | 3, 5 |
| 8 | Pocket URL/token、hidden input、capabilities、0700/0600 の config 原子的保存を実装する | 3 |
| 9 | redirect 非追従、secret redaction、file-object streaming PUT、error envelope、30/300秒 timeout を扱う HTTP 層を実装する | 3 |
| 10 | remote identity preflight、media/manifest/library、種類別412処理、no-op、`--no-original` を同期 orchestration へ実装する | 7, 8, 9 |
| 11 | entry-point dispatcher と `pocket connect/sync` を配線し、タスク1の既存 CLI と `./pocket` の挙動を確認する | 8, 10 |
| 12 | `http.server.ThreadingHTTPServer` の loopback 偽サーバーで wire-level の同期、redirect、競合、失敗、再実行収束を検証する | 9, 10 |
| 13 | README の送信方針、Pocket 最小手順、平文 token、`./pocket` の注意を更新する | 11 |
| 14 | 対象テスト、全テスト、`uv lock --check`、`uv build`、`git diff --check` を実行する | 1-13 |
| 15 | 所有者が実棚で connect、初回 sync、全 skip、別 target 差分を確認する | 14 |

## 完了条件・受け入れ基準

- [ ] `bunri song.mp3`、`bunri --help`、既存 validation error の出力と exit code が dispatcher 追加前から変わらない。
- [ ] `python -m bunri.cli` と console script の両方で既存 command と Pocket command が動く。
- [ ] 先頭 token が正確に `pocket` の場合だけ Pocket app へ渡り、実在 file `./pocket` は既存入力として扱われる。
- [ ] Web job で生成された package にも同じ sidecar が生成される。
- [ ] SHA-1 は1回の読み取りで40桁と12桁を得て、既存 cache directory 名は変わらない。
- [ ] 異なる完全 digest の sidecar、cache identity、remote manifest を検出した場合、対象 artifact または remote media を上書きする前に停止する。ただし remote 同時 sync の既知 TOCTOU は明示警告する。
- [ ] sidecar の `schema_version` は整数1であり、同じ digest の別 target が target 名順で加算される。
- [ ] sidecar の `formats` は今回成功した形式だけを mp3 → wav 順で持ち、`--no-mp3` は `["wav"]` になる。
- [ ] target の生成開始前に古い entry が原子的に無効化され、途中失敗時に成功済みとして残らない。他 target は維持される。
- [ ] `/packages/<safe>/.bunri-package.json` が404になる。
- [ ] 旧 package、MP3 無効 target、欠損/空 MP3、未知 target、symlink、path/identity 不整合をまとめて表示し、HTTP request を1件も送らない。
- [ ] manifest の label が `REGISTRY[target].label_ja` になる。
- [ ] remote の他曲、未知 target、全階層の未知 field を read-modify-write で維持する。
- [ ] protocol `1.x` を受理し、未知 major では更新案内付きで停止する。
- [ ] stable JSON が上流 golden と byte 単位で一致し、array-index key、Unicode、Number 表現、末尾 LF の境界を通る。
- [ ] upstream の `"Guitar"` fixture を受理し、Bunri generator golden は `"ギター"` になる。
- [ ] config directory/file が POSIX 対応 filesystem で0700/0600になり、保証不能時は明示警告する。
- [ ] token が stdout、stderr、例外、`repr`、HTTP 診断に現れない。
- [ ] redirect を1回も追従せず、metadata 30秒/media 300秒の timeout が注入可能である。
- [ ] media PUT が file 全体を memory に載せず、明示 `Content-Length` で送られる。
- [ ] media は HEAD の SHA-256/size 一致で skip し、PUT は `Content-Length` と `X-Bunri-Content-SHA256` を明示して送信し、応答の `X-Bunri-Content-SHA256` 不一致で停止する。空 body を示す応答の `Content-Length: 0` は検証に使わない。
- [ ] manifest/library は実質的変更なしで `updated_at` を変えず PUT もしない。
- [ ] library の412は再取得・再マージし、manifest の412は同一 digest のときだけ再マージする。いずれも初回 + 最大3回で、上限超過は停止する。
- [ ] manifest の412後に異なる digest を見つけた場合は即時停止し、media 上書き可能性を表示する。
- [ ] `--no-original` が既存 remote original を削除しない。
- [ ] media、manifest、library の各段階で失敗させても、次回 sync で収束する。
- [ ] README の英語・日本語に分離処理の不送信と Pocket のオプトイン送信が明記される。
- [ ] `uv run pytest -q -n auto`、`uv lock --check`、`uv build`、`git diff --check` が通る。
- [ ] version、`src/bunri/__init__.py`、`tests/test_smoke.py`、`uv.lock`、tag が本変更で更新されていない。

## 人手確認

upload token を持つ所有者が `https://bunri-pocket.orukubami.sh` に対して次を確認する。token は実装担当や実施報告へ共有しない。

1. `connect` が capabilities を検証して設定を保存する。
2. 初回 `sync` で `tracks/<song-id>/...` の media、manifest、library が規定 key に作られる。
3. 同じ `sync` の再実行が全 media/document を skip し、ETag と `updated_at` を変えない。
4. 別 target の生成・同期では、その target の2 media と manifest/library だけが差分になる。
5. `--no-original` の初回・既存 manifest 双方で、original の非追加・非削除が契約どおりになる。

## 未確定事項・リスクと判断の委ね方

| 項目 | 内容 | 実装時の扱い |
|---|---|---|
| identity の無い旧キャッシュ | `.bunri-input.json` が無い既存 `out/.cache/<cache_key>/` は入力を照合できず、別入力の12桁衝突時に既存 stage を再利用し得る(理論上の 48bit 衝突) | 採用して現在の full digest を identity として記録する(現状維持)。旧キャッシュの無効化・再分離は行わない。identity が記録された以降の衝突は検出して停止する |
| 標準ライブラリ streaming | Python 3.13 の `urllib` で file-object PUT と redirect 拒否を wire-level で確認する必要がある | `urllib`、次に `http.client` 直接利用まで判断してよい。外部 dependency が必要なら追加せず報告して停止する |
| JS Number byte 互換 | remote の未知 field も ECMAScript Number の parse/serialize 規則へ合わせる必要があり、Python の既定表現では不十分 | 上流生成 golden を正とする。golden 不一致を近似で通さず、原因と選択肢を報告して停止する |
| POSIX 権限非対応 filesystem | chmod 後も0700/0600を保証できない環境がある | mode 再検査後に平文 token の保護不能を明示して継続する。黙って成功扱いしない |
| 同一出力の同時 CLI | package、sidecar、cache の複数 process 更新は協調しない | 今回は非対応。lock を追加せず、必要なら別 scope として報告する |
| 同一棚の同時 sync | preflight と manifest PUT の間に media が上書きされ得る TOCTOU がある | 非対応として明記し、412 で異 digest を検出したら上書き可能性を警告して停止する。rollback/delete はしない |
| remote 部分状態 | R2 全体を跨ぐ transaction はなく、library だけ遅れる場合がある | media → manifest → library と冪等再実行で収束させる。自動削除や rollback は追加しない |
| package 候補表示 | 存在しない safe name に対して候補の選び方・表示数は厳密指定しない | `out/` 直下の内部名・symlink を除く同期可能そうな directory を安全に列挙する範囲で実装者が決めてよい |
| 公開 version | 機能追加後の release version はこの計画で決めない | version bump、lock 更新、release commit、tag は独立したリリース工程で決める |
