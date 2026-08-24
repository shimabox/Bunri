# 実装依頼: 曲一覧への分離済みトラックダウンロードリンク追加

## 背景

Web UIの曲一覧では完了した楽器行から練習プレイヤーを開けるが、生成済みのターゲット単体・バッキング音源を直接保存できない。完了行にmp3/wavのダウンロードリンクを追加し、再生と編集の双方で生成物を利用しやすくする。

## 対象

- リポジトリ: Bunri（このリポジトリのルートで作業する）
- ベースブランチ: `main`（参考。起点は次のSHAとする）
- ベース SHA: `778353b0d3e5a924560d92bff221ebb4a8b128d4`
- 作業ブランチ: `plan/2026-08-24-track-download-links`
- 実装: Sol medium（`codex exec`、`workspace-write`）
- 実装レビュー: Sol high

リポジトリルートで、必ずベースSHAから次のコマンドにより作業ブランチを作成してから着手する。

```bash
git switch -c plan/2026-08-24-track-download-links 778353b0d3e5a924560d92bff221ebb4a8b128d4
```

## 実装仕様

### API契約

`src/bunri/web/app.py`の応答専用処理で`_serialize_job()`の結果へ`downloads`を追加し、`/api/jobs`と`/api/songs`の双方に同じ情報を返す。`downloads`は永続化せず、`src/bunri/web/jobs.py`の`Job` dataclassと`<out>/web/jobs/*.json`は変更しない。

完了ジョブの構造は次の形とする。

```json
{
  "downloads": [
    {
      "track": "target",
      "label": "ギターのみ",
      "files": [
        {
          "format": "mp3",
          "url": "/packages/<encoded-safe>/<encoded-safe>.guitar.mp3",
          "filename": "スピッツ_チェリー_ギターのみ.mp3"
        },
        {
          "format": "wav",
          "url": "/packages/<encoded-safe>/<encoded-safe>.guitar.wav",
          "filename": "スピッツ_チェリー_ギターのみ.wav"
        }
      ]
    },
    {
      "track": "backing",
      "label": "ギターなし",
      "files": [
        {
          "format": "mp3",
          "url": "/packages/<encoded-safe>/<encoded-safe>.guitar.backing.mp3",
          "filename": "スピッツ_チェリー_ギターなし.mp3"
        },
        {
          "format": "wav",
          "url": "/packages/<encoded-safe>/<encoded-safe>.guitar.backing.wav",
          "filename": "スピッツ_チェリー_ギターなし.wav"
        }
      ]
    }
  ]
}
```

- 各トラックエントリは`track`、`label`、`files`を持たせる。
- `files`の各要素は`{format: "mp3"|"wav", url, filename}`を必ず持たせる。`format`はUIがリンクテキストに使うAPI契約であり、UI側でURLやファイル名から拡張子を解析しない。
- 命名規則と表示規則はサーバー側に集約する。
- `downloads`はtarget、backingの順、各`files`はmp3、wavの順とし、4 URLの順序をtarget mp3 → target wav → backing mp3 → backing wavに固定する。
- `status == "done"`かつ`package`がある場合だけ2トラック・計4ファイルを返し、待機中・処理中・失敗を含むそれ以外では必ず`downloads: []`を返す。
- URLのディレクトリ名と音声ファイルのベース名は、検証済み`job.package`の先頭セグメントから取得する。現在の`safe_filename(job.title)`で実体名を再計算しない。
- 保存ファイル名の曲名部分は`safe_filename(job.title)`から作る。旧sanitizerによるパッケージ実体名と現在の保存名が異なるケースでも、URLは既存実体、`filename`は現在のsafe化済みタイトルを使用する。
- ターゲットラベルは`REGISTRY[job.target].label_ja`を使い、`<label_ja>のみ`と`<label_ja>なし`を作る。現行レジストリ外の旧・未知ターゲットは、既存の`target_label`と同様にターゲットキーへフォールバックする。
- URL全体は既存`package_url`と同じ`quote()`方針でパーセントエンコードし、`/`の階層だけ維持する。日本語、空白、旧形式の`#`・`%`等を安全に扱う。
- `package_url`と既存の`target_label`は後方互換のため維持する。
- `original.mp3`は`downloads`に含めない。
- ファイルの`exists()`確認は追加しない。完了ジョブには常に4リンクを返し、欠損時は既存`StaticFiles`の404に任せる。

### UI仕様

