# StemLab → Bunri 全面リネーム 実装計画

- 日付: 2026-08-22
- ブランチ: plan/2026-08-22-rename-bunri
- 実装担当(駒): Claude sonnet — 機械的リネームが主体で設計は確定済みのため(デフォルト編成)
- バージョン: 0.2.0
- レビュー: Sol(herdr可視実行)

## 背景・目的

サービス名を StemLab から Bunri(分離)へ変更する。検索性・個性を高め、ツールの本質である音源分離と名称を一致させる。Pythonパッケージ、CLI、画面表示、Docker、配布物、リポジトリ名まで Bunri へ統一する。利用者は開発者本人のみのため、旧名称との互換機能や移行案内は設けず、完全なクリーンブレークとする。

## スコープ

### やること

- Pythonパッケージのリネーム
- distribution名、CLI、バージョンの変更
- 全import・内部識別子・表示文言の変更
- 環境変数、モデルキャッシュパスの変更
- Webワーカーとプロセスマーカーの変更
- README、Makefile、setup.shの変更
- Dockerfile、compose、CIの変更
- テストの追従と回帰テスト追加
- wheelなどの配布物検証
- マージ後のGitHubリポジトリ名・ローカルフォルダ名変更

### やらないこと

- .claude/plans/、docs/plans/、NOTES.md の書き換え
- 旧CLI stemlab / stemlab-web のエイリアス提供
- STEMLAB_MODEL_DIR のフォールバック
- stemlab.cli のlegacy marker対応
- 旧モデルキャッシュやDocker volumeの移行機能
- READMEへの移行案内
- PyPIへの実公開

## 方針

### 1. Pythonパッケージ

- src/stemlab/ を src/bunri/ へ変更
- 全importを stemlab.* から bunri.* へ変更
- Jinja2の PackageLoader を bunri へ変更
- docstringと現行コードコメントをBunri表記へ変更
- テスト内のimport、monkeypatch先、fixture、コメントを追従
- src/stemlab/ は残さない

### 2. 配布名・CLI・バージョン

pyproject.toml: distribution名 bunri / version 0.2.0 / CLI: `bunri = "bunri.cli:app"`、`bunri-web = "bunri.web.cli:app"`。src/bunri/__init__.py の __version__ も 0.2.0。旧entry pointは残さない。

### 3. 環境変数・モデルディレクトリ

正式な環境変数を BUNRI_MODEL_DIR とする。解決順序: 1. BUNRI_MODEL_DIR 2. プロジェクト内の models/ 3. 外部インストール時の ~/.cache/bunri/models。STEMLAB_MODEL_DIR は参照しない。旧キャッシュ移行用の scripts/migrate_model_cache.py は削除する。

### 4. Webワーカー・プロセスマーカー

- サブプロセス起動を python -m bunri.cli へ変更
- プロセスマーカーを bunri.cli へ変更(stemlab.cli は許容しない)
- worker thread名を bunri-web-worker へ、ログ接頭辞を [bunri-web] へ変更
- 安全性に関する既存原則は維持する: マーカー不一致のプロセスへsignalしない/停止確認前にPID sidecarを削除しない/停止を確認できないジョブを再実行しない/プロセスグループ全体を停止する
- 切り替え前に旧Webサーバーと旧分離プロセスをすべて停止する(マージ後手順)

### 5. 表記・内部識別子

READMEタイトル・説明・コマンド例・clone手順 / Makefile / setup.sh / CLI出力 / Web UIのtitle・見出し・説明 / FastAPIのtitle / Python docstringと現行コメント / JavaScriptグローバル識別子 / Hypothesis profile名 / テスト専用環境変数 / CI内のコメント / Dockerfile内のコメント をBunri表記へ変更。

表記規則: 製品名・画面表示は Bunri / Pythonパッケージ・CLI・Docker名は bunri / 環境変数は BUNRI_*。

### 6. Docker・compose

- Dockerイメージ: bunri:cpu / bunri:cuda / bunri:dev
- CPU/CUDAのENTRYPOINT: bunri
- compose service: bunri / named volume: bunri-models / モデルmount先: /root/.cache/bunri/models / 環境変数: BUNRI_MODEL_DIR
- 既存のDockerfile軽量テストを拡張: CPU/CUDAのENTRYPOINTが bunri / BUNRI_MODEL_DIR が使用されている / モデルパスが /root/.cache/bunri/models / stemlab entry pointが残っていない / dev/dev-depsの --extra web が維持されている

