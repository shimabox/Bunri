# Web UI の複数楽器選択・曲単位一覧 実装計画

- 日付: 2026-08-23
- ブランチ: `plan/2026-08-23-web-multi-target`
- 実装担当(駒): Sol medium (`codex exec`)。既存の Python/FastAPI、素の HTML/CSS/JavaScript、テストをまたぐ変更を一貫して実装するのに適した構成とする
- 実装レビュー: Sol high。API 互換性、永続ジョブの後方互換、非同期処理と画面表示の整合を重点的に確認する

## 背景・目的

現在の Web UI はアップロードごとに `guitar` のジョブを1件だけ作り、一覧もジョブ単位で表示している。1曲から複数の楽器を選んで分離できるようにし、同じ音源に属する楽器別ジョブを1枚の曲カードで確認できる構成へ拡張する。既存の単一 target クライアントと永続済みジョブを壊さず、後続の曲削除機能が同じ曲の定義と曲 ID を利用できる土台も整える。

## スコープ

### やること

- アップロード確認欄に `guitar / bass / drums / vocals / piano` のチェックボックスを追加し、初期状態では `guitar` のみを選択する
- 1つ以上の楽器選択を必須とし、選択した target ごとに既存スキーマの単一ジョブを1件ずつ作る
- `JobStore.create_jobs(...)` によるバッチ作成を追加し、同一曲のタイトル決定、target 別の重複排除、既存の単一ワーカーによる順次実行を維持する
- `GET /api/songs` を追加し、同じ `digest` のジョブを1曲としてグルーピングして返す
- Web UI の見出しを「曲一覧」のまま維持し、1曲1カードの中に楽器別の状態、処理中の経過時間、失敗時のエラー、完了時のプレイヤーリンクを表示する
- `POST /api/jobs` の複数 target 入力とレスポンスを追加しつつ、target 未指定時の `guitar` と既存の `job_id` / `dedup` を維持する
- 既存形式のジョブ JSON のフィールド、状態、読み込み互換性を維持する
- ジョブストア、API、実ブラウザ画面のテストを追加・更新する
- `README.md` の「Web UI で使う」節を複数楽器選択と曲単位一覧に合わせて更新する

### やらないこと

- 曲削除（削除 API、確認モーダル、`safepath` の削除 helper、削除関連テストを含む。次の PR で扱う）
- CLI の複数 target 一括指定
- 分離モデル、キャッシュ形式、音声処理パイプラインの変更
- ジョブの並列実行、実行中ジョブのキャンセル、新しいジョブ状態の追加
- 認証・ユーザー管理、LAN 公開
- UI フレームワークや外部 UI 依存の追加

## 方針

### 複数楽器のジョブ生成

`POST /api/jobs` は同名の `targets` フォームフィールドを複数受け取る。フィールドが未指定なら `guitar` だけを指定したものとして扱い、空文字、重複、`REGISTRY` にない値は、ファイル保存やジョブ作成を始める前に `400` で拒否する。受理した target はリクエスト内の並びに依存せず `REGISTRY` の定義順（`guitar / bass / drums / vocals / piano`）へ正規化し、この順に作成・投入する。

`JobStore.create_jobs(upload_path, digest, requested_title, targets)` を追加し、バッチ全体をロック内で扱う。既存の同一 `digest` ジョブがあれば、画面でその曲を代表する最新ジョブのタイトルをバッチの共通タイトルとして引き継ぐ。なければタイトル衝突解決をバッチにつき一度だけ行い、バッチ内で新規作成する全 target に同じタイトルを使う。重複排除は従来どおり `digest + target` ごとに行い、`done / queued / running` は再利用し、`error` は再作成できるようにする。作成処理側でも target が `REGISTRY` にあることを検証する。既存の `create_job(...)` は単一 target を `create_jobs(...)` に渡す互換ラッパーとして残す。

ワーカーは現在の1本のままとし、キューへ入れた新規ジョブを厳密に順次実行する。並列化は、分離処理の GPU・CPU・メモリ競合と共有キャッシュへの同時書き込みを増やすため採用しない。

