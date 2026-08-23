# 実装依頼: Web UI の複数楽器選択・曲単位一覧

## 背景

Web UI で1曲から複数楽器を選択して分離し、同じ音源の楽器別ジョブを1枚の曲カードで確認できるようにする。既存の単一 target API と永続ジョブの互換性を維持し、digest を曲の単位とする一覧 API を追加する。

## 対象

- リポジトリ: `shimabox/Bunri`（リポジトリルートで作業する）
- ベースブランチ: `main`（参考。起点は下の SHA）
- ベース SHA: `a6474602317b3980557f963f0944d1670a5f4d26`（**この SHA から** `git switch -c plan/2026-08-23-web-multi-target a6474602317b3980557f963f0944d1670a5f4d26` で作業ブランチを作る。ブランチ名から作らない）
- 作業ブランチ: `plan/2026-08-23-web-multi-target`
- 実装担当: Sol medium (`codex exec`)
- 実装レビュー: Sol high

## 実装仕様

### 複数 target の作成と POST 互換性

- `POST /api/jobs` は同名の `targets` フォームフィールドを複数受け取る。未指定時だけ `guitar` を既定値とする
- 空文字、重複、`REGISTRY` にない target は、アップロードファイルの保存やジョブ作成を始める前に `400` で拒否する
- 受理した target は `REGISTRY` の定義順である `guitar / bass / drums / vocals / piano` に正規化し、この順にジョブを作成・投入する
- `JobStore.create_jobs(upload_path, digest, requested_title, targets)` を追加する。バッチ内のタイトル解決は一度だけ行い、新規ジョブはすべて同じタイトルにする
- 同一 digest の既存ジョブがある場合は、最新ジョブのタイトルを引き継ぐ。同一 `created_at` の場合はジョブ ID で決定順を安定させる
- 重複排除は `digest + target` 単位とし、`done / queued / running` を再利用し、`error` は再作成可能とする。作成処理側でも target が `REGISTRY` にあることを検証する
- 既存の `create_job(...)` は `create_jobs(...)` を単一 target で呼ぶ互換ラッパーとして残す
- 単一ワーカーを維持し、新規ジョブは `REGISTRY` 順で厳密に順次実行する。並列実行は追加しない
- POST レスポンスに `jobs: [{"id": ..., "target": ..., "dedup": ...}]` を追加する。`job_id` は先頭 target のジョブ ID、`dedup` は全 target が再利用された場合だけ `true` とする
- 1件でも新規作成した場合は `202`、すべて再利用した場合は `200` とする。単一 target 利用時の既存 `job_id`、`dedup`、ステータスコードの意味を変えない

### 曲単位一覧 API

- 同じ `digest` のジョブを1曲として扱い、新しい永続 Song レコードは作らない
- 曲 ID は `digest` の UTF-8 バイト列を SHA-256 でハッシュした64文字の16進文字列とする。レスポンス時に導出し、ジョブ JSON には保存しない
- `GET /api/songs` は次の配列を返す

