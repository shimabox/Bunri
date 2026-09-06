# vocals / htdemucs モデル SHA-256 固定 実装計画

- 日付: 2026-09-07
- ブランチ: `plan/2026-09-07-model-sha256-pinning`
- 実装担当（駒）: Sol medium（`codex exec`、`workspace-write`）。Python のモデル取得境界、既存テスト、関連文書に限定された変更を、固定済みの設計に沿って実装する。

## 環境情報

- リポジトリ: `github.com/shimabox/Bunri`
- ベースブランチ: `main`
- ベース SHA: `60043e02722817c7ad83ad5c8ec42387208724f2`
- Python: `3.13.1`
- uv: `0.11.14`
- audio-separator: `0.44.3` 固定
- 対象テスト: `uv run pytest -q -n auto tests/test_separate.py tests/test_package.py tests/test_registry.py tests/test_dockerfile.py`
- 全テスト: `make test`（実体は `uv run pytest -q -n auto`）
- 依存不変確認: `uv lock --check`
- lint 相当: `git diff --check`。専用の lint、formatter、type checker のコマンドや設定はない。

## 背景・目的

Bunri の登録済み分離モデルのうち、vocals の `vocals_mel_band_roformer` と `htdemucs_6s` は、audio-separator の可変カタログおよび完全性検証をしないダウンロードに依存している。PyTorch checkpoint は読み込み時にコード実行へ至り得るため、対象モデルの全ファイルを Bunri が固定 SHA-256 で `load_model()` より前に検証し、検証済みファイルだけを audio-separator に渡す。

既に固定 SHA-256 で取得している guitar の becruily モデルも同じ固定モデル定義へ整理し、登録済みモデルの取得・検証の信頼境界を一箇所に集約する。

## 調査結果と信頼境界

### audio-separator の取得経路

audio-separator `0.44.3` の `download_file_if_not_exists()` は、保存先が通常ファイルなら `os.path.isfile()` だけでダウンロードを省略し、ファイルのハッシュを検証しない。保存先へ直接書き込むため、取得途中で失敗した不完全ファイルも、次回は存在するだけで再利用される可能性がある。一方、Bunri が検証済みファイルを先に実効 `model_file_dir` へ配置すれば、audio-separator に再取得させず読み込ませられる。

`vocals_mel_band_roformer` の定義は、remote の `download_checks.json` ではなく audio-separator wheel 内の `models.json` に含まれる。ただし、`list_supported_model_files()` は先に可変な `download_checks.json` を取得・解析する。対象ファイルは次の2つである。

- `vocals_mel_band_roformer.ckpt`
- `vocals_mel_band_roformer.yaml`

audio-separator が最初に試す TRvlvr release の ckpt・YAML URL は、2026-09-07 の実測でどちらも 404 だった。このため、Bunri が固定する正規取得 URL は、audio-separator のフォールバック先である次の mirror とする。

- `https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs/vocals_mel_band_roformer.ckpt`
- `https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs/vocals_mel_band_roformer.yaml`

`htdemucs_6s` は `download_checks.json` のキャッシュ実物で、識別子が `htdemucs_6s.yaml`、重みが `5c90dfd2-34c22ccb.th`、YAML が `htdemucs_6s.yaml` と定義されている。固定対象の URL は次のとおりである。

- `https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/5c90dfd2-34c22ccb.th`
- `https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/htdemucs_6s.yaml`

audio-separator は URL の basename で2ファイルを `model_file_dir` に置く。YAML は `models: ['5c90dfd2']` という bag 定義であり、Demucs loader は同じディレクトリの `.th` を探す。

### `download_checks.json` と既存ハッシュ機構

- `download_checks.json` は `https://raw.githubusercontent.com/TRvlvr/application_data/main/filelists/download_checks.json` から、キャッシュにない初回またはキャッシュ削除後に取得される。毎回取得されるわけではないが、参照先は可変である。
- JSON に SHA、checksum、hash の各フィールドはない。audio-separator の通常ダウンローダにも完全性検証はない。
- audio-separator の MD5 処理は一部 MDX / VR モデルの設定検索用であり、完全性検証ではない。今回の YAML ベース2モデルにも使用されない。
- Demucs は `5c90dfd2-34c22ccb.th` の `34c22ccb` と SHA-256 の先頭8桁だけを比較する。32 bit prefix に限られ、YAML は対象外で、失敗ファイルも削除しない。

したがって、audio-separator のカタログ・ダウンロード・既存 checksum 処理は、固定 SHA-256 の信頼根拠にはしない。

### 正値確認の進捗

