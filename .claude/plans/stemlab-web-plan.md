# StemLab Web ページ 実装プラン

2026-07-12 起草。ユーザー要望+確認済み事項:

- 音声ファイルの選択アップロード / ドラッグ&ドロップ
- アップロードされたら分離を実行。実行中であることが分かる(ポーリング)
- **ブラウザを閉じてもジョブは継続**する
- 完了したら練習用プレイヤーへのリンクが現れ、**次回訪問時もリンクが残る**
- **動作環境: ローカル専用**(自分の Mac、localhost、MPS 高速経路。認証不要・
  音源が手元を出ないので tab-maker ISSUES の著作権懸念は回避)
- **UI は guitar 固定**でシンプルに(--target 対応は将来の拡張余地として設計だけ確保)
- 役割分担: 実装 = sonnet(仕様確定済みの定型)、ジョブ状態機械の敵対的レビュー =
  opus、計画・契約・受入・E2E 確認 = Fable

## 全体構成

```
ブラウザ ──(upload / poll / 静的取得)── FastAPI (127.0.0.1:8b--)
                                          │
                                          ├─ ジョブストア(JSON ファイル、DB レス)
                                          ├─ ワーカースレッド(逐次キュー)
                                          │    └─ サブプロセスで `stemlab` CLI 実行
                                          └─ StaticFiles(out/ を配信 → player.html)
```

### 設計判断とその理由

1. **サーバー = FastAPI + uvicorn、フロント = ビルドなしの単一 HTML(インライン JS/CSS)**
   - リポジトリの流儀(プレイヤーと同じ自己完結 HTML、追加ツールチェーンなし)
   - 追加依存: `fastapi` / `uvicorn` / `python-multipart` のみ(web extra に隔離:
     `[project.optional-dependencies] web = [...]` — CLI だけ使う人に影響させない)
2. **分離ジョブはサブプロセスで既存 CLI を実行**(`stemlab <upload> -o <out> --title <t>`)
   - in-process 実行(build_package 直呼び)と比較して:
     クラッシュ隔離 / ジョブごとにメモリが完全に返る(長期常駐サーバーで torch の
     メモリ成長を心配しなくてよい)/ テスト済みの CLI 経路をそのまま再利用 /
     Web 層が torch を import しない(サーバー起動が軽い)
   - 進捗は粗粒度(queued → running → done/error)+ 経過秒数 + CLI の stdout を
     ジョブログファイルに保存(UI は状態バッジと経過時間を表示。分離の内部進捗%
     までは出さない — MPS で1曲数分なので割り切る)
3. **ジョブ永続化はファイルシステムのみ(DB レス)**
   - `out/web/jobs/<job_id>.json` に状態を保存(書き込みは tmp → rename の
     アトミック置換)
   - 「ブラウザを閉じても有効」= サーバープロセスが担う(ジョブはサーバー側)
   - 「サーバーを再起動しても壊れない」も担保: 起動時に jobs/ をスキャンし、
     `running` のまま残っているジョブ(前回クラッシュ)は `queued` に戻して
     再実行。分離キャッシュ(out/.cache)が効くので再実行コストは最小
   - 「リンクが次回も残る」= ジョブ一覧 + パッケージディレクトリのスキャンで復元
4. **重複対応**: アップロード時に sha1 digest を計算し、`uploads/<digest><拡張子>`
   に保存(同じ曲の再アップロードはファイル・キャッシュとも自然に共有)。
   同一 digest+target の完了ジョブが既にあれば新規実行せず既存リンクを即返す
5. **逐次実行**(ワーカー 1 本): MPS メモリと発熱を考えると並列分離は益なし。
   キュー順に 1 曲ずつ
6. **バインドは 127.0.0.1 固定**(ローカル専用の明示)。アップロードはサイズ上限
   (既定 200MB)と拡張子チェック(mp3/wav/m4a/flac/ogg)

## 画面仕様(1 ページのみ)

- ヘッダ: StemLab / 「音源からギター練習パッケージを作る」
- ドロップゾーン: ドラッグ&ドロップ or クリックでファイル選択。
  受付時に曲名(既定 = ファイル名)を確認できる小さな入力欄
- ジョブ一覧(新しい順):
  - `待機中` / `処理中(経過 mm:ss)` / `完了` / `失敗(理由)` のバッジ
  - 完了行: **「プレイヤーを開く」リンク**(`/packages/<曲名>/<曲名>.guitar.player.html`)
  - 失敗行: ログ末尾を折りたたみ表示
