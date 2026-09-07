# MODEL_LICENSES

Bunri は、ほかの開発者が公開している AI モデル(音源を分離する学習済みモデル)を使って動きます。この文書では、Bunri が使うモデルの利用条件をまとめています。ソフト(ライブラリ)の利用条件は [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)を参照してください。

## まず知っておいてほしいこと

- **ギター**のモデルは、作者が「非商用なら利用できる」と回答している段階で、正式なライセンス文書はありません。**現在の Bunri は非商用利用を前提としています**(README の注意事項も参照)。
- **ボーカル**のモデルは MIT License です。
- **ベース / ドラム / ピアノ**(およびギター・ボーカルが失敗したときの代替)に使う htdemucs_6s は、過去にメンテナーが当時のモデルについて「科学目的に限って提供」と回答していますが、このモデルに当てはまるかは明記されていません。
- モデルは Bunri に同梱していません。初回の分離時に各配布元から自動でダウンロードされます。

## 方針

- **モデル重みはこのリポジトリ(および GitHub Release)に同梱しません。** 初回の分離時に各配布元から `models/`(`BUNRI_MODEL_DIR`)へ自動ダウンロードされます。
- モデルの重みはコードとは別の権利関係にあり、配布元に置かれていることだけでは利用・再配布・商用利用の許諾確認にはなりません。
- 確認できていない点は「不明」「未確認」と正直に書きます。
- 以下の条件は 2026-09-07 時点の調査結果であり、配布元の変更で変わり得ます。

## ギター

- モデル: becruily guitar model(Mel-Band Roformer)
- 利用条件: 作者は非商用なら利用可能と回答
- Bunri への同梱: なし(初回実行時にダウンロード)
- 確認状況: 正式なライセンス文書はなし

### 技術情報