### 7. テスト内部の追従

- stemlab.* importをすべて bunri.* へ変更
- window.__stemlabWeb を window.__bunriWeb へ変更
- Hypothesis profile stemlab を bunri へ変更
- STEMLAB_TEST_LOG を BUNRI_TEST_LOG へ変更
- fake runnerのログ文字列をBunriへ変更
- プロセステストのmarkerを bunri.cli へ変更
- CLIテストを bunri / bunri-web に追従
- smoke testで bunri.__version__ == "0.2.0" を確認

## タスク分解

| # | タスク | 依存 |
|---|---|---|
| 1 | src/stemlab/ を src/bunri/ へ移動 | - |
| 2 | 全import・パッケージ参照を変更 | 1 |
| 3 | pyproject.toml のdistribution、version、entry pointを変更 | 2 |
| 4 | 環境変数とモデルディレクトリを変更 | 3 |
| 5 | Webワーカー、marker、ログ識別子を変更 | 4 |
| 6 | README、Makefile、setup.sh、Web UIを変更 | 5 |
| 7 | Dockerfile、compose、CIを変更 | 6 |
| 8 | 不要なモデルキャッシュ移行スクリプトを削除 | 7 |
| 9 | 全テストをリネームへ追従 | 8 |
| 10 | リネーム固有の回帰テストを追加 | 9 |
| 11 | uv.lock を再生成 | 10 |
| 12 | pytest、lock、build、CLI、Web起動を検収 | 11 |
| 13 | Solレビューを実施(采配役が行う — request.md のタスクには含めない) | 12 |
| 14 | 合格後にmainへマージ(采配役・ユーザー — request.md のタスクには含めない) | 13 |
| 15 | GitHubリポジトリ名とローカルフォルダ名を変更(マージ後、采配役 — request.md のタスクには含めない) | 14 |

## 完了条件・受け入れ基準

- [ ] uv lock --check が成功
- [ ] uv run pytest -q が全件成功
- [ ] git diff --check が成功
- [ ] uv build が成功
- [ ] wheel内のPythonパッケージが bunri/
- [ ] wheelのentry pointが bunri / bunri-web
- [ ] stemlab / stemlab-web entry pointが存在しない
- [ ] uv run bunri --help が成功
- [ ] uv run bunri-web --help が成功
- [ ] python -m bunri.cli --help が成功
- [ ] bunri-web --no-open が起動する
- [ ] Web UIへHTTP接続できる
- [ ] BUNRI_MODEL_DIR が指定先として使われる
- [ ] 未設定時にBunriの既定モデルパスが使われる
- [ ] 新規ワーカーが python -m bunri.cli を実行する
- [ ] bunri.cli のプロセスをsidecarから安全に停止できる
- [ ] マーカー不一致のPIDへsignalしない
- [ ] DockerfileのENTRYPOINT、環境変数、モデルパスがBunriへ統一されている
- [ ] compose service、image、volumeがBunriへ統一されている
- [ ] 現行README、コード、テスト、設定にStemLab表記が残っていない
- [ ] git grep -i stemlab の残存が歴史文書(.claude/plans/**、docs/plans/**、NOTES.md)だけ
- [ ] 作業ブランチに commit 済みであること

## マージ後の切り替え手順(実装駒のタスクではない)

1. 旧 stemlab-web を停止
2. 実行中の旧分離ジョブがないことを確認
3. 実装ブランチをmainへマージ
4. GitHubリポジトリ名を Bunri へ変更
5. ローカルの origin を新URLへ変更
6. git remote -v / git ls-remote origin を確認
7. ローカルフォルダを StemLab から Bunri へ変更
8. 旧絶対パスを含む .venv を削除
9. 新フォルダで uv sync --locked --extra web
10. Claude Codeのプロジェクト記憶を新パスへ移す
11. 必要ならagmsgを新パスで再登録
12. 新パスからCLI、Web UI、pytestを最終確認

## 未確定事項・リスクと判断の委ね方

なし。完全なクリーンブレークとして実施する。