`src/bunri/web/templates/index.html.j2`の各ターゲット表示を、ヘッダー行とダウンロード行を持つブロックとしてレンダリングする。

- ヘッダー行には楽器名、状態バッジ、完了時の「プレイヤーを開く」を置く。
- `job.status === "done"`かつ`Array.isArray(job.downloads) && job.downloads.length > 0`の場合だけ、各楽器ブロックの直下にダウンロード行を生成する。
- 表示は`ギターのみ: mp3 wav`、`ギターなし: mp3 wav`の形式とする。
- APIの`url`を`href`、`filename`を`download`属性、`format`をリンクテキストに設定する。
- `aria-label`は`<日本語トラック名>を<format>でダウンロード`とする。例は`ギターのみをmp3でダウンロード`。
- DOM生成は`textContent`とプロパティ設定を使い、タイトルやラベルをHTML文字列として挿入しない。
- 複数楽器では、各楽器ブロックに対応する4リンクを混線させず表示する。
- 狭い画面では折り返せるflexレイアウトとし、既存カードの幅を押し広げない。
- 既存の折りたたみ、ポーリング、フォーカス復元、エラー詳細、削除UI、プレイヤーリンクのURLと別タブ表示を維持する。

### 変更範囲

変更対象は原則として次の4ファイルに限定する。

- `src/bunri/web/app.py`
- `src/bunri/web/templates/index.html.j2`
- `tests/test_web_api.py`
- `tests/test_web_page.py`

次のファイルは変更しない。

- `src/bunri/package.py`
- `src/bunri/web/jobs.py`
- `src/bunri/templates/player.html.j2`
- `tests/test_package.py`

パッケージ出力構造、サーバー上の既存ファイル、プレイヤー、永続化スキーマ、配信ルートは変更しない。ダウンロード専用API、`Content-Disposition`付きレスポンス、マイグレーション、既存ファイルのリネーム・コピー、README変更、新規lint依存は追加しない。

## タスク（この順で）

1. `src/bunri/web/app.py`に、ターゲットラベル、音声URL、保存ファイル名、`downloads`配列を組み立てる応答専用ヘルパーを追加する。
2. `_serialize_job()`へ`downloads`を追加する。完了時はtarget/backing × mp3/wavを契約どおりの順序で返し、それ以外は空配列を返す。`/api/jobs`と`/api/songs`の直列化規則を一箇所に保つ。
3. `tests/test_web_api.py`のfake runnerに4音声ファイルを生成させ、API契約を検証する。少なくとも次をテストで固定する。
   - `format`、`url`、`filename`の値と構造
   - 4 URLの順序（target mp3 → target wav → backing mp3 → backing wav）
   - 日本語、空白、`#`、`%`等を含むURLのエンコード
   - 旧sanitizerによるパッケージ実体名と現在の保存名が異なるケースで、URLと保存名がそれぞれ正しいこと
   - 非完了・失敗時に`downloads: []`であること
   - `original.mp3`が応答に現れないこと
   - 永続化Job JSONに`downloads`フィールドがないこと
   - 4ファイル生成後に1ファイルを削除し、一覧APIは成功し、削除したファイルのURLへのアクセスだけが404になること
4. `src/bunri/web/templates/index.html.j2`にターゲット単位のダウンロード行とスタイルを追加し、APIから受け取った`label`、`format`、`url`、`filename`だけでリンクを作る。
5. `tests/test_web_page.py`のfake runnerに4音声ファイルを生成させ、実ブラウザで少なくとも次を検証する。
   - 完了前にはリンクがなく、完了後には4リンクが現れること
   - 4リンクの順序とmp3/wavのリンクテキスト
   - 正しい`href`、`download`属性、`aria-label`の値
   - `original.mp3`がDOMに現れないこと
   - 複数ターゲットでリンクが各楽器ブロックに正しく対応すること
6. 複数ターゲット、エラー表示、折りたたみ、ポーリング、フォーカス復元、削除UI、プレイヤーリンクの既存テストを通す。必要な場合だけ、セレクタをターゲットブロック単位へ調整する。
7. Playwright Chromiumを導入した環境で対象テストと全テストを実行し、ブラウザテストをskipさせずに検収する。
8. 変更対象だけを明示的にstageし、日本語の分かりやすいcommit messageでcommitする。`docs/plans/`配下はcommitに含めない。

## テスト・検証

依存関係をCI相当で準備する場合は次を実行する。