レスポンスには `jobs` 配列を追加し、各要素を `id / target / dedup` とする。互換フィールドの `job_id` は正規化後の先頭 target のジョブ ID、`dedup` はリクエスト内の全 target が再利用された場合だけ `true` とする。1件でも新規作成した場合は `202`、すべて再利用した場合は `200` とし、単一 target のリクエストでは従来の `job_id`、`dedup`、ステータスコードの意味を変えない。

### 曲単位の一覧

新しい永続 Song レコードは作らず、同じ `digest` のジョブを1曲として扱う。曲 ID は `digest` の UTF-8 バイト列を SHA-256 でハッシュした64文字の16進文字列とし、決定的で URL-safe な値としてレスポンス時に導出する。曲 ID はジョブ JSON に保存しない。

`GET /api/songs` は次の形の配列を返す。

```json
[
  {
    "id": "<digest から導出した曲 ID>",
    "title": "<代表タイトル>",
    "created_at": "<グループ内で最新の created_at>",
    "targets": [
      {
        "id": "<job id>",
        "target": "guitar",
        "target_label": "ギター",
        "status": "done",
        "created_at": "<job created_at>",
        "started_at": "<job started_at または null>",
        "finished_at": "<job finished_at または null>",
        "elapsed_seconds": 123.0,
        "package_url": "/packages/...player.html",
        "error": null
      }
    ]
  }
]
```

代表タイトルはグループ内の最新ジョブのタイトルとし、同一 `created_at` ではジョブ ID をタイブレークにして決定結果を安定させる。同じ target に失敗や再試行を含む複数ジョブがある場合も、同じ規則で最新の1件だけを `targets` に含める。既知 target は `REGISTRY` 順で並べ、既存データに含まれる未知 target はその後へ raw target 名の辞書順で並べ、`target_label` にも raw 名を使う。曲はグループ内の最新 `created_at` の降順とし、同値の場合は曲 ID で安定化する。既存の `GET /api/jobs` と `GET /api/jobs/{id}` は変更せず維持する。

フロントは `/api/songs` を polling し、各曲の `targets` に `queued` または `running` がある間は再取得を続ける。1曲1カードとし、カード内の各行に楽器名、状態バッジ、処理中のみ経過時間、失敗時のみエラー詳細、完了時のみ target 固有の「プレイヤーを開く」リンクを描画する。

### ジョブ記録の後方互換

複数 target は従来どおり、target が異なる複数の単一ジョブとして永続化する。ジョブ JSON の既存フィールドと `queued / running / done / error` の状態集合は変更せず、target 配列、永続 song ID、新状態は追加しない。`_validate_job_record` に `REGISTRY` 所属チェックを追加せず、既存の未知 target レコードも現在の型、UTF-8、サイズ、日時、パス形状の検証を通る限り読み込む。新規作成時の target 妥当性だけを API と作成処理で検証し、既存レコードの状態と package URL を維持する。

### Web UI と README

アップロード確認欄では5楽器を `REGISTRY` 順に表示し、確認欄を開いた時点で `guitar` のみを選択する。選択が0件ならアップロードボタンを無効化し、送信処理にもガードを置いて利用者に選択必須を示す。送信時は選択した各値を同名の `targets` フォームフィールドとして追加する。

`README.md` の「Web UI で使う」節では、アップロード時に複数楽器を選択できること、処理は順次実行されること、一覧が曲単位で楽器別状態とリンクを表示すること、同一音源・同一 target の結果が再利用されることを説明する。既存の書式に合わせ、1つの段落や箇条書き項目の途中でハード改行しない。

## タスク分解

| # | タスク | 依存 |
|---|---|---|
| 1 | `src/bunri/web/jobs.py` に target 検証、曲 ID 導出、曲グルーピング、`create_jobs(...)` を追加し、`create_job(...)` を互換ラッパー化する | - |
| 2 | `src/bunri/web/app.py` で複数 `targets` の事前検証、バッチ作成、互換レスポンス、`GET /api/songs` のシリアライズを実装する | 1 |
| 3 | `src/bunri/web/templates/index.html.j2` に5楽器のチェックボックス、選択必須制御、複数 target 送信、曲単位カードと楽器別行、`/api/songs` polling を実装する | 2 |
| 4 | `tests/test_web_jobs.py` にバッチ作成の同一曲名引き継ぎ、target 別 dedup、`REGISTRY` 順の厳密な順次実行を追加する | 1 |
| 5 | `tests/test_web_api.py` に複数 target、既定 `guitar`、未知・空・重複 target の `400`、曲のグルーピングと順序、単一 target POST の互換レスポンスを追加する | 2 |
| 6 | `tests/test_web_page.py` にチェックボックス送信、曲単位表示、楽器別の状態・リンクを確認する Playwright テストを追加する | 3 |
| 7 | 既存形式と未知 target を含むジョブ JSON が隔離されず、従来の状態と package URL のまま読み込める回帰テストを追加する | 1, 2 |
| 8 | `README.md` の「Web UI で使う」節を複数楽器選択と曲単位一覧に合わせて更新する | 3 |
| 9 | 対象テストと全テストを実行し、Sol high のレビュー指摘を反映して再検証する | 4–8 |

