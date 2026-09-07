# 実装依頼: vocals / htdemucs モデル SHA-256 固定

## 背景

Bunri の登録済み分離モデルのうち、vocals の `vocals_mel_band_roformer` と `htdemucs_6s` は、audio-separator の可変カタログと完全性検証をしないダウンロードに依存している。対象4ファイルを Bunri 側の固定 SHA-256 で `load_model()` より前に検証し、登録済みモデルでは未検証 bytes と可変 catalog を信頼境界から外す。

## 対象

- リポジトリ: `github.com/shimabox/Bunri`
- ベースブランチ: `main`（参考。起点は次の SHA とする）
- ベース SHA: `60043e02722817c7ad83ad5c8ec42387208724f2`
- 作業ブランチ: `plan/2026-09-07-model-sha256-pinning`
- 実装担当（駒）: Sol medium（`codex exec`、`workspace-write`）

リポジトリルートで、必ずベース SHA から次のコマンドにより作業ブランチを作成してから着手する。ブランチ名や現在の HEAD を起点にしない。

```bash
git switch -c plan/2026-09-07-model-sha256-pinning 60043e02722817c7ad83ad5c8ec42387208724f2
```

## 実装前提: 確定 SHA-256 の受け渡し

SHA-256 の正値は采配役が4ファイルを実取得して上流情報と突合・承認し、この `request.md` とは別に、依頼時の指示文で4ファイル分を実装駒へ渡す。実装前に、次の各ファイルについて64桁の確定 SHA-256 が指示文に明記されていることを確認する。

- `vocals_mel_band_roformer.ckpt`
- `vocals_mel_band_roformer.yaml`
- `5c90dfd2-34c22ccb.th`
- `htdemucs_6s.yaml`

4値のいずれかがない、64桁の16進数ではない、または同じ指示文の中で値が食い違う場合は、実装を開始せず、不足・不一致を報告して停止する。プレースホルダ、ローカルキャッシュの参考値、ファイル名に含まれる8桁 checksum を正値の代わりに使わない。4値が揃っている場合は、渡された値を `src/bunri/separate.py` の固定モデル定義へ定数として記載する。

## 実装仕様

### 固定 model / asset 定義

`src/bunri/separate.py` に、モデルファイル名から必要 asset 群を引く固定マニフェストを設ける。各 asset は少なくとも、audio-separator が参照するローカルファイル名、正規取得 URL、采配役から渡された SHA-256 を持たせる。

新たに固定する asset は次のとおりとする。

| モデル | ローカルファイル名 | 正規取得 URL |
|---|---|---|
| vocals | `vocals_mel_band_roformer.ckpt` | `https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs/vocals_mel_band_roformer.ckpt` |
| vocals | `vocals_mel_band_roformer.yaml` | `https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs/vocals_mel_band_roformer.yaml` |
| htdemucs | `5c90dfd2-34c22ccb.th` | `https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/5c90dfd2-34c22ccb.th` |
| htdemucs | `htdemucs_6s.yaml` | `https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/htdemucs_6s.yaml` |

vocals について audio-separator が最初に試す TRvlvr release の URL は、2026-09-07 時点で ckpt・YAML とも 404 のため使用しない。上表の nomadkaraoke mirror を固定 URL とする。確認していない別 mirror への自動切替は追加しない。

guitar の becruily モデルについて、既存の取得 URL、pinned Hugging Face commit、ローカル名、SHA-256、catalog 内容、`mlp_expansion_factor=1` の MaskEstimator patch を維持したまま、同じ固定マニフェストと共通の bootstrap 処理へ整理する。guitar の既存 SHA-256 は変更しない。

固定対象となるモデル名は、少なくとも次の3つである。

- `mel_band_roformer_guitar_becruily.ckpt`
- `vocals_mel_band_roformer.ckpt`
- `htdemucs_6s.yaml`

文字列の部分一致で未知モデルを既知モデルと誤認しないよう、固定マニフェストのキーとの完全一致で適用範囲を決める。

### 読み込み前の配置・検証

既知モデルをロードするたびに、次の順序を守る。

