# 曲一覧への分離済みトラックダウンロードリンク追加 実装計画

- 日付: 2026-08-24
- ブランチ: `plan/2026-08-24-track-download-links`
- 実装担当（駒）: Sol medium（`codex exec`、`workspace-write`）。API・UI・テストの4ファイルに限定された実装を、既存の命名規則と互換性を保ちながら進める。
- 実装レビュー: Sol high。API契約、旧データ互換性、DOM属性、永続化境界を重点的に確認する。

## 環境情報

- ベースブランチ: `main`
- ベース SHA: `778353b0d3e5a924560d92bff221ebb4a8b128d4`
- 対象テスト: `uv run pytest -q tests/test_web_api.py tests/test_web_page.py`
- 全テスト: `uv run pytest -q`（`make test` でも実行可能）
- CI相当の準備・実行:
  - `uv sync --frozen --extra web`
  - `uv run playwright install --with-deps chromium`
  - `uv run pytest -q`
- lint: `Makefile`、`pyproject.toml`、CIに専用lint/formatコマンドの定義はない。未定義の`ruff`等は導入しない。

## 背景・目的

現在のトップページでは、完了した楽器行から練習プレイヤーを開けるが、生成済みのターゲット単体・バッキング音源を直接保存する導線がない。完了行にmp3/wavのダウンロードリンクを追加し、再生用途とDAW等での編集用途の双方へ直接アクセスできるようにする。

サーバー上のパッケージ構造やファイル名は変更せず、同一オリジンの`<a download>`により保存時だけ日本語ファイル名を付与する。

## スコープ

### やること

- トップページの各楽器行に、完了ジョブだけダウンロード欄を表示する。
- 各ジョブについて次の4リンクを提供する。
  - ターゲット楽器のみ: mp3 / wav
  - ターゲット楽器なし（backing）: mp3 / wav
- `REGISTRY[target].label_ja`を使い、プレイヤーと同じ「ギターのみ」「ギターなし」形式のラベルにする。
- API応答時に、表示ラベル、音声形式、配信URL、`download`属性用ファイル名をサーバー側で組み立てる。
- URL中の日本語、空白、旧形式の`#`・`%`等を既存の`quote()`方針でパーセントエンコードする。
- APIテストと実ブラウザテストを追加・更新する。

### やらないこと

- `original.mp3`のダウンロードリンク追加。
- プレイヤー内へのダウンロードUI追加。
- `build_package()`の出力構造・出力ファイル名変更。
- サーバー上の既存ファイルのリネーム・コピー。
- 永続化済み`Job`レコードへのダウンロード情報追加やマイグレーション。
- ダウンロード専用APIや`Content-Disposition`付きレスポンスの新設。
- リンク生成時のファイル存在確認や欠損ファイルの修復。
- CLI単独で生成され、Webのジョブレコードを持たないパッケージの一覧取り込み。
- READMEやプレイヤーテンプレートの変更。

## 方針

### 現状と維持する境界

- `src/bunri/package.py`は次のファイルを生成する。
  - `<safe>.<target>.mp3`
  - `<safe>.<target>.wav`
  - `<safe>.<target>.backing.mp3`
  - `<safe>.<target>.backing.wav`
  - `<safe>.original.mp3`
  - `<safe>.<target>.player.html`
- wavは常に生成され、mp3は`mp3=True`の場合に生成される。WebワーカーはCLIへ`--no-mp3`を渡さないため、通常の新規Webジョブでは4音声ファイルすべてが生成される。
- `Job.package`は永続化された相対パスであり、音声ディレクトリではなく`<safe>/<safe>.<target>.player.html`を指す。
- `/packages`は`out_dir`を既存の`StaticFiles`で配信しているため、配信ルートは追加しない。
- `/api/songs`の各ターゲットには既に`target_label`がある一方、`/api/jobs`の応答にはない。
- プレイヤーも`REGISTRY[target].label_ja + "のみ"/"なし"`でラベルを作っている。
- `job.title`は表示用文字列であり、必ずしもファイル名として安全ではない。現行のsafe化は`/\:*?"<>|#%`の置換、先頭ドット除去、空値の`untitled`化、`web`の`web-package`化を行う。
- `package_url`と既存の`target_label`は後方互換のため維持する。

### APIでダウンロード情報を供給する

クライアントが`package_url`を文字列置換して音声ファイル名を推測する方式は採用しない。`package_url`はプレイヤーへのURLであり、そこから音声ファイルを導出すると、パッケージ命名規則、backing接尾辞、URLエンコード、ラベル生成規則がブラウザ側へ漏れて二重管理になるためである。

`src/bunri/web/app.py`に応答専用の組み立て処理を追加し、`_serialize_job()`の結果へ次の構造の`downloads`を追加する。

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

`downloads`は次を満たすAPI契約とする。

