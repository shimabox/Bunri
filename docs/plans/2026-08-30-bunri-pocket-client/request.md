# 実装依頼: Bunri Pocket クライアント（`bunri pocket connect / sync`）

## 背景

Bunri がローカルで生成した練習用 MP3 を、利用者自身が所有する Cloudflare Workers + R2 の Bunri Pocket へ明示的に同期できるようにする。分離処理のために入力音源を外部へ送らない既存の性質は維持し、`bunri pocket` を利用者が実行した場合だけ完成済み MP3 を送るオプトイン機能とする。

## 対象

- リポジトリ: `github.com/shimabox/Bunri`
- ベースブランチ: `main`（参考。起点は次の SHA とする）
- ベース SHA: `4752e02bb2f8e7b0edcc8596858f90f78764bee3`
- 作業ブランチ: `plan/2026-08-30-bunri-pocket-client`
- 実装担当(駒): Sol medium（Typer 互換・ファイルシステム安全性・標準ライブラリ HTTP と、正確さが要る境界が多く定型ではないため）
- 契約の正本: `github.com/shimabox/bunri-pocket` commit `a8efc3e20a5009c72c1d5bcdd07db6e046c84ceb`

リポジトリルートで、必ずベース SHA から作業ブランチを作成して着手する。

```bash
git switch -c plan/2026-08-30-bunri-pocket-client 4752e02bb2f8e7b0edcc8596858f90f78764bee3
```

契約は clone の配置を仮定せず、上記 repository の上記 commit を取得して参照する。実装時・テスト時に別 clone を runtime dependency にせず、必要な契約資産は指定 commit から Bunri の fixture へ snapshot する。

## 確定仕様

### 1. 変更後の構成

次を基準に実装する。小さな private helper の配置は責務を崩さない範囲で調整してよい。

```text
src/bunri/
├── cache.py
├── cli.py
├── package.py
├── package_metadata.py
└── pocket/
    ├── __init__.py
    ├── cli.py
    ├── config.py
    ├── http.py
    ├── local.py
    ├── protocol.py
    └── sync.py

tests/
├── fixtures/bunri_pocket_protocol_v1/
├── test_cli.py
├── test_package.py
├── test_package_metadata.py
├── test_pocket_config.py
├── test_pocket_http.py
├── test_pocket_local.py
├── test_pocket_protocol.py
├── test_pocket_sync.py
├── test_web_api.py
└── test_web_jobs.py
```

責務は次で固定する。

- `src/bunri/package_metadata.py`: Bunri package sidecar v1 の型、検証、symlink 非追従 read、target 無効化、成功時 merge、原子的 write
- `src/bunri/pocket/cli.py`: `connect`、`sync` の Typer command、入力と利用者向け表示
- `src/bunri/pocket/config.py`: base URL と token の検証、capabilities、設定 read/write と permission
- `src/bunri/pocket/protocol.py`: protocol v1 validator、JavaScript 互換 stable JSON、manifest/library の生成・merge
- `src/bunri/pocket/local.py`: safe name と package の local preflight、asset hash/size
- `src/bunri/pocket/http.py`: redirect 非追従 HTTP、response 上限、error envelope、安全な exception
- `src/bunri/pocket/sync.py`: remote preflight、media → manifest → library、ETag retry、集計

`src/bunri/package.py` は `package_metadata.py` だけを新たに import し、`bunri.pocket`、HTTP、Pocket CLI を import しない。protocol 層は HTTP/CLI を import せず、clock や serializer を test で差し替えられるようにする。

### 2. CLI dispatcher と command contract

既存の `src/bunri/cli.py:app` は単一 command の Typer app のまま残す。通常の Typer subcommand group へ変換しない。

- `pyproject.toml` の console entry point を `bunri = "bunri.cli:dispatch"` にする。
- `dispatch()` は argv の先頭 token が文字どおり `pocket` の場合だけ、その token を除いて Pocket app を `prog_name="bunri pocket"` で実行する。
- それ以外は現在の `app` を `prog_name="bunri"` で実行する。
- `if __name__ == "__main__"` も `dispatch()` を呼ぶ。
- `src/bunri/web/jobs.py` の `python -m bunri.cli <input> ...` は引き続き既存生成 command へ到達させ、argv を変更しない。
- bare token `pocket` だけを予約する。カレント directory にある実在 file `pocket` は `bunri ./pocket` で既存 CLI の input として扱う。

Pocket CLI は次を提供する。

```text
bunri pocket connect <URL> -o <OUT> [--token-stdin]
bunri pocket sync <SAFE_NAME> -o <OUT> [--no-original]
```

- `-o/--output` の既定は既存 CLI と同じ `out`。
- `connect` は token の argv option を持たない。通常は `Pocket upload token: ` という hidden prompt で読み、`--token-stdin` のときだけ stdin の1行から読む。
- `sync` の `<SAFE_NAME>` は表示 title 検索ではなく、`<OUT>` 直下の directory 名そのものとする。
- `sync` は config がなければ package を送らず、先に `connect` を案内する。
- `--no-original` は今回 original を追加しない指定であり、remote original の削除指定ではない。

dispatcher 変更前の次の出力と exit code を `_STABLE_TERMINAL` と `_plain()` で test に固定し、変更後も同じにする。

- `bunri --help`
- 実在 input の成功経路
- 存在しない input
- 未知 target
- 未知 device
- 未知 option
- 引数なし
- `python -m bunri.cli --help`
- Web runner が組み立てる既存 argv
- `bunri ./pocket` が実在 input `pocket` を処理する経路

### 3. 入力 SHA-1 と cache identity

`src/bunri/cache.py` に immutable な `InputDigest` 相当を追加する。

```text
full_sha1: 40桁の小文字16進数
cache_key: full_sha1[:12]
```