1. `Separator` を構築する。
2. constructor に渡した `_MODEL_DIR` を決め打ちせず、構築済みインスタンスの `separator.model_file_dir` を取得する。これは `AUDIO_SEPARATOR_MODEL_DIR` が constructor 引数を上書きする場合に実効ディレクトリを使うためである。
3. 選択モデルに必要な全 asset を、既存 `_download_if_missing()` で実効ディレクトリへ事前配置・検証する。
4. 選択モデルだけを返す固定 catalog を、その `Separator` インスタンスへ一時適用する。audio-separator の可変 `download_checks.json` を参照せず、別 YAML や追加 asset を選ばせない。
5. audio-separator の downloader を、要求された固定 asset が既に存在することだけを確認する guard に一時置換する。検証後の再ダウンロードや未検証 URL へのアクセスを許可しない。固定定義にないファイルを要求された場合も失敗させ、ネットワークへ委譲しない。
6. `separator.load_model(model_name)` を呼ぶ。becruily の場合だけ、既存の MaskEstimator patch をこの呼び出しの間に併用する。
7. 固定 catalog と downloader の一時置換は、成功、通常例外、`SystemExit` のいずれでも必ず元へ戻す。

単に asset を先置きして元の catalog を呼ぶ実装にはしない。可変 catalog が信頼境界内に残るためである。

`_download_if_missing()` の既存契約を維持する。

- 既存ファイルは全体を streaming hash し、一致すればネットワーク処理なしで再利用する。
- 既存ファイルが不一致ならそのファイルだけを削除し、期待値、実値、対象パスを含む `RuntimeError` にする。その呼び出しでは自動再取得せず、再実行時に取得する。
- 新規取得は一意な sibling temp file を使い、SHA-256 一致後だけ atomic replace する。不一致・取得失敗では正規名と一時ファイルを残さない。
- 複数プロセスの cold cache 競合でも、固定 `.part` 名を共有しない。
- 書き込み不能なモデルディレクトリでは、元の `PermissionError` の原因を失わない。

### フォールバックと未登録モデル

- guitar または vocals の既定モデルがロードまたは分離に失敗した場合は、既存契約どおり警告を表示し、`htdemucs_6s.yaml` へフォールバックする。フォールバック側の2 asset も、`load_model()` より前に同じ固定 SHA-256 検証を通す。
- フォールバック側も失敗した場合は、既存の combined error に既定モデルと htdemucs の双方の検証・ロード失敗を残す。
- 完全性エラーだけをフォールバック禁止にはしない。検証済みの別モデルへ切り替える既存挙動を維持する。
- bass / drums / piano が共有する既定の `htdemucs_6s.yaml` も同じ固定定義を使う。これらにはフォールバックを追加しない。
- `--model` で固定対象のモデル名を明示した場合も検証するが、既存契約どおり失敗時はフォールバックしない。
- 固定マニフェストにない任意の明示モデルは、従来どおり audio-separator に委譲する。Bunri の固定 SHA-256 検証対象とはしない。

### テスト

`tests/test_separate.py` を更新し、少なくとも次を検証する。テストの URL 取得は必ず mock し、実ネットワーク、remote catalog、実モデルファイルを使わない。

- vocals と htdemucs の各 asset 組が、正しいローカル名、固定 URL、指示文で渡された SHA-256 を使う。
- 全 asset の検証が完了する前に `load_model()` が呼ばれない。
- 既存ファイル一致時はネットワーク処理を行わず再利用する。
- 1ファイルでも既存ハッシュが違えば、そのファイルだけを削除し、load を開始しない。
- 新規ダウンロード不一致では、正規名と一時ファイルを残さない。
- ckpt が一致し YAML が不一致、およびその逆のどちらでも load しない。
- htdemucs を bass / drums / piano で共有しても同じ固定 asset 定義を使う。
- guitar / vocals の既定モデル失敗後に、htdemucs の2 asset が検証される。
- fallback の htdemucs も不一致なら、combined error に双方の失敗が含まれる。
- 固定対象モデルを `--model` で明示しても検証され、失敗時にフォールバックしない。
- 未登録の明示モデルには固定 bootstrap を適用せず、従来どおり委譲する。
- `AUDIO_SEPARATOR_MODEL_DIR` が constructor 引数を上書きしても、実効ディレクトリへ配置する。
- cold cache の競合で一意 temp、SHA-256 検証、atomic replace を維持する。
- 書き込み不能時の例外で `PermissionError` の原因を失わない。
- catalog / downloader の一時置換を、成功、通常例外、`SystemExit` の後に復元する。
- becruily の既存 SHA、commit pin、catalog、MaskEstimator patch が維持される。

