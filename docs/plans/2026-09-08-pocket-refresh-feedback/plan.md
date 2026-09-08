# 「状態を再読込」の進行表示 実装計画

- 日付: 2026-09-08
- リポジトリ: `github.com/shimabox/Bunri`
- ベースブランチ: `main`（参考）
- ベース SHA: `674de86c7e2f5845270bf1a8a1a20ef03083a093`
- 作業ブランチ: `plan/2026-09-08-pocket-refresh-feedback`
- モード: 一気通貫（計画 → 実装 → 検収）
- 起案担当: 予定 `claude-fable-5-1` / effort 未指定。確認済み: `claude-fable-5-1` / effort 未取得（実行ハーネスのモデル表示で確認）
- 計画レビュー担当: 予定 `gpt-6-astra` / high（未実行）
- 編成の例外理由: なし
- 実装担当: `gpt-5.6-sol` / medium（Codex CLI、workspace-write）。単一テンプレートの JS/CSS と Playwright テストで境界が明確な中難度の変更のため
- 独立実装レビュー担当: `gpt-5.6-sol` / high の新規セッション（予定）
- 計画書の扱い: レビュー・privacy 検査後に共有可能な計画書として実装 commit に含める
- 公開範囲: 作業ブランチへの commit まで承認済み。push・PR 作成・マージは別途判断

## 背景・目的

Bunri の Web UI にある「状態を再読込」ボタンは、押すと Pocket（スマートフォン向け棚）に曲ごとの manifest と library を問い合わせ、各曲の同期状態バッジと「出力先の全パッケージ N件中、同期対象 N件」の文言を更新する。曲数によっては 1 回に数十秒かかるが、その間ボタンの見た目は変わらず、各曲のバッジが「確認中」になる以外の変化がない。完了しても「更新した」痕跡が残らないため、利用者には何が起きているのか、終わったのかが分からない。

この変更で、押した直後にボタンが「再読込中…」とスピナーに変わり、件数文言が経過秒つきの「棚の状態を確認しています… (0:12)」になり、完了時には更新時刻、失敗時には理由と時刻が同じ場所に残るようにする。

## スコープ

- 対象: `src/bunri/web/templates/index.html.j2`（CSS と JS）、`tests/test_web_page.py`
- 対象外: サーバー側 API の変更（`src/bunri/web/app.py` など）、Pocket 問い合わせの高速化、全曲アップロードの進行表示の変更、README の更新（ボタンの説明は現状 README にない）、曲カードのバッジ表示の変更

## 方針

1. 確認中（既存の `pocketChecking` が true の間）
   - 「状態を再読込」ボタン（`#sw-pocket-refresh`）を無効化し、中身を 14px のリングスピナー要素（`aria-hidden="true"`）と「再読込中…」の文字に置き換える。ボタンに `aria-busy="true"` を付ける。
   - 既存の `button.sw-btn:disabled { opacity: 0.45 }` はスピナーを薄くしてしまうため、確認中を示すクラスを付けた状態では不透明のまま、文字色を灰茶（`#8a7f74`）、カーソルを `progress` にする。
   - 件数文言（`#sw-pocket-count`）を「棚の状態を確認しています… (M:SS)」にする。M:SS は状態取得の要求開始からの経過秒で、既存の `fmtMMSS` を使い 1 秒刻みで進める。
   - 件数文言の優先順位は「全曲アップロード進行中の文言（`pocketProgressText`） > 確認中の文言 > 通常の件数文言」とし、既存の `renderPocketCount` の考え方（進行文言が最優先）を維持する。
   - バッジの「確認中」、全曲アップロードボタンと各曲のアップロードボタンの無効化は現状どおり変更しない。
   - ボタンを押した場合だけでなく、自動で走る再読込（ページ表示時、タブ復帰時、分離完了時、全曲アップロード完了時）でも同じ確認中表示にする。
2. 完了
   - 件数文言を「出力先の全パッケージ N件中、同期対象 N件 · HH:MM に更新」にする。HH:MM は完了時点の端末ローカル時刻の 24 時間表記（2 桁ゼロ埋め）。区切りは半角スペース + 中黒（U+00B7）+ 半角スペース。
   - ボタンの中身を元の「状態を再読込」に戻し、有効化して `aria-busy` を外す。
3. 失敗
   - 通信失敗(fetch の reject)、HTTP エラー応答(`res.ok` が false。本文が JSON として解析できても失敗とする)、JSON 解析失敗、または API が `state: "unknown"` を返した場合、件数文言を赤字(`#c0392b`)で「<メッセージ> · HH:MM」にする。メッセージは API が `state: "unknown"` を返した場合の `message` があればそれ、無ければ「棚の状態を確認できません。」。HTTP エラー応答と通信失敗では既定の「棚の状態を確認できません。」を使う。
   - 曲一覧下の既存エラー表示（`#sw-remote-only-error`）の挙動は変更しない。位置が離れているため重複表示は許容する。
   - 接続設定がない（`connected: false`）場合はツールバー全体が隠れる現状の挙動を維持し、件数文言は扱わない。
