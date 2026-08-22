"""Phase 0 スモークテスト。

依存関係が正しく解決・インストールされ、import できることだけを確認する。
重い初期化(モデルのロードなど)は行わない。
"""


def test_bunri_version() -> None:
    import bunri

    assert bunri.__version__ == "0.2.1"


def test_import_audio_separator() -> None:
    import audio_separator  # noqa: F401
    from audio_separator.separator import Separator  # noqa: F401


def test_import_torch() -> None:
    import torch  # noqa: F401


def test_import_soundfile() -> None:
    import soundfile  # noqa: F401


def test_import_jinja2() -> None:
    import jinja2  # noqa: F401


def test_import_numpy() -> None:
    import numpy  # noqa: F401