`tests/test_package.py` の Fake Separator と autouse fixture も固定マニフェストに合わせて更新し、パッケージテスト中に実モデル取得や remote catalog 取得が起きないようにする。既存テストへの追記で不自然になる場合に限り、`tests/` 配下へ新規テストファイルを追加してよい。

### 文書とコメント

- `MODEL_LICENSES.md`: 4ファイルの固定 SHA-256、上表の正規 URL、確認日、Bunri 側で初回取得・既存キャッシュを検証すること、known model では可変 catalog を使わないことを記載する。采配役から渡された確定値を使う。
- `README.md`: 初回取得と既存キャッシュの SHA-256 検証、不一致ファイルは削除され再実行時に再取得されること、任意の未登録 `--model` は固定対象外であることを簡潔に記載する。
- `NOTES.md`: vocals の「audio-separator 自身がダウンロードし bootstrap 不要」という現状記述を更新する。audio-separator 更新時に、固定 catalog と downloader guard が内部 API の契約に適合するか再確認する項目を追加する。
- `src/bunri/registry.py`: vocals の説明コメントだけを実装後の取得・検証経路に合わせて更新する。レジストリの値や挙動は変更しない。

## タスク（この順で）

1. 指示文に4ファイル分の確定 SHA-256 が揃い、各値が64桁の16進数であることを確認する。不足・不一致ならプレースホルダで進めず停止して報告する。
2. `src/bunri/separate.py` に固定 model / asset マニフェストを追加し、guitar の既存 becruily 定数・bootstrap を、既存の pin・SHA・patch を保った共通構造へ移す。
3. 選択モデルの全 asset を実効 `separator.model_file_dir` へ事前配置・検証し、固定 catalog と downloader guard を一時適用してから `load_model()` を呼ぶ処理を実装する。後始末は成功・失敗を問わず保証する。
4. vocals / guitar の既定モデルと htdemucs フォールバック、bass / drums / piano の共有、明示モデル、未登録モデルについて、既存のフォールバック境界を維持する。
5. `tests/test_separate.py` と `tests/test_package.py` を更新し、固定 asset、検証順序、失敗時の削除、競合、一時置換の復元、フォールバック、未知モデル非適用をネットワークなしで検証する。必要な場合だけ新規テストを追加する。
6. `src/bunri/registry.py` の vocals コメント、`README.md`、`NOTES.md`、`MODEL_LICENSES.md` を実装と一致する内容へ更新する。
7. 対象テスト、全テスト、lock 不変確認、diff の whitespace 確認を順に実行する。
8. 許可された実装・テスト・文書ファイルだけを明示的に stage し、日本語の commit message で1 commit にまとめる。push はしない。

## 変更可能な範囲

- `src/bunri/separate.py`
- `src/bunri/registry.py` の vocals コメントのみ
- `tests/test_separate.py`
- `tests/test_package.py`
- 必要な場合に限り `tests/` 配下の新規テストファイル
- `README.md`
- `NOTES.md`
- `MODEL_LICENSES.md`

上記以外は変更しない。特に `Dockerfile`、`compose.yaml`、`THIRD_PARTY_NOTICES.md`、依存定義、lockfile、CI 設定は変更しない。`docs/plans/2026-09-07-model-sha256-pinning/plan.md` と `docs/plans/2026-09-07-model-sha256-pinning/request.md` は実装 commit に含めない。

## テスト・検証

まず対象テストを実行する。

```bash
uv run pytest -q -n auto tests/test_separate.py tests/test_package.py tests/test_registry.py tests/test_dockerfile.py
```

対象テストがパスしたら、次をすべて実行する。

```bash
make test
uv lock --check
git diff --check
```

専用の lint、formatter、type checker は導入・実行しない。全テストの実体は `uv run pytest -q -n auto` である。すべてのモデル取得 URL を mock し、テスト中に実ネットワーク、remote catalog、実モデルを使わない。

## 完了条件