4. `prefers-reduced-motion: reduce` の環境ではリングの回転を止め、静止したリングを表示する。
5. 古い応答を無視する既存の世代カウンタ（`pocketStatusGeneration`）に合わせ、経過秒タイマーは最新の要求だけが動かす。新しい要求が始まったら経過秒は 0 から数え直し、要求の完了（成功・失敗とも）でタイマーを止める。
6. 件数文言には `aria-live` を付けない（毎秒の更新を読み上げさせない）。

## タスク分解

| # | タスク | 依存 |
|---|---|---|
| 1 | CSS 追加: リングスピナー（`@keyframes` による回転）、確認中ボタンの見た目、赤字文言、`prefers-reduced-motion` の停止 | - |
| 2 | JS: 確認中 / 完了 / 失敗の描画関数と経過秒タイマーを `refreshPocketStatus` の開始・成功・失敗・finally に組み込む。ボタンの初期ラベルは HTML の文字列から取得して復元する | 1 |
| 3 | 既存テスト更新: `tests/test_web_page.py` で件数文言を完全一致で検証している箇所（「出力先の全パッケージ 0件中、同期対象 0件」）を「… · HH:MM に更新」形式に合わせる（正規表現または前方一致） | 2 |
| 4 | 新規 Playwright テスト追加（`tests/test_web_page.py`）: 確認中表示、完了表示、失敗表示 | 2 |
| 5 | `uv run pytest -q -n auto` と `git diff --check` を実行し、変更対象を明示して staging し commit する | 3, 4 |

## 完了条件・受け入れ基準

- [ ] 状態取得の応答を保留した状態（テストでは `page.route("**/api/pocket/status", lambda _route: None)`）で、`#sw-pocket-refresh` が disabled、`aria-busy="true"`、テキストに「再読込中…」を含み、スピナー要素を子に持つ
- [ ] 同じ状態で `#sw-pocket-count` のテキストが「棚の状態を確認しています… (」で始まり、正規表現 `\(\d+:\d{2}\)$` に一致し、1 秒以上待つと秒の値が進む
- [ ] 状態取得が成功した後、`#sw-pocket-refresh` が有効で `aria-busy` を持たず、テキストが「状態を再読込」に戻り、`#sw-pocket-count` のテキストが正規表現 `^出力先の全パッケージ \d+件中、同期対象 \d+件 · \d{2}:\d{2} に更新$` に一致する
- [ ] 状態取得が失敗した後(テストでは `route.abort()`、および JSON 本文を持つ 500 応答の両方を検証する)、`#sw-pocket-count` のテキストが正規表現 `^棚の状態を確認できません。 · \d{2}:\d{2}$` に一致し、赤字を表すクラスを持ち、`#sw-remote-only-error` が従来どおり表示される
- [ ] 全曲アップロード進行中は進行文言が件数文言に勝つ（既存テスト `test_web_page.py` の「全曲アップロード: 0/2（First Song）」を検証するテストがそのまま通る）
- [ ] 既存テストの意図を変えずに `uv run pytest -q -n auto` が全件成功する（Playwright Chromium がある環境で実行）
- [ ] `git diff --check` に警告がない
- [ ] 作業ブランチへ commit 済み。変更対象（`src/bunri/web/templates/index.html.j2`、`tests/test_web_page.py`、計画書 2 ファイル）を明示して staging する

## 未確定事項・リスクと判断の委ね方

| 項目 | 内容 | 実装時の扱い |
|---|---|---|
| 関数名・クラス名 | 描画関数や CSS クラスの命名 | 担当が既存の `sw-` 接頭辞に合わせて決めてよい |
| テストの待ち方 | 秒が進むことの検証方法 | 担当が `page.wait_for_function` 等で決めてよい。固定 sleep で不安定にしない |
| 時刻の取得 | 完了時刻の取得元 | 端末の `Date` を使う。サーバー時刻は使わない |
| サーバー側の変更が必要になった場合 | 例: 経過秒をサーバーから返す | スコープ外。止まって報告する |
| 既存テストの意図を変えないと通らない場合 | 例: 件数文言の完全一致以外の既存検証が衝突する | 止まって報告する |

## 検証環境

- 全件テスト: `uv run pytest -q -n auto`
- 対象テストのみ: `uv run pytest -q -n auto tests/test_web_page.py`
- lint 相当: `git diff --check`
- 専用の lint / format / typecheck コマンドはない。
- CI は `.github/workflows/ci.yml` が Ubuntu で `uv run playwright install --with-deps chromium` の後に `uv run pytest -q -n auto` を実行する。ローカルに Playwright Chromium がない環境ではブラウザテストが skip されるため、検収は Chromium がある環境で行う。

## 公開経路

作業ブランチ `plan/2026-09-08-pocket-refresh-feedback` に commit するまでが承認範囲。push、`main` への PR 作成、マージは別途判断する。自動デプロイはない。CI は PR / push 時に `.github/workflows/ci.yml` が全テストを実行する。
