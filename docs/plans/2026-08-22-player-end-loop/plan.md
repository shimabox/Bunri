# 曲終端で停止する A-B ループの修正 実装計画

- 日付: 2026-08-22
- ブランチ: `plan/2026-08-22-player-end-loop`
- 実装担当(駒): Sol medium（codex exec。局所的な JavaScript 修正と Playwright 回帰テストの実装に必要な精度と速度のバランスを取る）
- 実装レビュー: Sol high（採用方針からの逸脱、イベント順序、回帰テストの有効性を重点確認する）
- 対象 issue: [shimabox/Bunri#4](https://github.com/shimabox/Bunri/issues/4)

## 背景・目的

プレイヤーで B 点を曲の終端に設定すると、1 周目の終端で A 点へ戻った後も実際の `<audio>` は停止したままになり、論理状態の `isPlaying` や再生ボタン表示と実再生状態が食い違う。ブラウザがメディアを停止させる終端固有の `ended` 状態を処理し、A=0、B=曲の終端という issue #4 の再現条件でも A-B ループが 2 周以上継続するようにする。

## スコープ

### やること

- `src/bunri/templates/player.html.j2` の active トラックの `ended` 処理を修正する。
- `tests/test_player_html.py` に、B 点がブラウザの報告する実メディア長と一致するケースの Playwright 回帰テストを追加する。
- A 点へ戻った後に active `<audio>` が再開し、時刻が再び進み、少なくとも 2 回の巻き戻しが発生することを検証する。
- ループなしの終端処理と、B 点が終端より手前にある既存 A-B ループの挙動を維持する。

### やらないこと

- A-B ループ以外の再生、速度、トラック切替仕様を変更しない。
- `window.__player` の公開 API や状態項目を追加しない。
- `tick()`、`playAll()`、再生状態管理全体をリファクタリングしない。
- Playwright、ffmpeg、その他の依存関係を追加しない。
- 音声バイナリをリポジトリへ追加しない。
- CI、Dockerfile、lint 設定を変更しない。
- Chromium 以外のブラウザ対応を追加しない。

## 方針

原因は、終端到達時にブラウザが `<audio>` を `paused` にした後も論理状態の `isPlaying` が `true` のまま残り、現在の `handleLoop()` が `seekAll(loopA)` だけを行うため、時刻だけ A 点へ戻って再生が再開されないことにある。

既存の `ended` ハンドラで active トラックの終端固有処理を分岐し、ループ中なら `seekAll(loopA)` の後に `playAll()`、ループなしなら従来どおり `pauseAll()` を実行する。

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

通常の B 点では従来どおり `handleLoop()` が巻き戻し、ブラウザが停止させた曲終端の場合だけ `ended` ハンドラが巻き戻しと再開を担当する。既存の `seekAll()`、`playAll()`、`pauseAll()` を利用し、追加状態は持たない。

`handleLoop()` 内で `el.paused` を検査して `playAll()` を呼ぶ代替案は採用しない。`handleLoop()` は `tick()` 内で `rafId` が `null` にされた直後に呼ばれるため、`playAll()` 内の `startTick()` と `tick()` 末尾の双方が次の animation frame を予約し、二重の rAF ループを作る可能性がある。採用案から変更するには再生状態管理まで検討範囲が広がるため、実装を止めて報告する。

Playwright テストは既存の `_needs_runtime`、`_render_dir()`、`_open()` を再利用する。`_render_dir()` で約 1 秒の無音 MP3 を一時生成し、原曲トラックだけを作成してイベント順序を単純化する。A 点を 0 に設定した後、ループなしで自然終了させ、終了時の `currentTime` を B 点として設定し、B と `state().duration` の一致を許容差付きで確認する。再度終端直前から再生し、DOM 上の active `<audio>` の `paused` と `currentTime` を直接観測して、1 回目の巻き戻し後に再生と時刻進行が続き、さらに 2 回目の巻き戻しが起きることを確認する。`state().playing` だけでは不具合を検出できないため、実音声要素の検証を必須とする。

## タスク分解

| # | タスク | 依存 |
|---|---|---|
| 1 | ベース SHA から作業ブランチを作成し、対象コードと既存テストを確認する | - |
| 2 | `tests/test_player_html.py` に、終端到達後に B 点を設定する Playwright 回帰テストを追加する | 1 |
| 3 | 現行コードで追加テストを実行し、A 点へ戻った後に実音声の時刻が進まず失敗することを確認する | 2 |
| 4 | `src/bunri/templates/player.html.j2` の `ended` ハンドラを、ループ中は `seekAll(loopA)` と `playAll()`、非ループ時は `pauseAll()` とする | 3 |
| 5 | 追加テストを実行し、skip ではなく成功したことを確認する | 4 |
| 6 | `tests/test_player_html.py` 全体を実行し、通常の B 点でのループ、トラック同期、再生ボタンなどの既存挙動を確認する | 5 |
| 7 | 全テストを実行し、新規依存やスコープ外変更がないことを確認する | 6 |
| 8 | 実装変更だけを明示的にステージして作業ブランチへ commit する。`docs/plans/` 配下は commit に含めない | 7 |
| 9 | Sol high が差分、テスト結果、採用方針への適合をレビューする | 8 |

## 完了条件・受け入れ基準

- [ ] issue #4 の再現手順どおり、A 点が 0、B 点がブラウザの報告する `duration` と一致するループを設定できる。
- [ ] 1 周目の終端で A 点へ戻った後、active `<audio>` が `paused === false` になる。
- [ ] A 点へ戻った後に active `<audio>` の `currentTime` が再び増加する。
- [ ] 少なくとも 2 回の A 点への巻き戻しが観測でき、1 回で停止しない。
- [ ] ループ中は `window.__player.state().playing === true` と再生ボタンの「一時停止」表示が維持される。
- [ ] ループなしで終端に達した場合は、従来どおり実音声、論理状態、再生ボタン表示が停止状態になる。
- [ ] 終端より手前の B 点を使う既存の A-B ループテストが成功する。
- [ ] 音声ファイルはテスト実行時に一時生成され、バイナリが commit されない。
- [ ] Playwright の追加テストが skip されず成功する。
- [ ] `uv run pytest -q tests/test_player_html.py -rs` が成功する。
- [ ] `uv run pytest -q` が成功する。
- [ ] 新規依存、CI、Dockerfile、lint 設定の変更がない。
- [ ] 実装変更が作業ブランチに commit 済みで、push されていない。
- [ ] `docs/plans/` 配下の `plan.md` と `request.md` が commit に含まれていない。

## 未確定事項・リスクと判断の委ね方

| 項目 | 内容 | 実装時の扱い |
|---|---|---|
| ブラウザテストの skip | Chromium または ffmpeg がない環境では既存マーカーにより skip され、コマンド自体は成功し得る | `-rs` で結果を確認し、必ず Chromium と ffmpeg がある環境または記載の Docker 経路で skip なしの成功を確認する |
| メディア時刻の揺らぎ | MP3 の実 duration、イベント発火、rAF の観測時刻には小さな差がある | B は固定値でなく `state().duration` と自然終了時の `currentTime` から設定する。許容差、待機時間、観測方法はフレークを避ける範囲で実装者が調整してよい |
| 巻き戻し回数の観測 | 短い音声ではポーリング間隔次第で A 点付近の瞬間を取り逃がす可能性がある | DOM の `currentTime` を継続観測し、必要ならテスト内だけのイベント記録を用いる。プロダクション API や状態は追加しない |
| rAF の二重起動 | `handleLoop()` から現在の `playAll()` を呼ぶと frame が重複予約される可能性がある | `ended` ハンドラ案を必ず採用する。別案が必要なら実装を止め、状態管理を含む変更範囲を報告する |
| Chromium 以外 | 現行 CI とテスト基盤は Chromium のみ | 今回の受け入れ対象は Chromium とする。他ブラウザへの対応が必要ならスコープを広げず報告する |
| 既存挙動への影響 | 複数トラックの `ended` や通常の B 点、ループなし終端に回帰が起こり得る | active トラック以外の `ended` は無視し、対象テストと全テストで確認する。追加修正がスコープ外へ及ぶ場合は止まって報告する |