- 新 API は input file を1回だけ block 読みし、両方を返す。
- `build_package()` は新 API を1回だけ呼ぶ。
- 既存 `file_digest(path)` は互換 wrapper として12桁を返す。
- cache directory は従来どおり `<OUT>/.cache/<cache_key>/` とし、directory 名を40桁へ変更しない。
- cache directory には完全 digest identity を原子的に記録・検査する。新規 private metadata の名前は `<OUT>/.cache/<cache_key>/.bunri-input.json` とし、整数 `schema_version: 1`、`algorithm: "sha1"`、`digest`、`cache_key` を持たせる。
- private metadata がない既存 cache は現在 input の identity を安全に追加して既存 stage cache を利用可能にする。metadata があり完全 digest が異なる場合は normalize/separate artifact を読まず、書き換えず停止する。
- cache directory または metadata の symlink を追従しない。write は既存 `replace_into()` 相当で行う。

### 4. package sidecar v1

生成物 `<OUT>/<SAFE_NAME>/.bunri-package.json` は次の exact shape を持つ。local internal schema であり、protocol document の `schema_version: "1.0"` とは別物である。

```json
{
  "schema_version": 1,
  "title": "Song title",
  "safe_name": "Song title",
  "source": {
    "algorithm": "sha1",
    "digest": "0123456789abcdef0123456789abcdef01234567",
    "cache_key": "0123456789ab"
  },
  "targets": [
    {
      "target": "guitar",
      "formats": ["mp3", "wav"]
    }
  ]
}
```

検証規則:

- `schema_version` は整数 `1` だけを受理する。bool は整数として受理しない。
- `title` は空でない文字列、`safe_name` は package directory 名と完全一致する文字列。
- `source.algorithm == "sha1"`、digest は40桁、cache key は12桁の小文字16進数で、`cache_key == digest[:12]`。
- target は `REGISTRY` に存在し `original` ではない。重複を拒否し、保存時は target 名順。
- `formats` は `mp3`、`wav` の部分集合で空にしない。重複を拒否し、保存時は mp3 → wav 順。
- 通常実行で target/backing の MP3 と WAV、original MP3、player まで成功した target は `["mp3","wav"]`。
- `--no-mp3` で target/backing WAV と player まで成功した target は `["wav"]`。
- disk に以前の MP3 が残っていても、今回の `formats` に `mp3` を戻さない。
- malformed、unsupported version、identity 不一致は自動修復しない。artifact 変更前に利用者へ報告して停止する。

更新順序を次で固定する。

1. full digest、title、safe name を決定し、`<OUT>`、cache、package directory の symlink/containment を検査する。
2. 既存 sidecar があれば sidecar 自体を symlink 非追従で読み、schema、safe name、full identity を検証する。full digest が異なれば何も上書きせず停止する。
3. 現在 target の entry を除き、他 target を維持した sidecar を同一 directory の一時 fileから `os.replace()` で原子的に保存する。sidecar がなければ targets が空の v1 sidecar を保存する。
4. normalize、separate、target/backing WAV、必要な MP3 と original、player を生成する。
5. すべて成功した後に sidecar を再読込して同一 identity と他 target を再検証し、現在 target を今回成功した `formats` で追加して原子的に保存する。
6. 途中失敗または最終 sidecar write 失敗では、現在 target を成功済みとして残さない。他 target の entry は維持する。

同一 `<OUT>` への複数 CLI process の同時実行は非対応とし、file lock は追加しない。

`src/bunri/web/app.py` の `/packages` private-path middleware により、`/packages/<SAFE_NAME>/.bunri-package.json` が404になることも test する。

### 5. local preflight

`sync` は config を安全に読んだ後、最初の HTTP request より前に選択 package の全検査と全 upload asset の SHA-256/size 計算を完了する。

SAFE_NAME と directory:

- 空、絶対 path、`.`、`..`、`/`、`\\` を含む値を拒否する。
- dot 始まり、`.pocket`、`.cache`、`web` を内部名として拒否する。
- `<OUT>/<SAFE_NAME>` は `<OUT>` 直下の symlink でない実 directory とする。
- package が見つからないときは、`<OUT>` 直下の dot/internal directory と symlink を除いた候補 directory 名を一覧してよい。候補数と並びは安全で決定的な範囲で実装者が決めてよい。

sidecar と asset:

- sidecar は package directory 直下の symlink でない通常 file とし、上記 schema と directory identity を満たす。
- sidecar の各 target は `formats` に `mp3` を含むことを必須にする。
- 各 target の `<SAFE_NAME>.<target>.mp3` と `<SAFE_NAME>.<target>.backing.mp3` は package directory 直下の symlink でない通常 fileで、size が正であることを必須にする。
- `--no-original` がない場合は `<SAFE_NAME>.original.mp3` も同じ条件で必須にする。
- `--no-original` の場合、local original が存在しても upload 対象へ含めない。
- WAV と player は upload 対象でも preflight 必須 file でもない。
- `formats` に `mp3` がない target は、古い MP3 が残っていても「同期不可（MP3 が無い）」とする。
- 未知 target、MP3 形式なし、target/backing/original の欠損・空・symlink、safe name/source 不整合を1 package 内で全件収集する。
- 1件でも違反があれば HTTP client を呼ばない。

asset descriptor は remote basename、local path、`audio/mpeg`、正の bytes、64桁小文字 SHA-256 を持つ。順序は original（有効時）→ target 名順の target → backing とする。

### 6. local error と成功 message

Rich の装飾差を除いた本文を test で固定する。placeholder は実際の `-o`、safe name、title、target、件数へ置換し、path を command に埋める場合は `shlex.join()` 相当で quote する。ただし、sidecar がない旧 package では title の情報源がないため、実 directory 名の SAFE_NAME を `--title` に使って `shlex.join()` 相当で quote し、`--target` は推測せずリテラルの `<target>` を残す。

config がない場合:

```text
error: Pocket の接続設定がありません。アップロードは開始していません。
先に接続してください:
  bunri pocket connect <Pocket URL> -o <OUT>
```

sidecar がない旧 package:

```text
error: Pocket 同期情報のない旧パッケージが見つかりました。アップロードは開始していません。
- <OUT>/<SAFE_NAME>/.bunri-package.json: 見つかりません

元の入力音源からパッケージを再生成してください:
  bunri /path/to/original.mp3 -o <OUT> --title '<SAFE_NAME>' --target <target>

<OUT>/.cache/ に同じ入力と target の分離キャッシュが残っていれば、分離処理は再実行されません。
サイドカーを手作業で作成したり、Web の Job JSON から値を移さないでください。
```

MP3 がない target:

```text
error: MP3 のない target があるため Pocket と同期できません。アップロードは開始していません。
- <TARGET>: .bunri-package.json の formats に mp3 がありません
- <TARGET>: <MISSING_OR_INVALID_MP3>

元の入力音源から MP3 を有効にして対象 target を再生成してください:
  bunri /path/to/original.mp3 -o <OUT> --title '<TITLE>' --target <TARGET> --mp3
```

一般の local preflight error は次の header の後に全違反を `- ` で列挙する。

```text
error: パッケージを安全に同期できません。アップロードは開始していません。
```

remote preflight で full digest 不一致:

```text
error: 同じ12桁の song ID に別の入力音源が登録されています。アップロードは開始していません。
song_id: <SONG_ID>
local digest: <LOCAL_FULL_SHA1>
remote digest: <REMOTE_FULL_SHA1>
```

manifest PUT の412後に異なる digest が出現した場合:

```text
error: 同期中に同じ12桁の song ID へ別の入力音源が登録されました。
manifest と library は更新していません。preflight 後に media を上書きした可能性があります。棚の状態を確認してから再実行してください。
```

connect 成功:

```text
Pocket に接続しました: <BASE_URL>
設定: <OUT>/.pocket/config.json
注意: upload token はこのファイルに平文で保存されています。設定は -o ごとに分かれます。
接続情報を削除するには <OUT>/.pocket を削除してください。
```

sync 成功:

```text
Pocket 同期が完了しました: <BASE_URL>
media: uploaded=<N> skipped=<N>
manifest: updated=<0|1> skipped=<0|1>
library: updated=<0|1> skipped=<0|1>
```

token、Authorization、config 全体を message、traceback、exception `repr` に含めない。

### 7. protocol v1 validator

`github.com/shimabox/bunri-pocket` の指定 commit にある `schemas/manifest-v1.schema.json`、`schemas/library-v1.schema.json`、`src/protocol/validate.ts`、`docs/protocol-v1.md` を正本として手書き validator を実装する。runtime JSON Schema dependency は追加しない。

最低限、次を検証する。

- schema version は `MAJOR.MINOR` 文字列で、v1 client は全 `1.x` を受理する。unsupported major と malformed/invalid document を分ける。
- song ID、SHA-1、SHA-256、target、reserved `original`、RFC3339 UTC `Z`、正の integer bytes。
- `source.cache_key == song_id == source.digest[:12]`。
- manifest route song ID と文書 identity。
- asset path は basename だけで、`original.mp3`、`<target>.mp3`、`<target>.backing.mp3` と role に応じて正確に一致する。
- instrument target、stem role、library song ID、library instrument target の重複を拒否する。
- instrument は target/backing stem を正確に1件ずつ持つ。
- library manifest path は `tracks/<song-id>/manifest.json` と一致する。
- top-level、source、instrument、stem/asset、library song/instrument の未知 field は受理する。

remote document は全体を validate してから merge し、invalid document の既知部分だけを救済して書き戻さない。

### 8. stable JSON の exact byte contract

`src/bunri/pocket/protocol.py` の serializer と remote JSON parser は、上流 `src/protocol/stable-json.ts` が parse 済み JavaScript value に対して出す byte を再現する。上流の `stableJson()` は `JSON.stringify(...)` の結果に自ら `"\n"` を1つ付加するため、その出力には末尾 LF が含まれる。Python の serializer も直列化結果に LF を1つ付加して出力する。

object key:

- ECMAScript array-index property key は、0〜4294967294 の正準10進表記だけとする。`"0"`、`"12"`、`"4294967294"` は該当し、数値昇順で先頭へ並ぶ。
- `"01"`、`"1.0"`、`"-1"`、`"4294967295"` は array-index ではない。
- array-index 以外は Unicode code point 順。UTF-16 code unit 順ではない。
- 全 nested object に再帰適用し、array の順序は保持する。

number:

- JSON number の入力字句は保持せず、JSON.parse 後の IEEE-754 binary64 の値と ECMAScript Number::toString/`JSON.stringify` の表現へ合わせる。
- `1.0` → `1`、`-0`/`-0.0` → `0`。
- 絶対値10^21以上は `1e+21` のような指数表記、10^-6未満は `1e-7` のような指数表記、それ以外は小数表記を含む ECMAScript の最短往復表現とする。
- exponent の `+`、先頭ゼロ、mantissa の不要な `.0` も JavaScript と一致させる。
- NaN、Infinity、-Infinity は `null` とする。
- client 自身が生成する protocol number は正の integer だけだが、remote の未知 field の number にも同じ parse/serialize を適用する。

string/byte:

- UTF-8、BOM なし。
- 非 ASCII と補助平面文字を `\\uXXXX` 化しない。
- quote、backslash、control character は `JSON.stringify` と同じ JSON escape を使う。
- separator 前後の空白なし。
- 末尾 LF はちょうど1つ。上流 golden との byte 比較はこの LF を含めて行う。

Python の `json.dumps(sort_keys=True)`、`repr(float)`、Decimal の入力字句保持だけで完了扱いにしない。上流 golden との byte 比較を合格条件にする。

整数風 key は上流実装の実 byte を正とし、code point 順だけへ理想化しない。上流 document の文言修正や `bunri-pocket` 側の変更はこの依頼へ含めない。

### 9. manifest 生成・merge

local file 名から remote path を次のように作る。