- ポーリング: アクティブなジョブがある間だけ 2 秒間隔で `GET /api/jobs`、
  なければ停止(タブ復帰時に再開)。ページ再訪時は一覧 API で全復元
- 文言は平易な日本語。UI はプレイヤー同様ミニマルに(装飾より分かりやすさ)

## API

| メソッド/パス | 内容 |
|---|---|
| `GET /` | ページ本体(テンプレート 1 枚) |
| `POST /api/jobs` | multipart アップロード(file, title 任意)→ `{job_id}`。既存完了ジョブと同一内容なら `{job_id, dedup: true}` |
| `GET /api/jobs` | 全ジョブ+状態+完了時は player への相対 URL |
| `GET /api/jobs/{id}` | 単一ジョブ状態(将来用。一覧ポーリングだけでも足りる) |
| `GET /packages/…` | out/ 配下の静的配信(プレイヤーと音声。ディレクトリリスティングなし) |

ジョブ JSON スキーマ:
```json
{"id": "j-<ulid風>", "digest": "…", "title": "斜陽", "target": "guitar",
 "status": "queued|running|done|error", "created_at": "…", "started_at": null,
 "finished_at": null, "error": null, "package": "斜陽/斜陽.guitar.player.html",
 "log": "web/logs/j-….log"}
```

## ファイル構成(新規)

```
src/stemlab/web/__init__.py
src/stemlab/web/app.py        # FastAPI ルート+静的配信(~120行)
src/stemlab/web/jobs.py       # ジョブストア(JSON 読み書き・状態遷移)+ワーカー(~150行)
src/stemlab/web/templates/index.html.j2  # 画面(インライン JS/CSS、~250行)
src/stemlab/web/cli.py        # `stemlab-web` エントリポイント(typer、--port/--out)
tests/test_web_jobs.py        # ストア/ワーカー(ランナーをフェイクに差し替え)
tests/test_web_api.py         # FastAPI TestClient(upload→状態遷移→一覧→dedup)
tests/test_web_page.py        # Playwright: D&D 配線・ポーリング・リンク表示(file:// でなく http)
```

- エントリポイントは **`stemlab-web` を別スクリプトとして追加**
  (`[project.scripts]`)。既存 `stemlab` CLI を typer サブコマンド化すると
  `stemlab song.mp3` の形が壊れるため、触らない
- `package.py` / `separate.py` 等のコアは**一切変更しない**(サブプロセス経由)

## 実装フェーズと契約

| 順 | 内容 | 担当 | ゲート |
|---|---|---|---|
| W1 | jobs.py + app.py + cli.py + API テスト(ランナーはフェイク) | sonnet | テスト通過 + Fable レビュー |
| W2 | index.html.j2(D&D・ポーリング・一覧)+ ブラウザテスト | sonnet | テスト通過 + Fable レビュー |
| W3 | ジョブ状態機械の敵対的レビュー(再起動復元・アトミック性・同時アップロード・パス正当性) | opus | 指摘の修正確認 |
| W4 | 実 E2E: サーバー起動 → 斜陽をブラウザからアップロード → 完了 → プレイヤー再生(Fable が chrome-devtools で確認)→ **ユーザー試用** | Fable+ユーザー | 試用 OK で commit |

## リスクと対応

- **サブプロセスの死活**: exit code 非 0 / タイムアウト(既定 2 時間)で `error` に
  遷移しログ末尾を保存。ワーカーは次のジョブへ進む
- **同名タイトルの衝突**: 既存 `_safe_filename` と同じ規則でフォルダ名化。同名でも
  digest が違えば別ジョブとして受け付け、パッケージは上書きでなく
  `<title>-2` のような連番回避(W1 で仕様確定)
- **アップロード中の切断**: 一時ファイルに書いて完了後 rename(既存の
  _download_if_missing と同じ規律)
- **プレイヤーの http 配信**: プレイヤーは file:// 前提で作ってあるが、http では
  制約が緩くなる方向なのでそのまま動く(相対 src のまま)。既存 Playwright
  テストが file:// を担保し続ける
- **将来拡張の置き場**: target 選択 UI / 削除ボタン / LAN 公開(bind オプション)は
  意図的に v1 から除外。ジョブ JSON に `target` を最初から持たせて拡張余地だけ確保

## 見積もり

コード ~520 行 + テスト ~300 行。実装は W1/W2 で sonnet 2 契約(または 1 契約に
まとめる)、私のレビュー・E2E を挟んで半日想定。