```bash
uv sync --frozen --extra web
uv run playwright install --with-deps chromium
```

対象テスト:

```bash
uv run pytest -q tests/test_web_api.py tests/test_web_page.py
```

全テスト:

```bash
uv run pytest -q
```

リポジトリに専用lint/formatコマンドは定義されていないため、新規ツールを導入しない。テストに加えて`git diff --check`で空白エラーを確認する。

## 完了条件

- [ ] 完了した楽器行だけに、楽器のみmp3/wavと楽器なしmp3/wavの計4リンクが表示される。
- [ ] 待機中・処理中・失敗ジョブではAPIが`downloads: []`を返し、UIにリンクが表示されない。
- [ ] `job.downloads`が空配列の場合、完了ジョブであってもダウンロード行の要素自体が生成されないことを実ブラウザテストで検証する。
- [ ] `downloads[].files[]`が`format`、`url`、`filename`を持ち、UIは`format`を表示に使用する。
- [ ] APIとDOMで4 URLがtarget mp3 → target wav → backing mp3 → backing wavの順になる。
- [ ] `original.mp3`がAPI応答にもDOMにも現れない。
- [ ] 旧パッケージ実体名と現在の保存名が異なるケースで、`href`は既存実体、`download`属性は現在のsafe化済みタイトルを正しく使う。
- [ ] URLが日本語・空白・特殊文字を安全にエンコードし、`download`属性が`<safe曲名>_<日本語トラック名>.<拡張子>`となる。
- [ ] 全リンクの`download`属性と`aria-label`が仕様どおりである。
- [ ] 複数楽器でもリンクが混線せず、既存UIとプレイヤーリンクの挙動が維持される。
- [ ] 4ファイル生成後に1ファイルを削除する明示テストで、一覧APIが成功し、削除したファイルのURLへのアクセスだけが404になることを固定する。
- [ ] 永続化Job JSONに`downloads`フィールドがない。
- [ ] `uv run pytest -q tests/test_web_api.py tests/test_web_page.py`がパスする。
- [ ] Playwright Chromium導入済み環境で`uv run pytest -q`がブラウザテストをskipせずパスする。
- [ ] `git diff --check`がパスする。
- [ ] 変更が作業ブランチにcommit済みである。
- [ ] `docs/plans/`配下がcommitに含まれていない。

## commitルール

- commit messageは日本語で、何を変更し、利用者にどのような効果があるかを分かりやすく表す。
- `type(scope):`形式の接頭辞は使用してよい。
- 作成経緯、依頼元、特定の利用事情、計画書への参照はcommit messageに書かない。
- `git add -A`は使わない。変更した実装・テストファイルをパスで明示してstageする。
- `docs/plans/`配下はstageもcommitもしない。
- pushしない。commitまでで止める。

## 未確定事項と判断の委ね方

- 勝手に決めてよい範囲: 応答専用ヘルパーの名前と分割、既存設計に沿ったテストfixtureの細部、既存`.sw-open-link`とカードに合わせたリンク間隔・色・枠線、狭い画面での折り返し方法。
- 固定済みで変更しない範囲: API構造、`format`契約、4 URLの順序、ラベル・URL・保存名の生成元、非完了時の空配列、original除外、永続化スキーマ非変更、ファイル存在確認を行わない方針。
- 止まって報告すべき範囲: 変更対象4ファイルを越える変更、依存追加、パッケージ構造や既存ファイルの変更、既存UI挙動や後方互換性の破壊、欠損ファイル向けの別UIや別APIが必要になる場合。
- 判断が必要になっても人間向けの質問UIは出さない。選択肢、影響、推奨案を報告に書いて停止する。

## 禁止事項

- pushしない（commitまで）。
- `git add -A`を使わない。
- `docs/plans/`配下をcommitに含めない。
- commit messageやコードコメント、ドキュメントに作成経緯・依頼元・特定の利用事情を書かない。
- スコープ外のファイルを変更しない。
- パッケージ出力、永続化スキーマ、既存ファイル名、配信ルートを変更しない。
- 未定義のlint/formatツールや新規依存を導入しない。
- 人間向けの質問UIを出さない。

## 報告フォーマット

- 作成・変更ファイル一覧
- 実行したテスト・lint相当の検証とその結果（ブラウザテストのskip有無を含む）
- commit SHAとcommit message
- 判断に迷った点・未解決の懸念（なければ「なし」）