| remote field | 値 |
|---|---|
| new `schema_version` | `"1.0"` |
| `song_id` | sidecar `source.cache_key` |
| `title` | sidecar `title` |
| `source` | sidecar の algorithm/full digest/cache key |
| original `path` | `original.mp3` |
| instrument `target` | sidecar target |
| instrument `label` | `REGISTRY[target].label_ja` |
| target stem `path` | `<target>.mp3` |
| backing stem `path` | `<target>.backing.mp3` |
| `content_type` | `audio/mpeg` |
| `bytes` | local descriptor の size |
| `sha256` | local descriptor の SHA-256 |
| `updated_at` | 実質的変更時だけ注入 clock の現在 UTC |

merge 規則:

- remote がなければ新規 `1.0` manifest を作る。
- remote があれば既存 `schema_version` を維持し、自動 upgrade/downgrade しない。
- remote `source.digest` が local full digest と異なれば12桁衝突として停止する。
- top-level と source の未知 field を維持する。
- instrument は `target` で merge し、remote にだけある target を削除しない。local target の既知 fieldだけを更新し、instrument 未知 field を維持する。
- stem は `role` で mergeし、既知 asset field と role だけを更新し、stem/asset の未知 field を維持する。
- instruments は target 名順、stems は target → backing 順。
- `--no-original` で新規 manifest を作る場合は `original: null`。既存 manifest の original はそのまま維持する。
- original を upload する場合は既知 asset field を local descriptor で更新し、remote original の未知 field を維持する。
- current `updated_at` のまま candidate を stable serialize し、existing stable byte と同じなら no-op とする。差がある場合だけ現在 UTC を設定して再直列化する。

上流 valid fixture の label `"Guitar"` は契約受理 test の入力であり、内容を書き換えない。Bunri generator の golden は別 file にし、`REGISTRY["guitar"].label_ja` の `"ギター"` を期待する。

### 10. library 生成・merge

- remote がなければ `schema_version: "1.0"`、現在 UTC、songs 配列で作る。
- remote があれば既存 `schema_version` と top-level unknown field、他 song を維持する。
- 対象 song は `song_id` で merge する。
- 対象 song の `title`、`manifest: "tracks/<song-id>/manifest.json"`、`has_original`、`updated_at` は確定 manifest から作る。
- `has_original` は確定 manifest の `original != null` と一致させる。`--no-original` だけを理由に既存 true を false にしない。
- instrument は確定 manifest から target/label を作り、target 単位の未知 field と remote にだけある instrument を維持する。
- songs は song ID 順、instruments は target 名順。
- 対象 song の `updated_at` は manifest の値。library top-level `updated_at` は library 自体が実質的に変わる場合だけ現在 UTCへ更新する。
- current top-level `updated_at` を維持した candidate の stable byte が existing と同じなら no-op とし、PUT しない。

### 11. fixture snapshot と golden の作り方

指定 upstream commit から次を snapshot する。

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

`media/` の元 directory は `fixtures/protocol-v1/` 配下であり、`sample.mp3` の元 path は `fixtures/protocol-v1/media/sample.mp3` とする。snapshot 先は次の構成にする。

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

取得手順:

1. `github.com/shimabox/bunri-pocket` を任意の作業 directory へ取得し、`a8efc3e20a5009c72c1d5bcdd07db6e046c84ceb` を detached checkout する。既存 clone の場所や Bunri と同じ親 directory であることを仮定しない。
2. schema と fixture は、上記の repository-relative path から byte を変えずコピーする。`media/sample.mp3` は `fixtures/protocol-v1/media/sample.mp3` からコピーする。
3. upstream clone で `npm ci` を実行し、project-local の `tsx` を使える状態にする。
4. repository の外に一時 TypeScript script を作り、upstream `src/protocol/stable-json.ts` の `stableJson()` を import し、`process.stdout.write(stableJson(value))` で stdout に1文書だけ出す。`stableJson()` 自身が末尾 LF を付けるため、追加加工はしない。一時 script は生成後に削除し、Bunri と upstream のどちらにも残さない。
5. upstream valid manifest/library を `JSON.parse()` した value に `stableJson()` を適用し、末尾 LF を含む stdout をそれぞれ `manifest-v1.stable.json`、`library-v1.stable.json` に保存する。
6. 同じ script から次の JavaScript value を構築して個別 golden を作る。
   - number: `1.0`、`-0`、10^21、10^-7、通常小数、shortest-round-trip の境界
   - non-finite: NaN、Infinity、-Infinity
   - key: `"0"`、`"12"`、`"4294967294"`、`"4294967295"`、`"01"`、`"1.0"`、`"-1"`
   - Unicode: 日本語、BMP、補助平面文字を含む key/value
   - nested object/array、control character、array order
7. Bunri generator の固定 clock と local asset descriptor から `generated/` の2文書を作る。上流 fixture の `"Guitar"` は変更せず、こちらだけ `"ギター"` とする。

`media/sample.mp3` は24 byte、SHA-256 `01316c8ec960ebe91747508e865d42eef794073d8d6c17eeb87d6f495bcb760b` であり、upstream manifest fixture の `bytes`/`sha256` と一致することを test で確認する。`generated/` の2文書も stable JSON の canonical byte として保存する。

一時 script の要点は次の形とする。import path と入力 path は argument で受け、個人環境の絶対 path を fixture や `UPSTREAM.md` に書かない。

```ts
const { stableJson } = await import(pathToFileURL(resolve(process.argv[2])).href);
const value = /* JSON.parseしたfixture、または上記vector */;
process.stdout.write(stableJson(value));
```

この呼び出しだけで golden は末尾 LF を含む。Python serializer との byte 比較も末尾 LF を含む file 全体に対して行う。

`UPSTREAM.md` に次を記録する。

- upstream repository identifier
- full commit `a8efc3e20a5009c72c1d5bcdd07db6e046c84ceb`
- snapshot 元の repository-relative path
- detached commit の取得、`npm ci`、temporary script + `npx tsx` による stable golden 生成手順
- temporary script を残さないこと
- upstream `"Guitar"` fixture は受理 test、`generated/` は Bunri の `"ギター"` 生成 test であること