- 各トラックエントリは`track`、`label`、`files`を持つ。
- `files`の各要素は`{format: "mp3"|"wav", url, filename}`を持つ。`format`はUIが表示に使う契約フィールドであり、UIはURLやファイル名から拡張子を解析しない。
- 命名規則と表示規則はサーバー側に集約する。
- ファイルの順序はtarget mp3 → target wav → backing mp3 → backing wavに固定する。具体的には`downloads`をtarget、backingの順、各`files`をmp3、wavの順に返す。
- `status == "done"`かつ`package`がある場合だけ2トラック・計4ファイルを返し、それ以外は必ず`downloads: []`とする。
- URLのディレクトリ名と音声ファイルのベース名は、検証済み`job.package`の先頭セグメントから取る。これにより、過去のsanitizerで作られたパッケージも実際のディスク配置へ到達できる。
- 保存ファイル名の曲名部分は`safe_filename(job.title)`から作る。URL上の旧パッケージ実体名と、現在の規則でsafe化した保存名は別物として扱う。
- 日本語ラベルは`REGISTRY[job.target].label_ja`を使用する。現行レジストリ外の旧・未知ターゲットは、既存の`target_label`と同様にターゲットキーへフォールバックし、一覧全体の直列化失敗を避ける。
- URL全体は既存`package_url`と同じく`quote()`でエンコードし、`/`の階層だけ維持する。
- `downloads`はAPI応答時だけ生成し、`src/bunri/web/jobs.py`の`Job` dataclassや`<out>/web/jobs/*.json`には保存しない。
- `/api/jobs`と`/api/songs`の双方で同じ情報を返し、直列化規則を一箇所に保つ。

### UI

`src/bunri/web/templates/index.html.j2`の各ターゲット表示を、ヘッダー行とダウンロード行を持つブロックとしてレンダリングする。

- ヘッダー行には楽器名、状態バッジ、完了時の「プレイヤーを開く」を置く。
- ダウンロード行には`ギターのみ: mp3 wav`、`ギターなし: mp3 wav`の形式でリンクを置く。
- `job.status === "done"`かつ`Array.isArray(job.downloads) && job.downloads.length > 0`の場合だけダウンロード行を生成する。
- 各リンクにはAPIの`url`を`href`、`filename`を`download`属性、`format`をリンクテキストとして設定する。UIでは拡張子解析を行わない。
- 各リンクに「ギターのみをmp3でダウンロード」の形式で`aria-label`を設定する。
- DOM生成は引き続き`textContent`とプロパティ設定を使い、タイトルやラベルをHTML文字列として挿入しない。
- 複数楽器では各楽器ブロックの直下にそれぞれ4リンクを置く。
- 既存の折りたたみ、ポーリング、フォーカス復元、エラー詳細、削除UI、プレイヤーリンクの挙動を維持する。
- 狭い画面では折り返せるflexレイアウトとし、既存カードの幅を押し広げない。

### 欠損ファイルの扱い

ファイルごとの`exists()`確認は行わず、完了ジョブには命名規則に基づく4リンクを常に返す。欠損時は既存`StaticFiles`の404に任せる。

理由は次のとおり。

- 通常のWebジョブは4音声ファイルを生成する。
- API一覧取得ごとに全完了ジョブ×4回のファイル確認を行う必要がない。
- ファイルが一覧取得後に削除される競合は、存在確認をしても防げない。
- `done`判定は現在プレイヤーHTMLの存在のみを確認しているため、手動削除や古い不完全パッケージは元々起こり得る。
- CLI単独の`--no-mp3`パッケージは、ジョブレコードがなければWeb一覧には現れない。

### 変更対象

- `src/bunri/web/app.py`
- `src/bunri/web/templates/index.html.j2`
- `tests/test_web_api.py`
- `tests/test_web_page.py`

原則として次は変更しない。

- `src/bunri/package.py`
- `src/bunri/web/jobs.py`
- `src/bunri/templates/player.html.j2`
- `tests/test_package.py`

## タスク分解

| # | タスク | 依存 |
|---|---|---|
| 1 | `app.py`にターゲットラベル取得、パッケージ音声URL、保存ファイル名、`downloads`配列を組み立てる応答専用ヘルパーを追加する | - |
| 2 | `_serialize_job()`に`downloads`を追加し、非完了ジョブでは空配列、完了ジョブではtarget/backing × mp3/wavを所定の順序で返す。`package_url`と永続化スキーマは維持する | 1 |
| 3 | `test_web_api.py`のfake runnerに4音声ファイルを生成させ、API構造、`format`契約、順序、URLエンコード、日本語保存名、旧実体名との不一致、original除外、非完了・失敗時の空配列、永続化Job JSON非変更を検証する。4ファイル生成後に1ファイルを削除し、一覧APIは成功し、削除したファイルのURLへのアクセスだけが404になることも検証する | 2 |
| 4 | `index.html.j2`にターゲット単位のダウンロード行とスタイルを追加し、APIの`label`、`format`、`url`、`filename`だけで4リンクを構築する | 2 |
| 5 | `test_web_page.py`のfake runnerに4音声ファイルを生成させ、完了前にはリンクがなく、完了後に正しいラベル、順序、URL、リンクテキスト、`download`属性、`aria-label`を持つ4リンクが現れることを実ブラウザで検証する | 4 |
| 6 | 複数ターゲット、エラー表示、折りたたみ、ポーリング、削除UIの既存テストを通し、必要ならセレクタをターゲットブロック単位に調整する | 3, 5 |
| 7 | 対象テストと全テストを実行し、ファイル配置、プレイヤー、永続化Jobレコードに変更がないことを確認する | 6 |

