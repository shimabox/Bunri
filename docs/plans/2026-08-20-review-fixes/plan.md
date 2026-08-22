# レビュー指摘修正(セキュリティ・安定性) 実装計画

- 日付: 2026-08-20
- ブランチ: plan/2026-08-20-review-fixes
- 実装担当(駒): Claude sonnet — デフォルト編成。設計は確定済みで各修正は局所的なため

## 背景・目的

直前のセキュリティ・安定性レビューで重大2件・高3件・中3件+改善3件が出た。ローカルWebサーバー経由の非公開データ漏えいとディレクトリ脱出を塞ぎ、ジョブ管理の安定性・テスト環境の欠陥を直す。

## スコープ

### やること

- 指摘1〜8の修正
- 改善3件(device指定の厳密化・モデルDLのcommit固定とSHA-256検証・CI追加)
- 各修正への回帰テスト追加

### やらないこと

- 非公開データ(web/・.cache/)を静的配信ルート外へ移す構造変更(既存 out/ レイアウトとCLI互換を壊すため、ミドルウェア強化で対応)
- Web UIへの target 選択UI追加
- CIでのDockerイメージビルド(代わりにDockerfileを検査する軽量テストで extra 漏れを検出)

## 方針

設計は以下で確定。実装駒はこの設計に従う。実装詳細=変数名・関数分割等のみ裁量。

1. **大文字小文字バイパス修正** — `src/stemlab/web/app.py` の `_block_private_package_paths` ミドルウェア: セグメント比較を `casefold()` 化し、さらに先頭が `.` のセグメントを含むパスを一律404にする(`.cache`/`.CACHE`/隠しファイルを一括遮断)。既存の `..` チェックは維持。
2. **`--title ..` によるディレクトリ脱出修正** — `src/stemlab/package.py` の `_safe_filename` と `src/stemlab/web/jobs.py` の `safe_filename`(意図的な重複コード。両方に同一変更): 不正文字置換後に先頭の `.` を全て除去(`..`→空→`untitled` にフォールバック)、結果が casefold で `web` に一致したら `web-package` に変名。加えて `build_package` で `package_dir.resolve()` が `out_dir.resolve()` 配下であることを `Path.is_relative_to()` で検証し、外れたら例外(文字列prefix比較は使わない)。
3. **`#` を含むタイトルでURLが壊れる問題の修正** — サニタイズ対象文字に `#` と `%` を追加(ファイル名slugからのみ除去。表示タイトルは生のまま維持)。加えてURL組み立て側で `urllib.parse.quote()` によるエンコード: `src/stemlab/web/app.py` の `_serialize_job` が返す `package_url`、および player テンプレートへ渡す `original_src`/`target_src`/`backing_src`(`src/stemlab/player.py` の `render_player` 側でエンコードし、空白や既存フォルダの `#` にも効かせる)。
4. **正常shutdownで実行中ジョブが error 化する問題の修正** — `src/stemlab/web/jobs.py` の `JobStore`: ①停止フラグを設定 → ②キューへ sentinel を投入(アイドル中ワーカーの `queue.get()` ブロックを確実に起こす)→ ③実行中サブプロセスへ SIGTERM → ④`_run_job` の終了処理は停止起因なら error にせず `queued` に戻して永続化(player 生成済みで成功していれば done のまま)→ ⑤ワーカーを join(timeout 15秒)。ワーカーループは停止フラグが立っていたら sentinel より前に並んでいる通常ジョブも実行せず終了する。`terminate_pid_from_sidecar` は子プロセスの停止を確認してから sidecar を削除する: SIGTERM → 猶予内ポーリング(猶予秒数は実装者判断、数秒目安)→ 残存なら SIGKILL → 停止確認後に sidecar 削除。SIGKILL 後も残る場合は sidecar を保持し、次回起動時に再回収できるようにする。stale pid(プロセス不在・マーカー不一致)は従来どおり即削除でよい。
5. **Docker dev イメージの web 依存漏れ修正** — `Dockerfile` の dev-deps ステージと dev ステージの `uv sync` 両方に `--extra web` を追加。
6. **壊れたジョブJSONで起動不能になる問題の修正** — `src/stemlab/web/jobs.py` の `_load_and_recover` にスキーマ検証を実装: 必須フィールドの存在と型 / `status` が既知値(queued|running|done|error)/ 日時フィールドが ISO 8601 として parse 可能 / ジョブID とファイル名の整合。不合格ファイルは一意な隔離名(既存の隔離ファイルを上書きしない。例: `<name>.json.bad-<ランダムtoken>`)へリネームして警告を出力し、残りのジョブは読み込み続行。
7. **Webワーカーの `--target` 未伝達修正** — `src/stemlab/web/jobs.py` の `default_runner` が組むCLIコマンドに `--target <target>` を追加。
8. **CSRF対策(厳密同一オリジン検証)** — `src/stemlab/web/app.py`: POST 等の非安全メソッドで Origin ヘッダが存在する場合、Origin の scheme・host・port がリクエスト先(自オリジン)と完全一致する場合のみ許可し、それ以外は403(例: `http://localhost:9999` → `localhost:8330` も拒否)。デフォルトポートの正規化は実装者判断。Origin ヘッダ無し(curl 等の非ブラウザ)は許可。

改善3件:

- **device 指定の厳密化** — `src/stemlab/separate.py`: CLI(`src/stemlab/cli.py`)の許可値・help・README を `auto|cpu|mps|cuda` に更新。指定デバイスは利用可否を torch で確認して不可なら例外(黙ってCPUへ落とさない)。可なら指定デバイスを実際に選択する(mps 指定時に CUDA へ自動選択で流れる等を防ぐ — Separator 構築の瞬間だけ他アクセラレータの可視性を隠す既存の monkeypatch パターンを一般化する)。
- **モデルダウンロードの固定と検証** — `src/stemlab/separate.py`: becruily モデルの URL を commit ピン `https://huggingface.co/becruily/mel-band-roformer-guitar/resolve/6409e7f88754b07ef7ca3bd1b76a15f010f1672a` に変更。SHA-256 検証を既存ファイルと新規ダウンロードの両方に適用: 既存ファイルが不一致 → 削除して例外 / 新規は一時ファイルへダウンロード → 検証 → 検証後に atomic replace。一時ファイル名は同時実行競合を避けるため一意な名前にする(固定 `.part` 名は使わない)。期待値:
  - ckpt(mel_band_roformer_guitar_becruily.ckpt): `83472bbf125774af5282d2e0b86df89eaf2dd45e8a4ec8d68e820ebf3e42a83c`
  - yaml(config_mel_band_roformer_guitar_becruily.yaml): `b681c3f886251b04b666b3f06e87ce65d7ec610e40b5d75915c01782e5444b0e`
- **CI 追加** — `.github/workflows/ci.yml` 新規作成: ubuntu-latest / ffmpeg を apt で明示導入(未導入だとパッケージ生成テストが skip され得る)/ uv セットアップ / `uv sync --frozen --extra web` / `uv run playwright install --with-deps chromium` / `uv run pytest -q`。使用する GitHub Actions はバージョンまたはコミットを固定する。加えて Dockerfile を検査する軽量回帰テスト(dev-deps / dev ステージの `uv sync` 行に `--extra web` が含まれることを assert)を tests/ に追加 — CI 自身の sync では Dockerfile の extra 漏れを検出できないため。

## タスク分解

| # | タスク | 依存 |
|---|---|---|
| 1 | sanitizer 強化(package.py / jobs.py)+ build_package 封じ込め(is_relative_to) | - |
| 2 | web/app.py: casefold+ドット遮断 / URL quote / 厳密同一オリジン検証 | 1 |
| 3 | web/jobs.py: sentinel 込み shutdown・sidecar 削除順序・JSONスキーマ検証+一意隔離・--target | 1 |
| 4 | separate.py: device 厳密化(+CLI/README 更新)・DL の commit ピン+SHA-256 二重検証 | - |
| 5 | Dockerfile --extra web / CI workflow(ffmpeg 込み・Action固定)/ Dockerfile 検査テスト | - |
| 6 | 回帰テスト一式(受け入れ基準を再現手順どおりテスト化) | 1-4 |

## 完了条件・受け入れ基準

- [ ] `uv run pytest -q` 全パス
- [ ] バイパス回帰テスト: 実際に `out/WEB/` と `out/.CACHE/` に秘密ファイルを作成した上で `/packages/WEB/…`・`/packages/.CACHE/…` が404を返す(Linux CI でも欠陥を検出できる形。小文字ディレクトリに大文字URLでアクセスする形式は不可)
- [ ] 脱出回帰テスト: `--title ..` 等が out 外へ書かない(`web`→`web-package` 変名含む)
- [ ] URL回帰テスト3本: ①新規タイトルの slug から `#`/`%` が除去され表示タイトルは維持される ②`render_player()` に空白・`#` を含む既存ファイル名を直接渡すと出力内で `%20`・`%23` にエンコードされる ③`#` 入りタイトルのジョブで API が返す `package_url` がエンコード済み(`%23` 等)であること
- [ ] shutdown回帰テスト: 実行中ジョブが `queued` に戻る+アイドル状態からの shutdown が join timeout せず即時完了する(sentinel の検証)+停止フラグ後は待機中の通常ジョブを開始しないこと+正常完了済みジョブは done のまま維持されること
- [ ] JSON隔離回帰テスト: 型不正・status不正・ID/ファイル名不整合・必須フィールド欠落・日時形式不正の各ケースで起動継続+一意名で隔離される
- [ ] --target回帰テスト: fake runner が `--target vocals` を受け取る
- [ ] CSRF回帰テスト: 同一オリジン Origin → 許可 / ポート違い(localhost:9999 等)・敵対 Origin → 403 / Origin 無し → 許可
- [ ] device回帰テスト: 指定デバイス不可時に例外+指定デバイスが実際に選択される
- [ ] SHA-256回帰テスト: 既存モデルファイルがハッシュ不一致のとき削除して例外になる(小さなダミーファイルで検証)
- [ ] Dockerfile 検査テストが `--extra web` の存在を assert する
- [ ] 作業ブランチに commit 済みであること

## 未確定事項・リスクと判断の委ね方

| 項目 | 内容 | 実装時の扱い |
|---|---|---|
| Origin のデフォルトポート正規化 | `http://localhost` vs `localhost:80` の扱い | 実装者が判断してよい |
| SIGKILL までの猶予秒数 | SIGTERM 後のポーリング猶予(数秒目安) | 実装者が判断してよい |
| CI の uv キャッシュ設定 | torch DL 時間の短縮方法 | 実装者が判断してよい |