test runtime は upstream clone や Node を参照せず、Bunri 内の snapshot だけで完結させる。

### 12. connect と config

base URL validation:

- `https` を原則とする。
- `http` は hostname が `localhost`、または `ipaddress` で loopback と判定される IPv4/IPv6 の場合だけ許可する。
- userinfo、query、fragment を拒否する。
- host がない URL を拒否する。
- trailing slash だけを除去し、既存 path prefix は維持する。例: `https://example.invalid/pocket/` の API は `https://example.invalid/pocket/api/v1/...`。
- path を勝手に decode/re-encodeして別 URL にしない。

token validation:

- prompt または stdin の1行を trim する。
- padding なし base64url alphabet だけを許可する。
- decode でき、32 byte 以上であることを必須にする。
- argv、environment variable、query parameter から受ける経路は作らない。
- secret を持つ value object は `repr=False` 相当とし、例外へ token や request header を入れない。

保存前に次を Bearer token 付きで実行する。

```text
GET <BASE_URL>/api/v1/upload/capabilities
metadata timeout: 30秒
```

success body は次と一致する capability を要求する。未知 field は許容するが、既知 field の欠落・型違い・値違いは保存前に停止する。

```json
{
  "api": { "major": 1 },
  "schemas": {
    "manifest": { "major": 1, "latest": "1.0" },
    "library": { "major": 1, "latest": "1.0" }
  },
  "limits": {
    "media_bytes": 94371840,
    "json_bytes": 1048576
  },
  "media": {
    "content_types": ["audio/mpeg"],
    "hash": "SHA-256",
    "conditional_json_put": true
  }
}
```

config の形:

```json
{
  "schema_version": 1,
  "base_url": "https://example.invalid",
  "token": "base64url-without-padding"
}
```

保存規則:

1. `<OUT>` を利用者指定 output root として扱い、`<OUT>/.pocket` が symlink でない実 directory であることを検査して作る。
2. POSIX では directory を0700にする。
3. 同じ directory で `mkstemp()` し、file descriptor を0600にする。
4. JSON を UTF-8、BOM なし、末尾 LF 1つで書き、flush、必要な fsync 後に `os.replace()` する。
5. 保存後に directory/file の mode を再検査する。
6. chmod を保証できない filesystem では平文 token を保護できない旨を stderr へ警告し、保存自体は継続する。
7. capabilities 失敗時は既存 config を変更しない。

config read 時も `.pocket` directory と `config.json` の symlink を追従せず、schema、URL、token を再検証する。

### 13. HTTP client

Python 3.13 標準ライブラリのみを使う。

- `urllib.request` の専用 opener で `HTTPRedirectHandler.redirect_request()` を無効化し、全 redirect status を error として返す。
- endpoint は base URL の path prefix を維持して組み立てる。
- metadata request の timeout は `METADATA_TIMEOUT_SECONDS = 30`、media PUT は `MEDIA_TIMEOUT_SECONDS = 300` 相当の定数にし、test で注入可能にする。
- response body は上限 + 1 byte だけ読み、JSON/capabilities/manifest/library が1,048,576 byteを超えたら停止する。error body も無制限に読まない。
- error envelope は `error.code`、安全な message、`supported_schema_major` だけを parse し、request/response header 全体を保持しない。
- JSON GET は strong ETag が必要。既存文書に ETag がない、weak、malformed なら更新せず停止する。404は absent として扱う。
- JSON PUT は stable byte を memory で作り、送信前に1,048,576 byte以下を確認する。`Content-Type: application/json` と正確な `Content-Length` を付ける。
- media PUT は open file object を request body に渡し、`Content-Length`、`Content-Type: audio/mpeg`、`X-Bunri-Content-SHA256` を明示する。全 file を `read()` して bytes 化しない。chunked transfer を使わない。
- media HEAD success は `Content-Length` と `X-Bunri-Content-SHA256` を検証する。media PUT success は応答の `X-Bunri-Content-SHA256` を検証し、空 body のため0となる応答の `Content-Length` は検証に使わない。
- Authorization token を stdout、stderr、exception、`repr`、captured diagnostic に出さない。

利用者向け error mapping:

- 401 `UNAUTHENTICATED`: token が無効。`connect` の再実行を案内する。
- 409 `UNSUPPORTED_SCHEMA_MAJOR`: client/server の互換性がない。更新を案内して停止する。
- 412 `PRECONDITION_FAILED`: sync 層へ渡して document 種別ごとの retry/衝突処理を行う。一律 retry しない。
- 413 `PAYLOAD_TOO_LARGE`: どの local document/asset が上限を超えたかを secret なしで示す。
- 422 `INVALID_DOCUMENT` / `CONTENT_MISMATCH`: document 契約違反または media metadata 不一致として停止する。
- 428 `PRECONDITION_REQUIRED`: client/server 契約差として更新案内付きで停止する。
- 429: `Retry-After` があれば表示し、自動 sleep/retry はしない。
- 503: auth/storage unavailable として再実行を案内するが、無制限 retry はしない。

loopback `ThreadingHTTPServer` で、redirect を1回も追従しないこと、file object を複数 block で読み正確な `Content-Length` かつ非 chunked で PUT することを wire-level で確認する。`urllib` で成立しない場合は標準ライブラリ内の `http.client` 直接利用まで検討してよい。外部 HTTP dependency が必要なら追加せず、選択肢と `uv.lock`/配布への影響を報告して停止する。

### 14. remote preflight と同期順序

local preflight と全 hash/size 算出後、最初の write より前に次を GET する。

```text
GET /api/v1/upload/manifest/<song-id>
GET /api/v1/upload/library
```

