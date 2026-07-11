# プレイヤー波形表示+クリックシーク 実装プラン(未着手・Phase 3 で計画のみ)

2026-07-11 起草。ユーザー判断は「やるかもしれないのでプランニングだけ」。

## 制約(前提)

- プレイヤーは file:// で開かれる単一 HTML。**fetch() / Web Audio の
  decodeAudioData は null origin で使えない**(player.py の docstring 参照)
  → ブラウザ内で音声データから波形を計算することは不可能
- 外部リソース参照は音声ファイルのみに保ちたい(HTML は自己完結)

## 方式: パッケージ生成時に Python で波形を事前計算し、インライン SVG で埋め込む

創設プランは「波形 PNG を data URI で」としていたが、**SVG polyline の方が有利**:

- 追加依存ゼロ(PNG 化には Pillow 等が要る。SVG は文字列生成のみ)
- 拡大・リサイズで劣化しない(プレイヤーの幅は可変)
- CSS でテーマ色を当てられる(再生済み部分の色分けが clip-path だけで済む)
- サイズも実用範囲: 2,000 点のピーク列で ~30KB(gzip 前)。PNG と同等以下

### パイプライン側(Python)

1. `src/stemlab/waveform.py`(新規):
   `waveform_peaks(wav_path, n_bins=2000) -> list[tuple[float, float]]`
   - soundfile でストリーム読み(blocks)、bin ごとに min/max ピークを取る
     (全読みでも 40MB/曲なので許容だが、blocks 読みならメモリ一定)
   - 正規化して [-1, 1]
2. `package.py`: プレイヤー描画前に **target / backing / original(input.wav)
   の 3 トラック分**のピーク列を計算し、`render_player(..., waveforms={...})` に渡す
   - 計算コスト目安: 数百 ms/トラック(sha1 digest と同オーダーの I/O)
   - キャッシュ不要(分離に比べ誤差レベル)。プレイヤー再生成は毎回走る設計のまま
3. `player.py`: ピーク列→ `<polygon>` 1 個の points 文字列に変換して
   テンプレートに渡す(上下対称のエンベロープ多角形)

### プレイヤー側(JS/CSS)

- トラックごとに `<svg viewBox="0 0 2000 200">` を重ね、表示中トラックのものを表示
  (ミキサー導入後は「選択中トラック or original」の 1 本表示で開始が簡潔)
- 再生位置: SVG の上に絶対配置の再生ヘッド(div)を `timeupdate` で移動。
  再生済み領域の色分けは `clip-path: inset()` で SVG を二層重ねる
- クリックシーク: コンテナの click で `offsetX / width * duration` を全トラックの
  currentTime に設定(既存の seek 同期ロジックを流用)
- AB ループ区間の可視化: 波形上に半透明帯を重ねる(既存 loop state を参照)

## テスト計画

- Python 側: 既知波形(正弦波・無音)でピーク列の値・bin 数・正規化を検証
- Playwright 側(既存の file:// ハーネスに追加):
  - SVG が描画される/クリックで currentTime が比例位置に飛ぶ
  - 再生ヘッドが timeupdate で動く(fake でなく実再生)
- 既存テスト「外部リソースなし」が SVG インライン化を自然に検証

## 見積もり

- Python 側 ~100 行+テスト、プレイヤー側 ~120 行(JS/CSS/SVG)
- 実装担当: opus(ブラウザ実証必須)、レビュー: Fable
- リスク: 波形と <audio> の duration のズレ(mp3 の VBR ヘッダ起因で ±0.1s 程度)
  → シークは比例計算なので実害なし。表示終端の見切れだけ確認する