- [ ] 采配役から渡された、上流情報と突合・承認済みの4つの SHA-256 だけが固定定義に記載されている。
- [ ] vocals / htdemucs の初回取得と既存キャッシュが、`load_model()` より前に完全 SHA-256 検証される。
- [ ] 不一致ファイルは削除され、未検証 bytes が YAML parser、`torch.load`、Demucs loader に渡らない。
- [ ] guitar、vocals、htdemucs の登録済み3モデルが、可変 `download_checks.json` に依存せずロードできる。
- [ ] 正しい既存ファイルは再取得せず、`BUNRI_MODEL_DIR` と Docker named volume の既存挙動を維持する。
- [ ] guitar / vocals のフォールバック先 htdemucs も同じ検証を通り、双方が失敗した場合は combined error に両方の原因が残る。
- [ ] 固定対象モデルの明示指定では検証するがフォールバックせず、任意の未登録 `--model` は従来互換かつ固定対象外である。
- [ ] becruily の既存 SHA、commit pin、catalog、MaskEstimator patch が維持される。
- [ ] catalog / downloader の一時置換が成功・失敗を問わず復元される。
- [ ] `uv run pytest -q -n auto tests/test_separate.py tests/test_package.py tests/test_registry.py tests/test_dockerfile.py` がパスする。
- [ ] `make test`、`uv lock --check`、`git diff --check` がパスする。
- [ ] テスト中に実モデルまたは remote catalog のダウンロードが発生しない。
- [ ] `MODEL_LICENSES.md`、`README.md`、`NOTES.md`、`src/bunri/registry.py` のコメントが実装と一致する。
- [ ] 作業ブランチに commit 済みである。commit message は日本語で、「何を・なぜ」が利用者視点で分かる内容にする。

## 未確定事項と判断の委ね方

- 勝手に決めてよい範囲: 固定済みの処理順序と既存契約を変えない範囲での型・定数・helper・context manager の名前、テスト helper の分割、新規テストファイルを使うかどうか。
- 止まって報告すべき範囲: 4つの確定 SHA-256 の不足・不一致、確定 SHA-256 と取得 bytes の不一致、固定 URL から取得できない状態、audio-separator `0.44.3` で固定 catalog / downloader guard を実現できない内部 API 差異、既存フォールバック契約の変更、許可範囲外のファイル変更、新規依存やスコープ拡大が必要な場合。
- mirror の判断: 固定 URL が使えない場合、同一 SHA-256 を確認していない別 URL へ自動で切り替えず、候補 URL と確認結果を報告して停止する。
- モデルライセンス: 未解決事項の判断やライセンス表現の拡張は行わず、取得・検証方法を現状の記載へ反映する範囲に留める。

## commit ルール

- commit message は日本語にする。
- commit message には、変更した機能と利用者にとっての理由だけを書く。特定の依頼元、個人的事情、作業のきっかけ、計画書への参照、私的なリンクを書かない。
- `git add -A` は使わない。変更可能なファイルだけをパス指定で stage する。
- `docs/plans/2026-09-07-model-sha256-pinning/plan.md` と `docs/plans/2026-09-07-model-sha256-pinning/request.md` は stage・commit しない。
- push しない。commit までで止める。

## 禁止事項

- push しない。
- プレースホルダ、未承認値、参考値で SHA-256 定数を実装しない。
- スコープ外のファイルを変更しない。無関係なリファクタを行わない。
- audio-separator を更新しない。新規依存、lint、CI、Docker volume 構成を追加しない。
- モデル重みをリポジトリまたは Docker image に同梱しない。
- 実ネットワーク、remote catalog、実モデルを使うテストを追加・実行しない。
- commit message、コードコメント、README 等の文書に、特定の利用元や個人的事情を修正理由として書かない。
- commit message、コードコメント、README 等の文書に、個人環境のローカルパス、OS ユーザー名、ホスト名、私的なリンクを書かない。リポジトリ内の参照はリポジトリルートからの相対パスにする。
- 人間向けの質問 UI や選択式プロンプトを出さない。停止条件に該当した場合は、選択肢と推奨案を通常の報告に書いて停止する。

## 報告フォーマット

- 変更ファイル一覧
- 実行したテスト・検証コマンドと、それぞれの結果
- commit SHA と commit message
- 判断に迷った点・未解決の懸念（なければ「なし」）