- 200は body 全体、validator、strong ETag を検証する。
- 404は absent とする。
- manifest が存在する場合、route identity と `source.digest` を検査する。local full digest と異なれば media PUT 前に12桁衝突として停止する。
- library が存在する場合、他 song を含め文書全体を検証する。
- library に対象 song があるのに manifest が404の場合は、参照が先行した不整合状態として write 前に停止する。manifest があり library に対象 song がない状態は、前回 library 更新失敗からの回復経路として許可する。

その後、次の順で write する。

#### media

1. original（有効時）、各 target を target 名順で target → backing の順に処理する。
2. `HEAD /api/v1/upload/media/<song-id>/<asset>` を実行する。
3. 200で `X-Bunri-Content-SHA256` と `Content-Length` が両方 local descriptor と一致すれば skip。
4. 404、metadata 欠落、不一致なら PUT。ETag だけで skip しない。
5. PUT は `Content-Length` と `X-Bunri-Content-SHA256` を明示して送信し、200/201を成功とする。応答の `X-Bunri-Content-SHA256` が期待値と一致しなければ停止する。空 body のため0となる応答の `Content-Length` は検証に使わない。

#### manifest

1. media がすべて成功/skip した後、manifest を再 GET する。
2. 再取得文書を validate し、digest を再確認して merge する。
3. no-op なら PUT せず確定 manifest とする。
4. 新規なら `If-None-Match: *`、既存なら GET の strong ETag を `If-Match` に付ける。
5. PUT 成功は200/201。応答 ETag を確認する。
6. 412なら最新を再 GET する。`source.digest` が local と同じ場合だけ再マージし、初回 PUT に加えて最大3回再試行する。
7. 412後の最新 digest が異なれば12桁衝突として即時停止し、manifest/library は更新せず、preflight 後に media を上書きした可能性を固定 message で報告する。
8. 最大3回の再試行を使い切ったら停止する。

#### library

1. manifest が成功または no-op と確定した後、library を再 GET する。
2. validate、確定 manifest から再マージし、no-op なら PUT しない。
3. 新規は `If-None-Match: *`、既存は strong ETag の `If-Match`。
4. 412は最新を再 GET・再マージして、初回 PUT に加えて最大3回再試行する。
5. retry 上限超過は停止する。library には full digest がないため、manifest の衝突分岐を流用しない。

同じ棚に対する複数 process の同時 sync は非対応とする。双方が manifest 不在を preflight した後に異なる曲の media を同じ12桁 keyへ書ける TOCTOU は残存リスクであり、lock service、rollback、media delete は追加しない。

失敗後の収束:

- media 途中失敗: manifest/library は未更新。再実行で一致 media を skip して続行。
- manifest 失敗: library は未更新。再実行で media を skip し manifest から続行。
- library 失敗: media/manifest は残る。再実行で両方を skip/no-op にして library へ収束。
- no-op 再実行: document PUT を行わず `updated_at` と ETag を変えない。

### 15. README

`README.md` の英語冒頭と日本語本文の「送信しない」説明を次の意味に更新する。

- 不変: 分離処理のために入力音源を外部へ送信することはない。Web UI も localhost 内で動く。
- オプトイン: `bunri pocket` を明示的に実行した場合だけ、分離後の MP3 を利用者所有の Pocket storage へ送る。

`bunri pocket` 節を追加し、少なくとも次を載せる。

```bash
bunri pocket connect https://your-pocket.example -o out
bunri pocket sync '曲名' -o out
bunri pocket sync '曲名' -o out --no-original
```

- `sync` の曲名は `out/` 直下の directory 名（safe name）であること。
- `--no-original` は original MP3 の新規送信を省略し、既存 remote original を消さないこと。
- `out/.pocket/config.json` に upload token が平文保存されること。
- 接続情報は `out/.pocket` を削除すれば消せること。
- bare file 名 `pocket` を既存分離 input にする場合は `bunri ./pocket` と指定すること。

README の既存方針に合わせ、通常段落内に手動改行を入れない。

### 16. 公開工程は分離する

この変更では次を更新しない。

- `pyproject.toml` の `version`（entry point だけは変更対象）
- `src/bunri/__init__.py`
- `tests/test_smoke.py`
- `uv.lock`
- release commit、tag

version bump、上記 version assertion、lock 更新、tag は独立したリリース工程で行う。`uv lock --check` で現在 lock が有効なままであることは検証する。

## タスク（この順で）

1. `tests/test_cli.py` と必要な subprocess test で、既存 CLI の help、成功、validation error、module invocation、Web runner argv の baseline 出力/exit code を固定する。`./pocket` の実在 input case も追加する。
2. 契約の正本 commit を取得し、schema、valid/invalid fixture、sample media を `tests/fixtures/bunri_pocket_protocol_v1/` へ snapshot する。一時 `npx tsx` script で stable golden を作り、`UPSTREAM.md` を記録する。
3. `src/bunri/pocket/protocol.py` と `tests/test_pocket_protocol.py` に validator、remote JSON parse、JS 互換 stable JSON、manifest/library generator/merge を実装する。
4. `src/bunri/cache.py` に1回読みの full SHA-1/cache key API と cache identity metadata を追加し、互換 wrapper と既存 cache path を維持する。
5. `src/bunri/package_metadata.py` と test を追加し、sidecar v1、symlink 非追従 read、原子的 write、target の二段階 invalidation/merge を実装する。
6. `src/bunri/package.py` に二段階 sidecar 更新を接続し、通常/`--no-mp3`/別 target/各失敗段階/full digest 衝突を `tests/test_package.py` で固定する。
7. Web runner が同じ `build_package()` へ到達することと、`/packages/<safe>/.bunri-package.json` が404になることを `tests/test_web_jobs.py`、`tests/test_web_api.py` で固定する。
8. `src/bunri/pocket/local.py` と `tests/test_pocket_local.py` に SAFE_NAME、directory、sidecar、formats、MP3、symlink、全違反集約、hash/size preflight を実装する。
9. `src/bunri/pocket/http.py` と `tests/test_pocket_http.py` に redirect 非追従、response 上限、error mapping、secret redaction、30/300秒 timeout、file-object media PUT を実装する。
10. `src/bunri/pocket/config.py` と `tests/test_pocket_config.py` に URL/token、hidden/stdin input、capabilities、0700/0600、atomic replace、permission warning、既存 config 保持を実装する。
11. `src/bunri/pocket/sync.py` と `tests/test_pocket_sync.py` に remote preflight、media/manifest/library、document 別412、no-op、`--no-original`、再実行収束、件数集計を実装する。
12. `src/bunri/pocket/cli.py` を追加し、`src/bunri/cli.py` の dispatcher と `pyproject.toml` の entry point を接続する。固定 message と token 非表示を test する。
13. loopback `ThreadingHTTPServer` による wire-level test で streaming、Content-Length、redirect、HEAD、ETag、412、部分失敗を検証する。
14. `README.md` の英語冒頭、日本語本文、`bunri pocket` 節を確定仕様どおり更新する。
15. 対象 test、全 test、lock/build/whitespace 検証を実行する。
16. 変更 file だけを path 指定で stage し、日本語の commit message で commit する。`docs/plans/` は stage/commit しない。push しない。