2026-09-07 時点の采配役による実測では、次を確認済みである。

- vocals YAML は正規 mirror からの実取得値がローカルキャッシュの参考値と一致した。
- htdemucs の `.th` は `dl.fbaipublicfiles.com`、YAML は TRvlvr release からの実取得値が、それぞれローカルキャッシュの参考値と一致した。
- vocals ckpt は、作者 Hugging Face リポジトリ `KimberleyJSN/melbandroformer` の LFS SHA-256 `87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e` が、ローカルキャッシュおよび `MODEL_LICENSES.md` の記載値と一致した。正規 mirror からの実取得は確認時点で進行中だった。

4ファイルの確定 SHA-256 は、采配役が実取得結果と上流情報を最終突合して承認する。実装駒には `request.md` とは別に、依頼時の指示文で4値を渡す。実装駒はプレースホルダや下記参考値で作業を進めず、渡された確定値を定数として記載する。

### ローカルキャッシュの参考値

以下は既存キャッシュから算出した参考値であり、採用済みの正値ではない。

| ファイル | ローカル実測 SHA-256 |
|---|---|
| `vocals_mel_band_roformer.ckpt` | `87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e` |
| `vocals_mel_band_roformer.yaml` | `b958b29c8f7195f0d86bee6759a33980db675c4ecaf2fcaa80fa125828e6cd38` |
| `5c90dfd2-34c22ccb.th` | `34c22ccb381c6f9fdbf324f04e1e2fe21aaaf293f5ded163a162697ff9a02ddd` |
| `htdemucs_6s.yaml` | `207405151270af8fd81c2373c25d27950916682ac91dca7884a11ce13dad6f58` |

vocals ckpt は `MODEL_LICENSES.md` の既存記載値と一致し、htdemucs の重みはファイル名に含まれる8桁 checksum と一致する。ただし、これらの一致だけでは採用根拠とせず、采配役による最終承認を必須とする。

## スコープ

### やること

- 次の4ファイルについて、正規取得 URL、ローカル名、采配役承認済み SHA-256 を Bunri 側に固定する。
  - `vocals_mel_band_roformer.ckpt`
  - `vocals_mel_band_roformer.yaml`
  - `5c90dfd2-34c22ccb.th`
  - `htdemucs_6s.yaml`
- 初回ダウンロードと既存キャッシュの両方を `load_model()` より前に検証する。
- 登録済みモデルについて、audio-separator の可変 `download_checks.json` と無検証ダウンローダを経由させない。
- guitar の既存実装を同じ固定モデル定義へ整理し、既存 SHA、commit pin、MaskEstimator patch を維持する。
- vocals / guitar から htdemucs へのフォールバック時も、htdemucs の全 asset を検証してから利用する。
- Docker named volume、`BUNRI_MODEL_DIR`、既存キャッシュとの互換性を維持する。
- テストとモデル取得文書を更新する。

### やらないこと

- 任意の `--model` で指定された未登録モデルすべてへのハッシュ固定。
- モデル重みのリポジトリまたは Docker image への同梱。
- audio-separator のバージョン更新。
- モデルライセンス上の未解決事項の判断。
- 新しい依存、lint、CI、Docker volume 構成の追加。

## 方針

`src/bunri/separate.py` に、モデル名から必要 asset 群を引く固定マニフェストを設ける。各 asset は少なくともローカルファイル名、正規取得 URL、SHA-256 を持つ。guitar の既存 becruily 定数と bootstrap もこの共通構造へ移し、既存の commit pin、SHA-256、ローカル名、カタログ定義、MaskEstimator patch の挙動を変えない。

既知モデルの実行は次の順序に固定する。

1. `Separator` 構築後、そのインスタンスが実際に使用する `separator.model_file_dir` を取得する。
2. 選択モデルに必要な全 asset を既存 `_download_if_missing()` で事前配置・検証する。
3. 選択モデルだけを返す固定 catalog をインスタンスへ一時適用する。
4. audio-separator の downloader を、固定 asset が存在することだけを確認する guard に一時置換し、検証後の再ダウンロードを禁止する。
5. `separator.load_model()` を呼ぶ。
6. becruily の場合だけ既存 MaskEstimator patch を併用する。
7. catalog と downloader の一時置換は、成功・失敗を問わず必ず復元する。

固定 catalog は選択中のモデルだけを返し、可変 catalog が別の YAML や追加 asset を選ぶ余地をなくす。単にファイルを先置きして元の catalog を呼ぶ案は、可変 catalog を信頼境界内に残すため採用しない。