## 完了条件・受け入れ基準

- [ ] アップロード確認欄に5楽器が `guitar / bass / drums / vocals / piano` の順で表示され、初期状態は `guitar` のみ選択されている
- [ ] 0件選択時はアップロードを開始できず、1件以上を選択すると同名の `targets` フォームフィールドで送信される
- [ ] 複数楽器を選ぶと選択した各 target の単一ジョブが1件ずつ作られ、共通の曲名を持つ
- [ ] target ジョブは `REGISTRY` 順にキューへ入り、既存の単一ワーカーで実行時間が重ならず順次実行される
- [ ] target 未指定の既存リクエストは `guitar` ジョブを作る
- [ ] 未知、空、重複 target はファイル保存やジョブ作成の前に `400` となる
- [ ] 同一 `digest + target` の `done / queued / running` ジョブは再利用され、`error` ジョブは再作成できる
- [ ] `POST /api/jobs` は target ごとの `id / target / dedup` を `jobs` 配列で返し、既存の `job_id` と `dedup` を維持する
- [ ] 1件でも新規ジョブがあれば `202`、すべて再利用なら `200` となる
- [ ] `GET /api/songs` は同じ digest を1曲にまとめ、同一 target の最新ジョブだけを含め、曲と target を定義済みの順序で返す
- [ ] 曲 ID は digest から決定的に導出される URL-safe な値で、ジョブ JSON には保存されない
- [ ] 一覧は見出し「曲一覧」を維持して1曲1カードとなり、各カードに楽器別の名前、待機中・処理中・完了・失敗の状態、処理中の経過時間、失敗の詳細、完了した target 固有のプレイヤーリンクが適切に表示される
- [ ] 既存の `GET /api/jobs` と `GET /api/jobs/{id}` のレスポンスが維持される
- [ ] 既存形式および未知 target のジョブ JSON が隔離されず、従来の状態と package URL で読み込める
- [ ] 外部 UI 依存と新しいジョブ状態が追加されていない
- [ ] `README.md` の「Web UI で使う」節が複数楽器選択と曲単位一覧を説明し、既存の改行形式に従っている
- [ ] `uv run pytest -q tests/test_web_jobs.py tests/test_web_api.py tests/test_web_page.py` がパスする
- [ ] `make test` がパスする

## 未確定事項・リスクと判断の委ね方

| 項目 | 内容 | 実装時の扱い |
|---|---|---|
| API の外部利用 | リポジトリ外の利用者は確認できず、レスポンス項目追加の影響範囲を完全には把握できない | 既存エンドポイントと単一 target POST の `job_id` / `dedup` / ステータスコードを維持する。破壊的変更が必要なら止まって報告する |
| legacy の同一 digest・複数タイトル | 過去データでは同じ digest に複数タイトルが存在する可能性がある | 最新ジョブのタイトルを代表値にし、新規 target もそのタイトルを引き継ぐ。既存ジョブやパッケージは変更しない |
| legacy の未知 target | 現行 validator は target を文字列として受け入れるため、`REGISTRY` 外の記録が存在し得る | 記録を読み込み、曲一覧では既知 target の後に raw 名で表示する。新規作成だけを拒否する |
| 同一時刻の順序 | 手作業の legacy JSON などで `created_at` が一致する可能性がある | ジョブ ID、曲 ID をタイブレークに使い、テスト可能な決定順にする |
| lint | 専用の lint、formatter、type-checker コマンドが設定されていない | 新規依存を追加せず、対象テストと `make test` を品質ゲートとする |
