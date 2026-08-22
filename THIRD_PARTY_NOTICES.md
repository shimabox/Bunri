# THIRD_PARTY_NOTICES

Bunri は、ほかの開発者が公開しているソフトを組み合わせて動きます。この文書では、その中でも利用条件に注意が必要なものを紹介します。AI モデルについては [MODEL_LICENSES.md](MODEL_LICENSES.md) にまとめています。

## まず知っておいてほしいこと: 非商用の条件があります

Bunri が内部で使っている **diffq** は、非商用に限って利用できます。Bunri 自身のコードは MIT License(`LICENSE`)ですが、Bunri を動かすときには diffq も使うため、**現在の Bunri は非商用利用を前提としています**(README の注意事項も参照)。

この文書では、Bunri が利用しているソフトのうち、特に利用条件に注意が必要なものを紹介しています。すべてのソフトの名前とバージョンは `uv.lock` に記録されています。

## 技術情報

### diffq の非商用条件について

- `diffq` 0.2.4 は CC BY-NC 4.0(非商用)です(<https://github.com/facebookresearch/diffq>)。`audio-separator` 経由で導入され、同梱の Demucs コードが量子化済みモデルの読み込みに利用します。
- CC BY-NC 4.0 は OSI 準拠の OSS ライセンスではありません。Bunri 自身を MIT にしても、実行に必要なソフト全体は「商用利用可能な完全 OSS」ではありません。
- Docker イメージにも `diffq` が含まれるため、イメージを配布する場合は特に注意してください(後述「Docker イメージについて」)。

### 確認方法

- ライセンスは、導入済み仮想環境(`.venv`)の各パッケージの `METADATA` (`importlib.metadata`)と `uv.lock` に記録されたバージョンから確認しました(確認日: 2026-08-22、`uv.lock` 時点のバージョン)。
- メタデータから確認できなかったものは「未確認」と記載しています。
- バージョンは `uv.lock` の更新で変わります。依存を更新したら本書も見直してください。

### Bunri が直接利用しているソフト(`pyproject.toml`)

| パッケージ | バージョン(uv.lock) | ライセンス | URL |
|---|---|---|---|
| audio-separator | 0.44.3 | MIT | <https://github.com/nomadkaraoke/python-audio-separator> |
| torch (PyTorch) | 2.13.0 | 複数のライセンスを含む(BSD-3-Clause のほか Apache-2.0(LLVM-exception 付きを含む)、BSD-2-Clause、MIT、BSL-1.0 など。詳細は PyTorch の LICENSE / third_party を参照) | <https://github.com/pytorch/pytorch> |
| torchvision | 0.28.0 | BSD-3-Clause | <https://github.com/pytorch/vision> |
| numpy | 2.4.6 | BSD-3-Clause(同梱コードに 0BSD / MIT / Zlib / CC0-1.0 を含む) | <https://numpy.org/> |
| soundfile | 0.14.0 | BSD-3-Clause(同梱 libsndfile は LGPL-2.1+) | <https://github.com/bastibe/python-soundfile> |
| jinja2 | 3.1.6 | BSD-3-Clause | <https://github.com/pallets/jinja> |
| rich | 15.0.0 | MIT | <https://github.com/Textualize/rich> |
| typer | 0.26.8 | MIT | <https://github.com/fastapi/typer> |
| fastapi(`web` extra) | 0.139.0 | MIT | <https://github.com/fastapi/fastapi> |
| uvicorn(`web` extra) | 0.51.0 | BSD-3-Clause | <https://github.com/encode/uvicorn> |
| python-multipart(`web` extra) | 0.0.32 | Apache-2.0 | <https://github.com/Kludex/python-multipart> |

### 上記のソフトがさらに利用しているソフトのうち、重要なもの

| パッケージ | バージョン(uv.lock) | ライセンス | 備考 |
|---|---|---|---|
| **diffq** | 0.2.4 | **CC BY-NC 4.0(非商用)** | Facebook Research。`audio-separator` 経由で導入され、同梱 Demucs コード(`uvr_lib_v5/demucs/states.py` 等)が量子化済みモデルの読み込みに利用。<https://github.com/facebookresearch/diffq> |
| demucs(同梱コード) | (PyPI パッケージとしては未導入) | MIT(未確認: `audio-separator` に vendoring された `uvr_lib_v5/demucs/` は Facebook Research 由来のヘッダのみで、LICENSE 原文は同梱されていない。upstream <https://github.com/facebookresearch/demucs> は MIT) | htdemucs_6s の推論コード |
| onnxruntime | 1.27.0 | MIT | Microsoft。<https://github.com/microsoft/onnxruntime> |
| onnx-weekly | 1.23.0.dev20260706 | Apache-2.0 | `audio-separator` が pre-release のみを公開する onnx-weekly に依存 |
| onnx2torch(`onnx2torch-py313`) | 1.6.0 | Apache-2.0 | |
| UVR 由来モデルコード(`audio_separator/separator/uvr_lib_v5/`) | audio-separator 0.44.3 同梱 | 未確認(`audio-separator` 全体は MIT。元は Ultimate Vocal Remover <https://github.com/Anjok07/ultimatevocalremovergui>(MIT)および lucidrains/BS-RoFormer(MIT)由来だが、ファイル単位の権利表示は確認していない) | Mel-Band Roformer / MDX / VR / Demucs の推論実装 |
| rotary-embedding-torch | 0.6.5 | MIT | lucidrains |
| einops | 0.8.2 | MIT | |
| beartype | 0.18.5 | MIT | |
| librosa | 0.11.0 | ISC | |
| julius | 0.2.8 | MIT | Facebook Research |
| pydub | 0.25.1 | MIT | |
| samplerate | 0.1.0 | MIT | |
| resampy | 0.4.3 | ISC | |
| scipy | 1.18.0 | BSD-3-Clause | |
| tqdm | 4.68.4 | MPL-2.0 AND MIT | |
| requests | 2.34.2 | Apache-2.0 | |
| pyyaml | 6.0.3 | MIT | |
| ml_collections | 1.1.0 | Apache-2.0 | |

上記以外のソフト(`uv.lock` 参照)は網羅していません。

### 外部ツール

| ツール | ライセンス | 備考 |
|---|---|---|
| ffmpeg | LGPL-2.1+(ビルド構成により GPL-2.0+/GPL-3.0+。Homebrew / Debian 配布版の構成は未確認) | 入力の正規化・mp3 変換に外部コマンドとして呼び出すだけで、Bunri には同梱しません。Docker イメージには Debian パッケージの ffmpeg が含まれます。<https://ffmpeg.org/legal.html> |
| uv | MIT OR Apache-2.0 | セットアップ・実行時のみ |

### Docker イメージについて

`Dockerfile` でビルドしたイメージには上記の Python パッケージ(`diffq` を含む)と Debian の ffmpeg が含まれます。配布形態が変わるため、イメージを公開配布する場合は本書の内容を改めて精査してください。現状、Docker イメージの公開配布は行っていません。