## 必須テストケース

### CLI/dispatcher

- 既存 `--help`、実在 input、missing input、unknown target/device/option、no args の出力と exit code が不変。
- console entry point と `python -m bunri.cli` の双方。
- 先頭が exact `pocket` の場合だけ Pocket app。
- `pocket-song.mp3` 等の prefix は既存 CLI。
- 実在 file `pocket` を `./pocket` で既存 CLI へ渡せる。
- Web runner argv が従来どおり。

### digest/sidecar/package

- input を1回だけ読み、40桁と12桁が一致する。
- `file_digest()` は従来どおり12桁。
- cache directory 名は12桁のまま。
- cache identity が同じなら既存 cache を使い、異なる full digest なら artifact read/write 前に停止。
- sidecar `schema_version` の bool、文字列、未知 versionを拒否。
- 通常実行は `["mp3","wav"]`、`--no-mp3` は `["wav"]`。
- `--no-mp3` 前の古い MP3 が残っても formats は `["wav"]`。
- 別 target 追加で既存 target を維持し、target 名順になる。
- 現在 target は生成前に消え、normalize/separate/export/mp3/original/player の各失敗で復活しない。
- 最終 sidecar write 失敗でも現在 target を成功扱いしない。
- sidecar/current artifact の symlink を追従しない。
- same safe name の異なる full digest は artifact を上書きしない。
- Web job 生成物にも sidecar ができ、static route から sidecar は404。

### protocol/stable JSON

- upstream valid fixture 全件を受理し invalid fixture 全件を指定理由で拒否。
- `1.x` を受理し、unsupported major と malformed version を区別。
- top-level/source/instrument/stem/asset/library song/instrument の未知 field を保持。
- upstream manifest/library stable golden と byte 一致。
- array-index 境界 key が数値順で先頭、他 key が code point 順。
- BMP/補助平面/日本語、nested object、array order、control escape。
- `1.0`、`-0`、指数境界、最短往復、NaN/±Infinity。
- BOMなし、空白なし、末尾 LF 1つ。
- Bunri generator golden は label `"ギター"`、upstream fixture は `"Guitar"` のまま。
- no-op merge で timestamp を変えず、既知変更時だけ clock を使う。
- remote target/song/unknown field を削除しない。
- `--no-original` で新規 null、既存 original 維持。

### local/config

- SAFE_NAME の absolute、empty、dot、separator、dot/internal name、directory symlinkを拒否。
- missing package の安全な候補表示。
- missing sidecar の固定 message。
- `formats` に mp3 がない全 target、欠損/空/symlink MP3、unknown target、identity mismatch を集約し HTTP 0件。
- `--no-original` なしの original 必須、ありの original 非対象。
- config なしの固定 message と HTTP 0件。
- HTTPS、localhost/IPv4/IPv6 loopback HTTP、path prefix、trailing slash。
- userinfo/query/fragment、非 loopback HTTP、不正 host の拒否。
- hidden prompt と `--token-stdin`、base64url/decoded length。
- capability の全既知 field と型、mismatch 時に既存 config 非変更。
- POSIX 0700/0600、directory/config symlink 拒否、atomic replace。
- mode 保証不能時の明示 warning。
- token が output/error/exception/repr にない。

### HTTP/sync

- 301/302/303/307/308を追従しない。
- metadata 30秒、media 300秒を注入して timeout test。
- response body 上限。
- media body は file object を block 読みし、非 chunked、正確な Content-Length。
- HEAD hash+size 一致で skip。片方欠落/不一致で PUT。
- media PUT は `Content-Length` と hash header を明示し、response の hash 一致を必須にする。空 body の response `Content-Length: 0` は検証に使わない。
- remote preflight digest 不一致で media PUT 0件。
- 初回 sync の key/order/header/body、manifest/library 作成。
- 同じ sync の再実行は全 skip/no-opで ETag/timestamp 不変。
- 別 target 追加はその2 media と document 差分だけ。
- media、manifest、library の各段階で1回失敗後、次回 sync で収束。
- manifest/library の `If-None-Match: *` と strong `If-Match`。
- library 412 は GET/merge/retry、初回 + 最大3回、上限超過停止。
- manifest 412後、same digest は GET/merge/retry、different digest は即時停止と上書き可能性 message。
- preflight 後に別 digest manifest が出現する競合 case。
- 429 は Retry-After を表示して自動待機しない。
- 401/409/413/422/428/503 の安全な案内。
- success 集計 message と base URL。token/Authorization 非表示。

## テスト・検証

対象 test:

```bash
uv run pytest -q -n auto tests/test_cli.py tests/test_package.py tests/test_package_metadata.py tests/test_pocket_config.py tests/test_pocket_http.py tests/test_pocket_local.py tests/test_pocket_protocol.py tests/test_pocket_sync.py tests/test_web_api.py tests/test_web_jobs.py
```