```json
[
  {
    "id": "<digest から導出した曲 ID>",
    "title": "<グループ内の最新ジョブの title>",
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

- 代表タイトルと曲の `created_at` はグループ内の最新ジョブから取得する。同一 target に複数ジョブがある場合も最新の1件だけを返す。最新判定は `created_at`、同値ならジョブ ID の順で安定させる
- 既知 target は `REGISTRY` 順とし、legacy の未知 target はその後へ raw target 名の辞書順で並べる。既知 target の `target_label` は `REGISTRY` の `label_ja`、未知 target は raw 名とする
- 曲順はグループ内の最新 `created_at` の降順、同値なら曲 ID で安定させる
- target 内のジョブ項目は既存 `_serialize_job(...)` と同じ意味を維持し、`target_label` を加える
- 既存の `GET /api/jobs` と `GET /api/jobs/{id}` は変更しない

### ジョブ JSON の後方互換

- 複数 target は target が異なる複数の単一ジョブとして保存する
- ジョブ JSON のフィールドと `queued / running / done / error` の状態集合を変更しない。target 配列、永続 song ID、新状態を追加しない
- `_validate_job_record` に `REGISTRY` 所属チェックを追加しない。既存の未知 target レコードも現在の型、UTF-8、サイズ、日時、パス形状の検証を通れば読み込む
- 既存形式のレコードが従来の状態と package URL で取得できることを回帰テストする

### Web UI と README

- `src/bunri/web/templates/index.html.j2` のアップロード確認欄に `guitar / bass / drums / vocals / piano` のチェックボックスをこの順で追加する。確認欄を開くたびに `guitar` のみを既定選択にする
- 選択が0件ならアップロードボタンを無効化し、送信処理にも選択必須のガードを置く。送信時は選択値ごとに同名の `targets` フォームフィールドを追加する
- 一覧の見出しは「曲一覧」のまま維持し、polling 先を `/api/songs` に切り替える。いずれかの target が `queued` または `running` の間は再取得を続ける
- 1曲1カードとし、各 target の行に楽器名、状態バッジ、処理中の経過時間、失敗時のエラー詳細、完了時の target 固有の「プレイヤーを開く」リンクを表示する
- 外部 UI 依存を追加せず、既存の素の HTML/CSS/JavaScript の構成を維持する
- `README.md` の「Web UI で使う」節に、複数楽器選択、順次実行、曲単位の楽器別状態とリンク、同一音源・同一 target の再利用を反映する。既存の書式に合わせ、段落や箇条書き項目の途中でハード改行しない

## タスク(この順で)

1. ベース SHA から指定コマンドで作業ブランチを作り、対象コードと既存テストの前提を確認する
2. `src/bunri/web/jobs.py` に target 検証、曲 ID 導出、曲グルーピング、`create_jobs(...)` を実装し、`create_job(...)` を互換ラッパーにする
3. `src/bunri/web/app.py` で複数 `targets` の事前検証、バッチ作成、互換 POST レスポンス、`GET /api/songs` のシリアライズを実装する
4. `src/bunri/web/templates/index.html.j2` に5楽器のチェックボックス、選択必須制御、複数 target 送信、曲カードと楽器別行、`/api/songs` polling を実装する
5. `tests/test_web_jobs.py` に、バッチ作成時の同一曲名の引き継ぎ、target 別 dedup、`REGISTRY` 順で実行時間が重ならない順次実行のテストを追加する
6. `tests/test_web_api.py` に、複数 target、未指定時の `guitar`、未知・空・重複 target の `400` と処理前拒否、`GET /api/songs` の digest グルーピング・最新ジョブ選択・曲順・target 順、単一 target POST の互換レスポンスを追加する
7. `tests/test_web_page.py` に、既定チェック状態、0件選択時の制御、複数チェックボックス送信、曲単位表示、楽器別状態とプレイヤーリンクの Playwright テストを追加する
8. `tests/test_web_jobs.py` または `tests/test_web_api.py` に、既存形式と未知 target を含むジョブ JSON の読み込み回帰テストを追加する
9. `README.md` の「Web UI で使う」節を指定仕様に合わせて更新する
10. 対象テストと全テストを実行する。Chromium が未導入で画面テストを実行できない場合だけ `uv run playwright install --with-deps chromium` で準備する
11. 実装変更を機能単位で作業ブランチへ commit する。store/API、フロント、テスト/README などに分けてよい。各 commit では変更ファイルを明示して `git add` し、`docs/plans/` 配下を含めない
12. ベース SHA との差分を Sol high で読み取り専用レビューし、API 互換性、ジョブ JSON の後方互換、順次実行、曲グルーピング、画面の選択必須制御を確認する。指摘があれば Sol medium で修正し、該当テストと全テストを再実行して追加 commit する

## 完了条件

- [ ] アップロード確認欄に5楽器が指定順で表示され、初期状態は `guitar` のみ選択されている
- [ ] 0件選択ではアップロードできず、複数選択時は選択した target ごとに同名の `targets` フォームフィールドが送信される
- [ ] 選択した target ごとに共通タイトルの単一ジョブが作成され、`REGISTRY` 順にキューへ入り、単一ワーカーで重ならずに実行される
- [ ] target 未指定のリクエストは `guitar` を使い、未知・空・重複 target はファイル保存やジョブ作成前に `400` となる
- [ ] `digest + target` 単位の再利用と、失敗ジョブの再作成が機能する
- [ ] POST が target ごとの `id / target / dedup` を `jobs` 配列で返し、単一 target の既存 `job_id`、`dedup`、ステータスコードを維持する
- [ ] `GET /api/songs` が同一 digest を1曲にまとめ、同一 target の最新ジョブ、決定的な曲 ID、定義済みの曲順と target 順を返す
- [ ] Web UI が1曲1カードを表示し、楽器別の名前、状態、処理中の経過時間、失敗の詳細、完了時の固有プレイヤーリンクを表示する
- [ ] 既存の `GET /api/jobs` と `GET /api/jobs/{id}` が維持される
- [ ] 既存形式と未知 target のジョブ JSON が隔離されず、従来の状態と package URL で読み込める
- [ ] 外部 UI 依存、新しいジョブ状態、永続 Song レコードが追加されていない
- [ ] `README.md` の「Web UI で使う」節が更新され、段落や箇条書き項目の途中でハード改行されていない
- [ ] `uv run pytest -q tests/test_web_jobs.py tests/test_web_api.py tests/test_web_page.py` がパスする
- [ ] `make test` がパスする
- [ ] Sol high のレビューで重大な未解決指摘がない
- [ ] 作業ブランチに commit 済みで、`docs/plans/` 配下の `plan.md` / `request.md` が commit に含まれていない
- [ ] push していない

## 未確定事項と判断の委ね方

- 勝手に決めてよい範囲: helper の命名や配置、内部の戻り値型、CSS の class 名、テスト fixture の分割、エラーメッセージの文言など、上記 API 契約・表示要件・後方互換を変えない実装詳細
- 止まって報告すべき範囲: スコープ変更、既存 API フィールドや状態の削除・意味変更、ジョブ JSON スキーマ変更、ジョブの並列化、外部依存の追加、既存データの隔離や移行が必要になる変更、対象テストまたは全テストの未解決失敗

## 禁止事項

- push しない（commit まで）
- `git add -A` を使わない。commit 対象の変更ファイルを明示して add する
- `docs/plans/` 配下の `plan.md` / `request.md` を commit に含めない
- 曲削除 API、確認モーダル、`safepath` の削除 helper、削除関連テストを実装しない
- CLI の複数 target 一括指定、ジョブの並列実行、実行中ジョブのキャンセルを追加しない
- スコープ外のファイルを触らない（無関係なリファクタを行わない）

## 報告フォーマット

- 変更ファイル一覧
- commit 一覧
- 実行したテスト・lint とその結果（lint が未設定ならその旨）
- Sol high のレビュー結果と、指摘への対応
- 判断に迷った点・未解決の懸念
