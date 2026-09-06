# 実装依頼: Bunri Pocket Web 棚同期・一括同期

## 背景

既存の `bunri pocket sync <SAFE_NAME>` の安全で冪等な同期ロジックを CLI/Web 共通サービスへ整理し、Web UI からの個別・一括同期と CLI の `bunri pocket sync --all` を追加する。同期状態、進行状況、旧パッケージの再生成案内を利用者に示しつつ、秘密情報と既存 Web ジョブの後方互換を保護する。

## 対象

- リポジトリ: `github.com/shimabox/Bunri`
- ベースブランチ: `main`（参考。起点は下記 SHA）
- ベース SHA: `60043e02722817c7ad83ad5c8ec42387208724f2`（**この SHA から** `git switch -c plan/2026-09-05-pocket-web-sync-all 60043e02722817c7ad83ad5c8ec42387208724f2` で作業ブランチを作る。ブランチ名から作らない）
- 作業ブランチ: `plan/2026-09-05-pocket-web-sync-all`
- 実装担当(駒): Sol medium（`codex exec`、`workspace-write`）

最初に次を実行し、指定 SHA を起点に作業すること。

```bash
git switch -c plan/2026-09-05-pocket-web-sync-all 60043e02722817c7ad83ad5c8ec42387208724f2
```

### 触ってよいファイル範囲

- `src/bunri/pocket/` 配下。既存 `cli.py`、`config.py`、`http.py`、`local.py`、`protocol.py`、`sync.py` の更新、および責務を明確にするための共通サービス/lock 用モジュール追加を含む。
- `src/bunri/web/app.py`
- `src/bunri/web/jobs.py`
- `src/bunri/web/templates/index.html.j2`
- `tests/test_cli.py`
- `tests/test_pocket_*.py`
- `tests/test_web_api.py`
- `tests/test_web_jobs.py`
- `tests/test_web_jobs_properties.py`
- `tests/test_web_page.py`
- 上記と同じ責務に限定した、新規の `tests/test_pocket_*.py` または `tests/test_web_*.py`
- `README.md`

上記以外のファイル、新しい HTTP/runtime dependency、Bunri Pocket Worker/PWA、バージョン情報、リリース設定は変更しない。許可範囲外の変更が不可避なら実装を止め、必要な選択肢と推奨を報告する。

## 確定仕様

- `sync --all` でサイドカーのない旧パッケージは同期対象外として全件報告し、「再生成が必要」と案内する。有効な曲の同期は続行し、legacy の存在だけでは非0終了にしない。
- 一括同期中に1曲で remote エラーが発生したら、その曲で停止する。完了・失敗・未実行を集計表示し、非0終了する。後続曲は実行せず、再実行時に冪等同期で収束させる。
- Web の `全曲アップロード` は、Web 一覧に見える曲だけでなく、出力ディレクトリ内の全パッケージを対象にする。Web 一覧に出ない CLI 生成曲も含め、対象件数を UI に表示する。
- sync 排他は非ブロッキングとする。競合時、CLI は明示エラー、Web は HTTP 409 を返し、二重ジョブを作らない。
- Web からの同期は original MP3 を含める。`--no-original` 相当の UI は追加しない。
- Pocket status API の endpoint 名と JSON の細部は、秘密を返さず、song ID で対象を指定し、remote 検査を軽量なジョブポーリングから分離する限り、実装者が決めてよい。

## タスク(この順で)

1. 現行挙動の回帰テストを固定する。
   - CLI の既存 `bunri pocket sync <SAFE_NAME>`、Pocket の preflight/HTTP/protocol/synchronize、Web API/job/page の現在の契約を確認する。
   - 既存分離 Job JSON に job kind がないこと、分離 worker が CLI subprocess を使うこと、起動時 recovery、same-origin、曲削除の既存挙動を回帰テストで保護する。

2. 全件パッケージ探索とローカル identity 検証を `src/bunri/pocket/` に追加する。
   - `src/bunri/pocket/local.py` の `preflight()` を同期元の正本として維持し、サイドカーとディレクトリ直下の実 MP3 を再検査して SHA-256/size を算出する。
   - 最大20件に制限された既存 `package_candidates()` は一括列挙に流用せず、全件を決定的な順序で返す関数を追加する。
   - dot directory、`web`、`.cache`、`.pocket`、symlink、通常ディレクトリでないものを候補から除外する。
   - サイドカーのないディレクトリを legacy、すなわち `再生成が必要` として分類する。
   - Web 個別同期用に expected full SHA-1 を受ける検査を用意し、実行直前にサイドカーの full digest と一致することを確認する。
   - 同じ full digest の別 safe name、および同じ12桁 song ID の別 full digest を検出する。正しいタイトルや asset を推測しない。