## 完了条件・受け入れ基準

- [ ] トップページの完了した楽器行だけにダウンロード欄が表示される。
- [ ] 待機中・処理中・失敗ジョブではAPIが`downloads: []`を返し、ダウンロードリンクが表示されない。
- [ ] `job.downloads`が空配列の場合、完了ジョブであってもダウンロード行の要素自体が生成されないことを実ブラウザテストで検証する。
- [ ] 1楽器につき、楽器のみmp3/wavと楽器なしmp3/wavの計4リンクが表示される。
- [ ] APIとDOMの4 URLがtarget mp3 → target wav → backing mp3 → backing wavの順序になる。
- [ ] `downloads[].files[]`が`format`、`url`、`filename`を持ち、`format`は`mp3`または`wav`である。
- [ ] UIが`format`をリンクテキストに使い、URLやファイル名の拡張子を解析しない。
- [ ] `original.mp3`のリンクがAPI応答・DOMのどちらにも含まれない。
- [ ] 表示ラベルが`label_ja + "のみ"/"なし"`となり、プレイヤー内の表記と一致する。
- [ ] 各`href`が既存パッケージ内の正しいtarget/backingファイルを指し、日本語・空白・特殊文字を安全にURLエンコードしている。
- [ ] 旧sanitizerによるパッケージ実体名と現在の`safe_filename(job.title)`による保存名が異なる場合も、`href`は実体名、`download`属性は現在のsafe化済み曲名を使い、リンクが正しく機能する。
- [ ] 各リンクの`download`属性が`<safe曲名>_<日本語トラック名>.<拡張子>`となる。
- [ ] 例として`スピッツ_チェリー_ギターのみ.mp3`、`スピッツ_チェリー_ギターなし.wav`が得られる。
- [ ] タイトルに`/`等が含まれても、保存名には`safe_filename()`適用後の曲名が使われる。
- [ ] 各リンクの`aria-label`が`<日本語トラック名>を<format>でダウンロード`となる。
- [ ] ダウンロードリンクは同一オリジンの`/packages/...`を利用し、サーバー上のファイル名を変更しない。
- [ ] プレイヤーリンクのURL、別タブ表示、既存UI動作が維持される。
- [ ] 複数楽器の曲では、各楽器に対応する4リンクが混線せず表示される。
- [ ] 4ファイル生成後に1ファイルを削除する明示テストで、一覧APIが成功し、削除したファイルのURLへのアクセスだけが404になることを固定する。
- [ ] 永続化されたJob JSONに`downloads`フィールドが追加されていない。
- [ ] `uv run pytest -q tests/test_web_api.py tests/test_web_page.py`がパスする。
- [ ] `uv run pytest -q`がパスする。CI相当ではPlaywright Chromiumを導入し、ブラウザテストをskipさせない。

## 未確定事項・リスクと判断の委ね方

| 項目 | 内容 | 実装時の扱い |
|---|---|---|
| 細部の見た目 | リンク間隔、色、枠線等は厳密指定されていない | 既存の`.sw-open-link`とカードのデザインへ合わせる範囲で実装者が判断してよい |
| 欠損ファイル | `done`は音声4ファイルの存在まで保証しておらず、手動削除・旧パッケージでは404になり得る | 存在確認は追加せず404を許容する。リンク非表示や警告UIが必要なら別要件として止まって報告する |
| 旧ターゲット | 永続化済みレコードには現行レジストリ外のターゲットが存在し得る | 既存互換性に合わせ、ラベルはターゲットキーへフォールバックする |
| 旧sanitizerのパッケージ | URL上のパッケージ名と現在の`safe_filename(job.title)`が一致しない場合がある | 配信URLは`job.package`の実体名、保存名は現在のsafe化済みタイトルから作る |
| ブラウザテスト環境 | Chromium未導入環境では既存テストがskipされる | CI相当の検収では`uv run playwright install --with-deps chromium`後に実行する |
| lint | リポジトリにlintコマンド・設定がない | 新規ツール導入は行わず、既存テストと`git diff --check`で検証する |
