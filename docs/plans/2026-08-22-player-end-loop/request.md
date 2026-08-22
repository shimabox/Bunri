# 実装依頼: 曲終端で停止する A-B ループの修正

## 背景

B 点が曲の終端と一致する A-B ループでは、1 周目に A 点へ戻っても実際の `<audio>` が停止したままとなり、論理状態や再生ボタン表示と食い違う。終端固有の `ended` 状態で再生を再開し、issue #4 の A=0、B=曲の終端という条件で 2 周以上ループを継続させる。

## 対象

- リポジトリ: `shimabox/Bunri`
- リポジトリルート: `.`（以下のファイルパスはすべてここからの相対パス）
- 対象 issue: [#4](https://github.com/shimabox/Bunri/issues/4)
- ベースブランチ: `main`（参考。起点は下記 SHA）
- ベース SHA: `cdc0588533202c4f69757be5e6db14a3eddaa98e`（**この SHA から**次のコマンドで作業ブランチを作り、ブランチ名からは作らない）
- 作業ブランチ: `plan/2026-08-22-player-end-loop`
- ブランチ作成コマンド: `git switch -c plan/2026-08-22-player-end-loop cdc0588533202c4f69757be5e6db14a3eddaa98e`
- 実装担当: Sol medium（codex exec）
- 実装レビュー: Sol high
- 実装対象: `src/bunri/templates/player.html.j2`、`tests/test_player_html.py`

## タスク(この順で)

1. 指定のブランチ作成コマンドを実行し、`src/bunri/templates/player.html.j2` の `handleLoop()`、`tick()`、`playAll()`、`pauseAll()`、`ended` ハンドラと、`tests/test_player_html.py` の Playwright テスト・ヘルパーを確認する。
2. `tests/test_player_html.py` に回帰テストを先に追加する。既存の `_needs_runtime`、`_render_dir()`、`_open()` を再利用し、`_render_dir()` で約 1 秒の無音 MP3 を一時生成する。原曲トラックだけを作成し、リポジトリへ音声バイナリを追加しない。
3. 回帰テストでは、ページの準備完了後に A 点を 0 に設定し、B 未設定のまま終端直前から再生して自然終了させる。DOM の `#tm-audio-original` と `window.__player.state()` を用い、ループなしでは実音声が paused、`playing` が false、再生ボタン `#tm-play` が「再生」になる既存挙動を確認する。
4. 自然終了時の active `<audio>.currentTime` で B 点を設定し、A=0、ループが active、B と `window.__player.state().duration` が許容差内で一致することを確認する。固定の duration 値は使わない。
5. 再び終端直前へ seek して再生し、次を直接検証する。

   - 1 回目に A 点へ巻き戻った後、active `<audio>.paused === false` である。
   - 巻き戻し後に active `<audio>.currentTime` が A 点より先へ再び増加する。
   - その後さらに A 点への巻き戻しが起き、少なくとも 2 周継続する。
   - ループ中は `window.__player.state().playing === true` で、`#tm-play` の表示が「一時停止」のままである。

   `state().playing` だけでは今回の不具合を検出できないため、DOM 上の実音声要素の `paused` と `currentTime` の検証を省略しない。メディア時刻の揺らぎを考慮し、狭すぎない許容差と `wait_for_function` などの条件待機を使う。
6. 実装変更前に追加テストを実行し、A 点へ戻った後に実音声が再開しないという想定原因で失敗することを確認する。Chromium または ffmpeg がないため skip された場合は失敗確認と見なさず、下記の環境準備または Docker 経路を利用する。
7. `src/bunri/templates/player.html.j2` の `ended` ハンドラを次の確定方針で修正する。active トラック以外のイベントは無視し、ループ中は `seekAll(loopA)` の後に `playAll()`、ループなしは `pauseAll()` とする。

   ```js
   el.addEventListener("ended", function () {
     if (name !== activeName) return;
     if (loopActive()) {
       seekAll(loopA);
       playAll();
     } else {
       pauseAll();
     }
   });
   ```

   `handleLoop()` 内で `el.paused` を見て `playAll()` を呼ぶ案へ変更しない。`tick()` が `rafId = null` にした直後に `playAll()` の `startTick()` が走ると、`tick()` 末尾と合わせて次の animation frame を二重予約する可能性がある。既存の `seekAll()`、`playAll()`、`pauseAll()` を利用し、追加状態や `window.__player` の公開 API は増やさない。
8. 追加テスト、`tests/test_player_html.py` 全体、全テストの順に実行する。ブラウザテストは `-rs` の結果を見て、追加テストが skip されず成功したことを確認する。

   ```bash
   uv run pytest -q tests/test_player_html.py -rs
   uv run pytest -q
   ```

   Chromium 未導入時は、既存依存の範囲で次を実行する。

   ```bash
   uv sync --frozen --extra web
   uv run playwright install --with-deps chromium
   ```

   ローカルで Chromium と ffmpeg を用意できない場合は、次の Docker 経路で少なくとも対象ファイルのブラウザテストを実行する。

   ```bash
   docker build --target dev -t bunri:dev .
   docker run --rm bunri:dev -q tests/test_player_html.py
   ```

9. 差分が `src/bunri/templates/player.html.j2` と `tests/test_player_html.py` の必要最小限の変更だけであることを確認する。専用 lint コマンド・設定はないため、新たに追加しない。
10. `git add -A` は使わず、`git add src/bunri/templates/player.html.j2 tests/test_player_html.py` のように実装変更ファイルを明示してステージし、作業ブランチへ commit する。`docs/plans/` 配下の `plan.md` と `request.md` はステージも commit もしない。push はしない。
11. 変更ファイル、テスト結果、commit、判断事項を報告し、Sol high による実装レビューへ引き渡す。

## 完了条件

- [ ] issue #4 の再現手順どおり A 点を 0、B 点をブラウザの報告する `duration` と一致させられる。
- [ ] 1 周目の終端で A 点へ戻った後、active `<audio>.paused === false` となる。
- [ ] A 点への巻き戻し後に active `<audio>.currentTime` が再び増加する。
- [ ] 少なくとも 2 回の A 点への巻き戻しを観測でき、1 回で停止しない。
- [ ] ループ中は `window.__player.state().playing === true` と再生ボタンの「一時停止」表示を維持する。
- [ ] ループなしの自然終了では、実音声、論理状態、再生ボタン表示が従来どおり停止状態になる。
- [ ] 終端より手前の B 点を使う既存の A-B ループテストが成功する。
- [ ] 音声は一時生成され、バイナリが commit されていない。
- [ ] Playwright の追加テストが skip されず成功する。
- [ ] `uv run pytest -q tests/test_player_html.py -rs` が成功する。
- [ ] `uv run pytest -q` が成功する。
- [ ] 新規依存、CI、Dockerfile、lint 設定の変更がない。
- [ ] 実装変更が `plan/2026-08-22-player-end-loop` に commit 済みである。
- [ ] `docs/plans/` 配下の `plan.md` と `request.md` が commit に含まれていない。
- [ ] push されていない。

## 未確定事項と判断の委ね方

- 勝手に決めてよい範囲: 回帰テスト名、テスト内の補助変数、メディア時刻の許容差、タイムアウト、ポーリングまたはテスト内だけのイベント記録方法。いずれも 2 周以上の継続と DOM の `paused` / `currentTime` を安定して直接検証できる範囲に限る。
- 止まって報告すべき範囲: `ended` ハンドラ以外の方式への変更、`handleLoop()`・`tick()`・`playAll()` の状態管理変更、公開 API や依存の追加、CI・Dockerfile の変更、対象 2 ファイル以外への実装変更、既存の通常ループ・ループなし終端・トラック同期を壊す変更、Chromium 以外までの対応拡大。

## 禁止事項

- push しない。作業ブランチへの commit までとする。
- `git add -A` を使わない。変更ファイルを明示して add する。
- `docs/plans/` 配下の `plan.md` と `request.md` を commit に含めない。
- `handleLoop()` 内から `playAll()` を呼ぶ代替案へ変更しない。変更が必要なら実装を止めて報告する。
- `window.__player` の公開 API・状態項目を追加しない。
- 音声バイナリ、新規依存、lint 設定を追加しない。
- スコープ外のファイルを変更せず、再生状態管理全体をリファクタリングしない。

## 報告フォーマット

- 変更ファイル一覧
- 実行したテスト・lint とその結果（Playwright テストが skip されていないことを含む）
- commit SHA と commit メッセージ
- push していないこと、および `docs/plans/` 配下を commit に含めていないことの確認
- 判断に迷った点・未解決の懸念