3. PUT を行わない remote 状態検査を追加する。
   - 状態確認は GET/HEAD のみに限定する。
   - `未同期`: remote manifest が存在せず、library にも対象曲がない。
   - `棚にある`: manifest の source digest が一致し、`merge_manifest()` が実質変更なしになり、対象 media の HEAD がローカル SHA-256/size と一致し、library に曲があり、`merge_library()` も実質変更なしになる。
   - `差分あり`: 同じ曲の remote manifest/library/media は存在するが、`棚にある` の条件のいずれかが一致しない。
   - `再生成が必要`: パッケージディレクトリはあるが `.bunri-package.json` がない。
   - 設定破損、到達不能、unsupported schema、remote 文書不正は `確認できません` とし、`未同期` に落とさない。
   - 同じ12桁 song ID に別 full digest がある場合は差分と競合を示し、アップロード不可にする。

4. `src/bunri/pocket/sync.py` の低レベルな remote 収束処理の上に、CLI/Web 共通サービスを実装する。
   - 設定読込、全件探索、legacy 分類、個別・一括 preflight、重複 identity 検出、remote 状態検査、sync 排他、`synchronize()` 呼び出し、バッチ集計、安全なエラー分類を共通化する。
   - CLI 固有の Rich 表示や Web 固有の JSON/HTTP status をサービス層に混ぜず、型付き結果/エラーを各入口で変換する。
   - `_download_files()`、`/api/songs[].downloads`、Web 表示用ファイル一覧を同期判断、preflight、manifest 生成に使わない。
   - remote の未知フィールド、既存の別 target、別曲は削除しない。

5. CLI/Web 間の非ブロッキング sync lock を実装する。
   - `out/.pocket/` 配下の秘密を含まない lock file と `fcntl.flock()` を使い、macOS/Linux/WSL を対象にする。
   - lock file は symlink を追わず、安全な `.pocket` ディレクトリ内だけで開く。PID、URL、token は書かない。
   - 個別 sync は1曲の完了まで、一括 sync は batch 全体の完了まで保持する。
   - 競合時は待たず、CLI は明示エラー、Web は 409 とし、Web ではジョブを作らない。
   - プロセス終了時に OS が lock を解放し、stale lock の手作業削除を不要にするテストを追加する。
   - 対象は sync 同士の排他に限定し、外部 CLI によるパッケージ生成との競合全般は解決しない。

6. `bunri pocket sync --all` を実装する。
   - `src/bunri/pocket/cli.py` の positional `SAFE_NAME` を任意にし、`SAFE_NAME` と `--all` のどちらか一方だけを必須にする。両方指定と両方未指定は利用者向け引数エラーにする。
   - 次の呼び出しをサポートする。

   ```bash
   bunri pocket sync <SAFE_NAME> -o out
   bunri pocket sync --all -o out
   bunri pocket sync --all -o out --no-original
   ```

   - config と lock の確認後、全件探索、legacy 分類、全サイドカー付き候補の preflight、重複 identity 検査を行い、最初の HTTP 書き込み前にローカル検証を完了する。
   - legacy は同期対象外として一覧報告し、有効な曲の同期を続ける。legacy の存在だけでは非0終了にしない。ただし、同期対象のローカル不正または duplicate identity が1件でもあれば、HTTP request 前に全体を停止する。
   - 検証済みパッケージを決定的な順序で逐次同期する。`--no-original` は全曲へ一律適用する。
   - remote エラーが発生したらそこで停止し、曲ごとの結果と完了・失敗・未実行の合計を表示して非0終了する。

7. Web job schema を後方互換な識別型へ拡張する。
   - 旧 JSON に job kind がなければ分離 job として読み込む。
   - Pocket job は分離 job の `target`、`upload`、`package` を無理に流用しない。
   - 既存の単一 worker を使い、分離 job は従来どおり CLI subprocess、Pocket job は共通サービスの Python 関数を直接呼ぶ。
   - Web 内の分離と sync を逐次化し、同じパッケージの更新と読取を同時に行わない。
   - Pocket job の `queued / running / done / error`、経過時間、結果を永続化する。
   - 起動時に `running` のまま残った Pocket job は `queued` へ戻し、冪等に再実行する。既存分離 job の recovery は変えない。

8. batch job、重複防止、削除競合、安全なエラー保存を実装する。
   - 全曲同期を1つの batch job とし、legacy を分類してから同期対象の全 preflight と identity 検査を完了し、その後に最初の HTTP 書き込みを行う。
   - 総件数、完了件数、現在処理中の曲、legacy、完了、失敗、未実行を保存/表示できる構造にする。
   - 1曲で remote エラーが発生したら後続を止め、完了・失敗・未実行を確定する。
   - 同一曲の個別 job、同一 batch の `queued`/`running` job を重複登録しない。
   - 個別同期 job が `queued`/`running` の曲は削除を 409 で拒否する。一括同期中は対象確定との競合を避けるため、曲削除を拒否する。
   - raw exception、HTTP URL、token、Authorization、config 内容を Job JSON、ログ、stderr、Web の error 詳細へ保存/表示せず、固定された安全な利用者向けメッセージへ分類する。

