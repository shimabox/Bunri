# Bunri — よく使う操作の入り口。詳しくは README.md を参照。

.PHONY: setup web test separate

# 前提チェック(uv / ffmpeg)+依存の導入。何度実行しても安全
setup:
	./setup.sh

# ブラウザ画面を起動(http://127.0.0.1:8330/ が自動で開く。停止は Ctrl+C)
web:
	uv run bunri-web

# コマンドラインで直接分離: make separate FILE=path/to/song.mp3
separate:
ifndef FILE
	$(error FILE を指定してください: make separate FILE=path/to/song.mp3)
endif
	uv run bunri "$(FILE)"

# 開発用: 全テスト実行
test:
	uv run pytest -q