全 test:

```bash
uv run pytest -q -n auto
```

lock/build/whitespace:

```bash
uv lock --check
uv build
git diff --check
```

CI 相当の依存準備が必要な場合:

```bash
uv sync --frozen --extra web
uv run playwright install --with-deps chromium
```

専用 lint/format command は定義されていない。未定義の formatter/linter や新規 dependency を導入しない。

## 完了条件

- [ ] 既存 CLI の出力・exit code・Web runner argv が不変で、`bunri pocket connect/sync` と `./pocket` が仕様どおり動く。
- [ ] full SHA-1 と12桁 key を1回の読み取りで得て、cache path を維持し、完全 digest 衝突を拒否する。
- [ ] sidecar v1 が整数 version、source identity、target、今回成功した formats を持つ。
- [ ] target を生成前に無効化し、途中失敗時に古い target entry を残さず、他 target は維持する。
- [ ] `--no-mp3` target は `["wav"]` となり、古い MP3 があっても sync preflight で停止する。
- [ ] local 全違反を集約し、違反時 HTTP request が0件である。
- [ ] upstream fixture/golden と `UPSTREAM.md` が指定 commit から再現可能である。
- [ ] stable JSON が上流 TypeScript と byte 一致し、array-index key、Unicode、Number、control、LF の全境界を通る。
- [ ] manifest/library が protocol `1.x` と全 unknown field を維持し、label は Bunri generator で `label_ja` になる。
- [ ] config が capabilities 成功後だけ原子的に保存され、POSIX で0700/0600、token は全 diagnostic から redaction される。
- [ ] redirect 非追従、metadata/media timeout、response 上限、streaming Content-Length が wire test で確認される。
- [ ] media は HEAD の hash+size で skip し、PUT は size と hash を明示して送信し、応答 hash の一致を検証する。空 body の応答 size は検証に使わない。manifest/library は stable no-op で PUTを省略する。
- [ ] remote digest 衝突は preflight で media 前に停止し、manifest 412後の異 digest は上書き可能性を報告して停止する。
- [ ] library と same-digest manifest の412は初回 + 最大3回の再取得・再マージで収束し、上限超過は停止する。
- [ ] `--no-original` が既存 original を削除しない。
- [ ] 各段階の失敗後に再実行で収束する。
- [ ] README の英語・日本語に分離処理の不送信、Pocket のオプトイン送信、最小手順、平文 token、削除方法、`./pocket` が記載される。
- [ ] `uv run pytest -q -n auto` が通る。
- [ ] `uv lock --check`、`uv build`、`git diff --check` が通る。
- [ ] version、`src/bunri/__init__.py`、`tests/test_smoke.py`、`uv.lock` が変更されていない。
- [ ] 作業ブランチに commit 済みである。
- [ ] `docs/plans/` が commit に含まれていない。

## commit ルール

- commit message は日本語で、何を変更し、利用者にどのような効果があるかを分かりやすく表す。
- `type(scope):` 形式の接頭辞は使用してよい。
- 作成経緯、依頼元、特定の利用事情、計画書への参照、私的な link を commit message に書かない。
- code comment と README も機能・入力・挙動・理由を一般化して説明し、作成経緯や特定の利用元を書かない。
- `git add -A` を使わない。実装・test・fixture・README・entry point の変更 file を path で明示して stage する。
- `docs/plans/` を stage/commit しない。
- version、`src/bunri/__init__.py`、`tests/test_smoke.py`、`uv.lock` を stage/commit しない。
- push しない。commit までで止める。

## 未確定事項と判断の委ね方

- 勝手に決めてよい範囲: private class/function 名、責務を崩さない helper 分割、clock/opener の具体的な注入形、package 候補の上限と安全な表示順、既存 style に沿う test fixture helper の分割。
- 固定済みで変更しない範囲: sidecar shape/version/formats、二段階更新、bare `pocket` だけを分岐する dispatcher、protocol v1、stable JSON byte 規則、30/300秒 timeout、config location/permission、local preflight 全件集約、同期順、document 別412、retry 回数、`--no-original`、README の送信方針、公開工程の分離。
- 止まって報告すべき範囲: 外部 HTTP/validator dependency の追加、upstream golden との byte 不一致、指定 commit の契約と本依頼の矛盾、既存 CLI 表示/exit code の変更、同時実行 lock や rollback/delete の追加、Worker/PWA変更、R2 S3 API 利用、`sync --all`、version/lock/tag 更新、秘密 token または本番 access が必要になる場合。
- 判断が必要になっても人間向けの質問 UI を出さない。選択肢、影響、推奨案を実施報告に書いて停止する。

## 禁止事項

- push しない（commit まで）。
- `git add -A` を使わない。
- `docs/plans/` を commit に含めない。
- 本番 upload token を要求・保存・表示しない。
- token を argv、URL、environment variable から受ける機能を追加しない。
- redirect を追従しない。
- sidecar を手作業で生成する migration、Job JSON/Web API からの推測移行を追加しない。
- remote の曲、target、asset、unknown field を削除しない。
- 同時実行 lock、rollback、remote delete、`sync --all` を追加しない。
- 外部 HTTP client、runtime schema validator、未定義 lint/format tool を追加しない。
- Bunri Pocket Worker/PWA を変更しない。
- version、`src/bunri/__init__.py`、`tests/test_smoke.py`、`uv.lock`、tag を変更しない。
- commit message、code comment、README、fixture provenance に作成経緯、特定の利用元、私的な link、個人環境の local path を書かない。
- スコープ外の refactor を行わない。
- 人間向けの質問 UI を出さない。

## 報告フォーマット

- 作成・変更 file 一覧
- 実行した test・lock/build・lint 相当検証と結果
- commit SHA と commit message
- upstream snapshot/golden の取得 commit と再生成確認結果
- 判断に迷った点・未解決の懸念（なければ「なし」）