9. Pocket status、個別同期、全曲同期の Web API を追加する。
   - 基本形は `GET /api/pocket/status`、`POST /api/pocket/sync/{song_id}`、`POST /api/pocket/sync` とする。endpoint 名と JSON の細部は確定仕様の範囲で調整してよい。
   - status は `connected`、曲ごとの同期状態/同期可否/再生成案内、ファイルシステム上の全曲同期対象件数を返し、接続先 URL、token、config の実値を返さない。
   - 個別 POST はブラウザから任意の safe name を受けず、song ID からサーバー側で expected full digest とパッケージを解決する。
   - 全曲 POST は Web 一覧外の CLI 生成曲を含む、ファイルシステム上の全パッケージを対象にする。
   - POST は既存 same-origin middleware の対象にする。入力不正、接続なし、再生成必要、重複 job、lock 競合を安全な固定メッセージと適切な 4xx に変換し、lock 競合は 409 とする。
   - 既存 `GET /api/songs` には曲に紐づく最新同期 job の軽量状態だけを追加し、remote 検査を行わない。

10. `src/bunri/web/templates/index.html.j2` に Pocket UI を追加する。
    - Pocket 接続済みの場合だけ、接続先を含まない「Bunri Pocket 連携中」バナーをヘッダ付近へ表示する。
    - 曲一覧見出し付近へ `全曲アップロード` を追加し、Web 一覧外の CLI 生成曲も含むファイルシステム全件が対象であることと件数を明示する。
    - 各曲カードに同期状態バッジと `⬆ アップロード` を追加する。
    - サイドカーなしはボタンを無効にし、「元の入力音源から再生成してください。キャッシュが残っていれば分離処理は省略されます」と案内する。
    - remote 検査中は既存曲一覧を保ったまま `確認中` と表示する。初回表示、タブ復帰、同期完了、明示再読込で状態を更新し、2秒ごとの通常 job poll では remote 検査しない。
    - sync job の badge、経過時間、ポーリング、フォーカス復元は既存の `badgeFor()` 等の考え方を共用する。sync 中は全同期ボタンを無効化して重複送信を防ぐ。
    - `棚にある` 完了後も「プレイヤーを開く」「⬇ ダウンロード」の既存動作を変えない。

11. `README.md` を更新する。
    - Web の連携表示、状態、個別/全曲アップロード、Web 全曲同期がファイルシステム全件を対象とすることを説明する。
    - `bunri pocket sync --all` と `--no-original` の使用例を追記する。
    - サイドカーのない旧パッケージは再生成が必要であること、キャッシュが残っていれば分離を省略できることを説明する。
    - 同時 sync は待機せず拒否されること、再実行で冪等に収束することを説明する。
    - 作業のきっかけとなった特定の利用元や個人的事情、私的リンク、個人環境のパスは書かない。

12. 対象テスト、全テスト、追加検証を実行し、明示的に変更を stage/commit する。
    - まず対象テストを実行する。

      ```bash
      uv run pytest -q -n auto \
        tests/test_cli.py \
        tests/test_pocket_*.py \
        tests/test_web_api.py \
        tests/test_web_jobs.py \
        tests/test_web_jobs_properties.py \
        tests/test_web_page.py
      ```

    - 次に全テストと追加検証を実行する。

      ```bash
      uv run pytest -q -n auto
      uv lock --check
      uv build
      git diff --check
      ```

    - 変更ファイルを確認し、`git add -A` は使わない。実装で変更したファイルをパスで明示して `git add` する。
    - `docs/plans/2026-09-05-pocket-web-sync-all/plan.md` と `docs/plans/2026-09-05-pocket-web-sync-all/request.md` は stage/commit しない。
    - commit message は日本語で、利用者から見た「何を・なぜ」が分かる内容にする。作業事情、依頼元、計画書への参照、私的リンク、個人環境のパスを書かない。
    - commit まで行い、push はしない。

## 完了条件