モデルディレクトリには `_MODEL_DIR` を決め打ちせず、構築済み `Separator` の実効 `model_file_dir` を使う。audio-separator は `AUDIO_SEPARATOR_MODEL_DIR` が設定されていると constructor 引数を上書きするためである。通常ホストと Docker では、従来どおり `BUNRI_MODEL_DIR` が示す場所を利用する。

### 検証失敗とフォールバック

- 既存ファイルのハッシュ不一致は、`_download_if_missing()` の既存契約どおりそのファイルを削除し、期待値、実値、対象パスを含む `RuntimeError` にする。
- 新規ダウンロードのハッシュ不一致は最終配置を行わず、一意な一時ファイルも削除する。
- 明示的 `--model`、および bass / drums / piano の htdemucs 失敗は、そのまま終了する。
- vocals または guitar の既定モデル失敗は、既存契約どおり警告を表示し、htdemucs へフォールバックする。フォールバック側も必ず事前検証する。
- フォールバック側も失敗した場合は、既存の combined error に双方の検証エラーを残す。
- 完全性エラーだけを特別にフォールバック禁止にはしない。検証済みの別モデルへ切り替える既存挙動を維持する。
- 書き込み不能なモデルディレクトリでは、元の `PermissionError` の原因を失わない明確なエラーにする。

## 変更対象

- `src/bunri/separate.py`
- `src/bunri/registry.py` の vocals コメントのみ
- `tests/test_separate.py`
- `tests/test_package.py`
- 必要な場合に限り `tests/` 配下の新規テスト
- `README.md`
- `NOTES.md`
- `MODEL_LICENSES.md`

`Dockerfile`、`compose.yaml`、`THIRD_PARTY_NOTICES.md` は変更しない。パス・volume、依存、ライセンス自体は変わらないためである。

## タスク分解

| # | タスク | 依存 |
|---|---|---|
| 1 | 采配役が隔離した新規キャッシュへ4ファイルを実取得し、URL、redirect、サイズ、SHA-256 を記録する。Hugging Face LFS oid、audio-separator registry、Demucs 公開 manifest 等と突合し、採用値を最終承認して、依頼時の指示文で実装駒へ渡す | - |
| 2 | `src/bunri/separate.py` に固定 model / asset 定義を追加し、既存 becruily 定数・bootstrap を共通構造へ移す | 1 |
| 3 | 既知モデル用の事前配置、固定 catalog、無検証再ダウンロード防止 guard を実装し、`_run_separation()` の `load_model()` 直前へ接続する | 2 |
| 4 | `tests/test_separate.py` を更新し、vocals・htdemucs の asset 組、呼び出し順序、既存／新規不一致、unknown model 非適用、fallback 連鎖をネットワークなしで検証する | 3 |
| 5 | `tests/test_package.py` の Fake Separator と autouse fixture を更新し、パッケージテストがモデルを実ダウンロードしないことを保証する | 3 |
| 6 | `src/bunri/registry.py`、`NOTES.md`、`MODEL_LICENSES.md`、`README.md` の取得経路、検証状況、適用範囲を更新する | 1–3 |
| 7 | 対象テスト、全テスト、lock 確認、`git diff --check` を実行する | 4–6 |

タスク1は采配役の作業であり、実装駒へは委ねない。実装駒は4つの確定 SHA-256 が依頼時の指示文に揃っていない場合、プレースホルダや参考値でタスク2以降を進めず停止して報告する。

## テストで押さえるエッジケース

- vocals / htdemucs のそれぞれで、全 asset の検証が完了するまで `load_model()` が呼ばれない。
- 既存ファイル一致時はネットワーク処理を行わず再利用する。
- 1ファイルでも既存ハッシュが違えば、そのファイルだけを削除して load を開始しない。
- 新規ダウンロード不一致では、正規名と一時ファイルの双方を残さない。
- ckpt が一致し YAML が不一致、またはその逆の場合も load しない。
- htdemucs を bass / drums / piano で共有しても同じ固定 asset 定義を使う。
- guitar / vocals の既定モデル失敗後、htdemucs の2 asset が検証される。
- fallback の htdemucs も不一致なら、combined error に双方の失敗が含まれる。
- 同じ既知モデルを `--model` で明示した場合も検証するが、失敗時はフォールバックしない。
- 未登録の明示モデルは従来どおり audio-separator に委譲し、Bunri の固定モデルと誤認しない。
- `AUDIO_SEPARATOR_MODEL_DIR` が constructor 引数を上書きした場合も、実効ディレクトリに配置する。
- 複数プロセスの cold cache 競合でも、一意 temp、SHA-256 検証、atomic replace を維持する。
- 書き込み不能なモデルディレクトリでは、`PermissionError` の原因を失わない。
- catalog / downloader の一時置換は、`load_model()` の成功・通常例外・`SystemExit` のすべてで復元される。
- テストは URL を mock し、実モデルや実ネットワークを使わない。

