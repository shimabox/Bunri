# 実装依頼: StemLab → Bunri 全面リネーム

## 背景

サービス名を StemLab から Bunri(分離)へ変更する。Pythonパッケージ、CLI、画面表示、Docker、配布物まで Bunri へ統一する。利用者は開発者本人のみのため、旧名称との互換機能や移行案内は設けず、完全なクリーンブレークとする。バージョンは 0.2.0 に上げる。

表記規則: 製品名・画面表示は Bunri / Pythonパッケージ・CLI・Docker名は bunri / 環境変数は BUNRI_*。

## 対象

- リポジトリ: StemLab（現 Bunri。このリポジトリのルートで作業する）
- 作業ブランチ: plan/2026-08-22-rename-bunri(ここから作成して作業する)
- ベースブランチ: main

## タスク(この順で)

1. **Pythonパッケージの移動**: `src/stemlab/` を `src/bunri/` へ移動する(`git mv` 推奨)。`src/stemlab/` は残さない。対象は配下の全ファイル(`__init__.py`、`audio.py`、`cache.py`、`cli.py`、`package.py`、`player.py`、`registry.py`、`safepath.py`、`separate.py`、`templates/player.html.j2`、`web/__init__.py`、`web/app.py`、`web/cli.py`、`web/jobs.py`、`web/templates/index.html.j2`)。
2. **import・パッケージ参照の変更**: 全importを `stemlab.*` から `bunri.*` へ変更する。Jinja2 の `PackageLoader` を `bunri` へ変更する。docstringと現行コードコメントを Bunri 表記へ変更する。
3. **配布名・CLI・バージョンの変更**: `pyproject.toml` で distribution名を `bunri`、version を `0.2.0`、entry point を `bunri = "bunri.cli:app"` / `bunri-web = "bunri.web.cli:app"` に変更する。`src/bunri/__init__.py` の `__version__` も `0.2.0` にする。旧entry point(`stemlab` / `stemlab-web`)は残さない。エイリアスも提供しない。
4. **環境変数・モデルディレクトリの変更**: 正式な環境変数を `BUNRI_MODEL_DIR` とする。解決順序は 1. `BUNRI_MODEL_DIR` 2. プロジェクト内の `models/` 3. 外部インストール時の `~/.cache/bunri/models`。`STEMLAB_MODEL_DIR` は参照しない(フォールバックも設けない)。
5. **Webワーカー・プロセスマーカーの変更**(`src/bunri/web/jobs.py` ほか該当箇所):
   - サブプロセス起動を `python -m bunri.cli` へ変更
   - プロセスマーカーを `bunri.cli` へ変更(`stemlab.cli` は許容しない。legacy marker対応は作らない)
   - worker thread名を `bunri-web-worker` へ、ログ接頭辞を `[bunri-web]` へ変更
   - 安全性に関する既存原則は維持する: マーカー不一致のプロセスへsignalしない/停止確認前にPID sidecarを削除しない/停止を確認できないジョブを再実行しない/プロセスグループ全体を停止する
6. **表記・内部識別子の変更**: `README.md`(タイトル・説明・コマンド例・clone手順。移行案内は書かない)/ `Makefile` / `setup.sh` / CLI出力 / Web UIのtitle・見出し・説明(`src/bunri/web/templates/index.html.j2`)/ FastAPIのtitle(`src/bunri/web/app.py`)/ Python docstringと現行コメント / JavaScriptグローバル識別子 を Bunri 表記へ変更する。
7. **Docker・compose・CIの変更**(`Dockerfile`、`compose.yaml`、`.github/workflows/ci.yml`):
   - Dockerイメージ: `bunri:cpu` / `bunri:cuda` / `bunri:dev`
   - CPU/CUDAのENTRYPOINT: `bunri`
   - compose service: `bunri` / named volume: `bunri-models` / モデルmount先: `/root/.cache/bunri/models` / 環境変数: `BUNRI_MODEL_DIR`
   - CI内のコメント、Dockerfile内のコメントも Bunri 表記へ変更
   - 旧モデルキャッシュや旧Docker volumeの移行機能は作らない
8. **移行スクリプトの削除**: 旧キャッシュ移行用の `scripts/migrate_model_cache.py` を削除する。
9. **全テストのリネーム追従**(`tests/` 配下):
   - `stemlab.*` importをすべて `bunri.*` へ変更(monkeypatch先、fixture、コメントも追従)
   - `window.__stemlabWeb` を `window.__bunriWeb` へ変更
   - Hypothesis profile `stemlab` を `bunri` へ変更
   - `STEMLAB_TEST_LOG` を `BUNRI_TEST_LOG` へ変更
   - fake runnerのログ文字列を Bunri へ変更
   - プロセステストのmarkerを `bunri.cli` へ変更
   - CLIテストを `bunri` / `bunri-web` に追従
   - smoke testで `bunri.__version__ == "0.2.0"` を確認
10. **リネーム固有の回帰テストの追加**: 既存のDockerfile軽量テスト(`tests/test_dockerfile.py`)を拡張する — CPU/CUDAのENTRYPOINTが `bunri` / `BUNRI_MODEL_DIR` が使用されている / モデルパスが `/root/.cache/bunri/models` / `stemlab` entry pointが残っていない / dev/dev-depsの `--extra web` が維持されている、を検証する。
11. **uv.lock の再生成**: distribution名変更に伴い `uv.lock` を再生成する。
12. **検収**: pytest(`uv run pytest -q`)、lock(`uv lock --check`)、build(`uv build` と wheel の中身・entry point確認)、CLI(`uv run bunri --help` / `uv run bunri-web --help` / `python -m bunri.cli --help`)、Web起動(`bunri-web --no-open` で起動しHTTP接続確認)を実施する。`git grep -i stemlab` の残存が歴史文書(`.claude/plans/**`、`docs/plans/**`、`NOTES.md`)だけであることを確認する。

## 完了条件

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

## 未確定事項と判断の委ね方

- 勝手に決めてよい範囲: コミット分割・一括置換の手段などの実装詳細
- 止まって報告すべき範囲: スコープ変更・受け入れ基準を満たせない場合・依存追加

## 禁止事項

- push しない(commit まで)
- スコープ外のファイルを触らない(`.claude/plans/`、`docs/plans/`、`NOTES.md`、`models/`、`out/` 等。リファクタの誘惑に乗らない)
- 旧名の互換機能を勝手に足さない(旧CLIエイリアス・STEMLAB_MODEL_DIR フォールバック・legacy marker対応・移行案内はすべて対象外)
- PyPIへの実公開はしない

## 報告フォーマット

- 変更ファイル一覧
- 実行した検証(テスト・lock・build・CLI・Web起動)とその結果
- 判断に迷った点・未解決の懸念