- [ ] Pocket 未接続ではバナーと同期操作を表示せず、接続情報の実値も返さない。
- [ ] 接続済みでは「連携中」バナーが表示されるが、URL、token、config 内容は HTML、API、ログに現れない。
- [ ] 曲ごとに `棚にある / 未同期 / 差分あり` が `synchronize()` の remote 収束条件と一致して表示される。
- [ ] サイドカーのない旧パッケージは `再生成が必要` となり、HTTP PUT が発生しない。
- [ ] `sync --all` は legacy を全件報告し、有効な曲の同期を続ける。
- [ ] legacy の存在だけでは `sync --all` を非0終了にせず、実際の検証失敗または remote エラーを非0終了にする。
- [ ] Web 個別同期と全曲同期が `queued → running → done/error` で表示される。
- [ ] Web 全曲同期は Web 一覧外の CLI 生成曲を含むファイルシステム全件を対象にし、件数を表示する。
- [ ] Web 同期は共通 `preflight()`/`synchronize()` 経路を直接利用し、常に original MP3 を含める。
- [ ] `_download_files()` または downloads レスポンスを壊しても同期結果に影響しないことをテストで固定する。
- [ ] Web 内の重複 sync、CLI/Web 間の同時 sync は待機せず拒否される。Web の排他競合は 409 で、job を作らない。
- [ ] sync lock は異常終了後に stale 状態を残さない。
- [ ] `bunri pocket sync --all` は対象を決定的な順序で逐次同期する。
- [ ] 同期対象の local preflight または duplicate identity が失敗した場合、最初の HTTP request 前に停止する。
- [ ] remote 途中失敗時は後続を停止して完了・失敗・未実行を集計し、再実行で収束する。
- [ ] remote の未知フィールド、既存の別 target、別曲を削除しない。
- [ ] 旧 Web Job JSON を読み込め、既存分離 job の subprocess/recovery 動作が変わらない。
- [ ] 個別同期中の対象曲削除と、一括同期中の曲削除が 409 で拒否される。
- [ ] status 検査は GET/HEAD のみで PUT しない。
- [ ] token、Authorization、base URL が Web レスポンス、Job JSON、ログ、stderr、エラー詳細に含まれない。
- [ ] 対象テストが通る。
- [ ] `uv run pytest -q -n auto` が通る。
- [ ] `uv lock --check`、`uv build`、`git diff --check` が通る。
- [ ] 作業ブランチに commit 済みであること。commit message は日本語・わかりやすさ重視とし、作業事情、依頼元、計画書への参照、私的リンク、個人環境のパスを書かない。
- [ ] push していないこと。
- [ ] 計画書2ファイルを commit に含めていないこと。

## 未確定事項と判断の委ね方

- 勝手に決めてよい範囲: 既存責務を保つ範囲の関数名、型名、共通サービスのファイル分割、内部データ構造、テスト fixture の構成、Pocket status API の endpoint 名と JSON の細部、負荷と鮮度を損なわない範囲の status 再取得タイミング。
- 守るべき境界: Web API/HTML/job/ログ/エラーに秘密を出さないこと、song ID 指定をサーバー側で full digest へ解決すること、remote status 検査を通常の軽量 job poll から分離すること、Web sync は original を含めること、legacy は報告して有効曲を続行すること、remote エラー時は停止・集計すること、排他競合は非ブロッキングにすること。
- 止まって報告すべき範囲: 新しい依存の追加、許可されたファイル範囲外の変更、既存分離 job/API の破壊的変更、Worker/PWA の変更、remote データ削除、同期の並列化、パッケージ生成競合の全面解決、即時 cancel のための subprocess 方式への変更、バージョン更新・リリース作業。
- direct sync の shutdown は既存の30秒/300秒 timeout と、次回起動時の冪等再実行で扱う。これで安全に実装できず即時 cancel が必要なら、選択肢と推奨を報告して停止する。
- 判断に迷っても人間向け質問 UI は出さない。選択肢、影響、推奨案を報告して停止する。

## 禁止事項

- push しない。commit までとする。
- `git add -A` を使わない。変更ファイルを明示して stage する。
- `docs/plans/2026-09-05-pocket-web-sync-all/plan.md` と `docs/plans/2026-09-05-pocket-web-sync-all/request.md` を commit に含めない。
- commit message に作成経緯、依頼元、計画書への参照を書かない。日本語で利用者向けの「何を・なぜ」だけを書く。
- commit message、コードコメント、README などに、私的なリンクや個人環境のローカルパス、OS ユーザー名、ホスト名を書かない。
- スコープ外のファイルを触らない。
- `_download_files()`、Job JSON の download URL、Web 表示用ファイル一覧を同期入力として使わない。
- サイドカーを推測・手作業生成しない。旧 Job JSON から移行しない。
- Pocket の曲、target、asset、未知フィールドを削除しない。
- 自動同期、常時監視、双方向同期、複数 sync の並列実行を追加しない。
- 新しい HTTP/runtime dependency を追加しない。
- 人間向けの質問 UI を出さない。判断が必要なら、選択肢と推奨を報告に書いて停止する。

## 報告フォーマット

- 変更ファイル一覧
- 実行した対象テスト、全テスト、`uv lock --check`、`uv build`、`git diff --check` と各結果
- 作成した commit の SHA と日本語 commit message
- push していないこと、および計画書2ファイルを commit に含めていないことの確認
- 判断に迷った点・未解決の懸念。なければ「なし」と明記