- target: `guitar`(既定)
- ファイル: `mel_band_roformer_guitar_becruily.ckpt`(HF 上の名前:`becruily_guitar.ckpt`) + `config_mel_band_roformer_guitar_becruily.yaml`(HF 上の名前:`config_guitar_becruily.yaml`)
- 配布元: becruily / <https://huggingface.co/becruily/mel-band-roformer-guitar>
- 固定した commit: HF commit `6409e7f88754b07ef7ca3bd1b76a15f010f1672a`
- SHA-256: ckpt `83472bbf125774af5282d2e0b86df89eaf2dd45e8a4ec8d68e820ebf3e42a83c`、yaml `b681c3f886251b04b666b3f06e87ce65d7ec610e40b5d75915c01782e5444b0e`
- 作者回答: [discussions/9](https://huggingface.co/becruily/mel-band-roformer-guitar/discussions/9)— 「generally free to use as long as it's non commercial」(基本的に非商用なら無料)という段階。必要なら明示ライセンスを追加する意向と、商用は Discord で個別相談との記載あり
- 商用利用: 非商用のみ(作者回答ベース。正式文書なし)
- 再配布: 不明(作者の明示なし。Bunri は同梱せず HF から直接取得)
- Bunri 側での検証: あり。`src/bunri/separate.py` が固定 commit から取得し、ダウンロード後に SHA-256 を検証します(不一致の場合はファイルを削除してエラー)

## ボーカル

- モデル: Kimberley Jensen vocals model(Mel-Band Roformer)
- 利用条件: MIT License
- Bunri への同梱: なし(初回実行時にダウンロード)
- 確認状況: 作者の配布元は MIT。Bunri が取得するファイルが作者配布のものと同一であることを確認済み

### 技術情報

- target: `vocals`(既定)
- ファイル: `vocals_mel_band_roformer.ckpt` + `vocals_mel_band_roformer.yaml`
- 作者: Kimberley Jensen(KimberleyJSN)。audio-separator カタログ名"MelBand Roformer | Vocals by Kimberley Jensen"
- 作者の一次配布元: <https://huggingface.co/KimberleyJSN/melbandroformer> (**MIT** タグ。2026-08-22 確認)
- Bunri の取得元: checkpoint <https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs/vocals_mel_band_roformer.ckpt>、YAML <https://github.com/nomadkaraoke/python-audio-separator/releases/download/model-configs/vocals_mel_band_roformer.yaml>
- 同一性: checkpoint は作者 HF 版 `MelBandRoformer.ckpt` と SHA-256・サイズ(913,106,900 bytes)が一致することを 2026-09-07 に確認済み
- 商用利用: 可(MIT)
- 再配布: 可(MIT)
- SHA-256: checkpoint `87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e`、YAML `b958b29c8f7195f0d86bee6759a33980db675c4ecaf2fcaa80fa125828e6cd38`
- Bunri 側での検証: あり。初回取得と既存キャッシュの両方を検証し、固定カタログからロードします

## htdemucs_6s(ベース / ドラム / ピアノの既定、ギター / ボーカルの代替)

- モデル: Demucs v4 htdemucs_6s(Hybrid Transformer Demucs, 6 stems)
- 利用条件: Demucs のコードは MIT。モデル(重み)の利用条件は不明
- Bunri への同梱: なし(初回実行時にダウンロード)
- 確認状況: 過去の issue では、当時の学習済みモデルについて「MIT License の対象外で、科学目的に限って提供している」と回答されています。ただし、この回答は htdemucs_6s の公開前のもので、このモデルにも当てはまるかは明記されていません

### 技術情報

- target: `bass` / `drums` / `piano` の既定、`guitar` / `vocals` のフォールバック
- ファイル: `htdemucs_6s.yaml` → 重み `5c90dfd2-34c22ccb.th`
- 作者: Meta / Facebook Research(Alexandre Défossez ほか)。
  <https://github.com/facebookresearch/demucs>
- 配布元: 重み <https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/5c90dfd2-34c22ccb.th>、YAML <https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/htdemucs_6s.yaml>
- ライセンス: Demucs コードは MIT。学習済み重みのライセンスは upstream で明文化されていません。[facebookresearch/demucs#327](https://github.com/facebookresearch/demucs/issues/327)でメンテナー(Alexandre Défossez)が 2022-05-23 に「The model weights are not covered by the MIT license, and are provided only for scientific purposes」(重みは MIT の対象外で、科学目的に限って提供)と回答しています(2026-08-22確認)。ただし、この回答は htdemucs(v4)公開前のもので、htdemucs_6s というこのモデルに当てはまるかは明記されていません。htdemucs_6s の学習データには MUSDB HQ のほか非公開データが含まれるとされます
- 商用利用: 不明(コードは MIT、重みは明文化なし)
- 再配布: 不明
- SHA-256: 重み `34c22ccb381c6f9fdbf324f04e1e2fe21aaaf293f5ded163a162697ff9a02ddd`、YAML `207405151270af8fd81c2373c25d27950916682ac91dca7884a11ce13dad6f58`
- Bunri 側での検証: あり。初回取得と既存キャッシュの両方を検証し、固定カタログからロードします

## 取得経路についての補足

- `guitar`、`vocals`、`htdemucs_6s` は Bunri 自身(`src/bunri/separate.py`)が上記の固定 URL から取得し、`load_model()` より前に全ファイルの SHA-256 を検証します。既存キャッシュも毎回検証し、不一致の場合は該当ファイルを削除してエラーにします。
- 登録済みモデルのロード中は対象モデルだけを返す固定カタログと、検証済み asset の存在だけを確認する downloader guard を使います。audio-separator の可変 `download_checks.json` は参照しません。
- 任意の未登録モデルを `--model` で指定した場合は固定対象外で、従来どおり audio-separator のカタログ・取得処理へ委譲します。
- `audio-separator` 自体は、UVR の MDX / Demucs モデルについて追加の `model_data` JSON も TRvlvr/application_data から取得します。

## クレジット

- becruily — Mel-Band Roformer guitar model
- Kimberley Jensen — Mel-Band Roformer vocals model
- Meta / Facebook Research — Demucs (htdemucs_6s)
- Anjok07 / TRvlvr — Ultimate Vocal Remover とその公開モデル配布
- nomadkaraoke — python-audio-separator