## 文書更新

- `MODEL_LICENSES.md`: 4ファイルの固定 SHA-256、正規 URL、確認日、Bunri 側の検証あり、known model では可変 catalog を使わないことへ更新する。
- `README.md`: 初回取得と既存キャッシュの SHA-256 検証、不一致時の削除と再実行による再取得、任意の未登録 `--model` は固定対象外であることを簡潔に追記する。
- `NOTES.md`: vocals は「audio-separator 自身がダウンロード、bootstrap 不要」という現状記述を更新し、audio-separator 更新時に固定 catalog の契約を再確認する項目を追加する。
- `src/bunri/registry.py`: vocals のコメントだけを現行実装に合わせて更新する。
- `Dockerfile` / `compose.yaml`: パス・volume 仕様は変更不要。
- `THIRD_PARTY_NOTICES.md`: ライセンスや依存が変わらないため変更不要。

## 完了条件・受け入れ基準

- [ ] 4ファイルの SHA-256 が采配役により上流情報と突合・承認され、依頼時の指示文で実装駒へ渡されている。
- [ ] vocals / htdemucs の初回取得・既存キャッシュの双方が、読み込み前に完全 SHA-256 検証される。
- [ ] 不一致ファイルは削除され、未検証 bytes が YAML parser、`torch.load`、Demucs loader に渡らない。
- [ ] guitar、vocals、htdemucs の登録済み3モデルは、可変 `download_checks.json` に依存せずロードできる。
- [ ] 正しい既存モデルは再ダウンロードせず、Docker named volume でも再利用できる。
- [ ] fallback の htdemucs も同じ検証を通り、失敗時の最終エラーに両方の原因が残る。
- [ ] 任意の未登録 `--model` の既存互換性と、検証対象外という境界が維持・明記されている。
- [ ] `AUDIO_SEPARATOR_MODEL_DIR` により constructor 引数が上書きされても、実効モデルディレクトリが使われる。
- [ ] `uv run pytest -q -n auto tests/test_separate.py tests/test_package.py tests/test_registry.py tests/test_dockerfile.py` がパスする。
- [ ] `make test`、`uv lock --check`、`git diff --check` がパスする。
- [ ] テスト中に実モデルまたは remote catalog のダウンロードが発生しない。
- [ ] `MODEL_LICENSES.md`、`README.md`、`NOTES.md` の記述が実装と一致する。

## 未確定事項・リスクと判断の委ね方

| 項目 | 内容 | 実装時の扱い |
|---|---|---|
| 正の4ハッシュ | ローカルキャッシュ値だけでは真正性の根拠にならない | 采配役が実取得・上流突合して承認し、依頼時の指示文で渡す。値が不足・不一致なら実装駒は停止して報告する |
| vocals の正規配布元 | audio-separator が最初に試す TRvlvr release の ckpt・YAML URL は 2026-09-07 時点で 404。作者 Hugging Face 版との対応確認も必要 | 固定 URL は audio-separator のフォールバック先である nomadkaraoke mirror とする。ckpt は作者リポジトリの LFS SHA-256 とも照合し、采配役の最終承認なしで固定しない |
| YAML の公表ハッシュ | ckpt より公表値が見つからない可能性がある | pinned upstream revision、release asset、複数配布元の byte 一致を記録し、采配役の承認なしで固定しない |
| htdemucs 重みの根拠 | ファイル名が保証するのは SHA-256 の先頭8桁だけ | 公式 manifest 等の完全 SHA-256 を優先し、8桁一致だけで承認しない |
| audio-separator 内部 API | instance method の一時置換は `0.44.3` の catalog / downloader 契約に依存する | 現在はバージョン固定のため許容する。更新チェックリストと回帰テストで契約を固定する |
| 検証コスト | vocals ckpt は約871 MB で、分離実行ごとに全体を読む | 完全性を優先して streaming hash を維持する。package cache hit 時は分離自体が走らない |
| 既存キャッシュ | 古い・途中取得・改変ファイルは初回利用時に削除される | 意図した移行挙動とし、README に再実行で再取得される旨を記載する |
| primary URL 廃止 | SHA-256 が正しくても取得 URL は消える可能性があり、TRvlvr の vocals URL は既に廃止例となった | 同一 SHA-256 を確認済みの mirror だけを追加可能とし、未確認 mirror へ自動切替しない |

