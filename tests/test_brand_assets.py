from __future__ import annotations

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from pressay.ui import APP_ICON_PATH, ASSET_DIRECTORY, make_icon


def test_primary_brand_asset_is_packaged_and_loadable(monkeypatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    image = QImage(str(APP_ICON_PATH))

    assert APP_ICON_PATH.is_file()
    assert not image.isNull()
    assert image.width() == 512
    assert image.height() == 512
    assert not make_icon(size=64).isNull()
    assert not make_icon("#22c55e", size=64).isNull()
    for name in ("logo-mark.svg", "wordmark.svg"):
        assert not QImage(str(ASSET_DIRECTORY / name)).isNull()
    app.processEvents()
