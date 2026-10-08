"""Desktop icons drawn on top of rugs that lie "below icons".

On the Mac such a rug sits in a window between the wallpaper and Finder's
icons. KDE Plasma draws wallpaper and icons in one window, so nothing can go
in between; instead the icons are redrawn here, over the rug only (where
there is no rug the real icons show as usual, and clicks always reach them).
"""

import configparser
from pathlib import Path

from PyQt6.QtCore import QMimeDatabase, QPointF, QRectF, QStandardPaths, Qt
from PyQt6.QtGui import QColor, QFont, QIcon, QImage, QImageReader, QPainter, QPixmap, QTextOption

_IMAGE_TYPES = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff"}


def _desktop_dir() -> Path:
    return Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation))


def _find(name: str):
    """The file behind an icon (its label may be a .desktop file's Name)."""
    d = _desktop_dir()
    p = d / name
    if p.exists():
        return p, name
    for f in d.glob("*.desktop"):
        try:
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            cp.read(f, encoding="utf-8")
            if cp.get("Desktop Entry", "Name", fallback=None) == name:
                return f, name
        except Exception:
            continue
    return None, name


def _icon_image(path: Path | None, size: int) -> QImage | None:
    if path is None:
        return QIcon.fromTheme("unknown").pixmap(size, size).toImage()
    if path.is_dir():
        return QIcon.fromTheme("folder").pixmap(size, size).toImage()
    if path.suffix.lower().lstrip(".") in _IMAGE_TYPES:
        r = QImageReader(str(path))
        r.setAutoTransform(True)
        s = r.size()
        if s.isValid() and s.width() > 0 and s.height() > 0:
            k = size / max(s.width(), s.height())
            r.setScaledSize(s * k)
        img = r.read()
        if not img.isNull():
            return img
    if path.suffix == ".desktop":
        try:
            cp = configparser.ConfigParser(interpolation=None, strict=False)
            cp.read(path, encoding="utf-8")
            icon = cp.get("Desktop Entry", "Icon", fallback="")
            ic = QIcon(icon) if icon.startswith("/") else QIcon.fromTheme(icon)
            if not ic.isNull():
                return ic.pixmap(size, size).toImage()
        except Exception:
            pass
    mt = QMimeDatabase().mimeTypeForFile(str(path))
    ic = QIcon.fromTheme(mt.iconName(), QIcon.fromTheme(mt.genericIconName(), QIcon.fromTheme("unknown")))
    return ic.pixmap(size, size).toImage()


class IconLayer:
    def __init__(self, icons):
        self.icons = icons
        self._cache_key = None
        self._images = []
        self._layers = {}

    def _prepare(self, dpr):
        key = (tuple(self.icons.items), dpr)
        if key == self._cache_key:
            return
        self._cache_key = key
        self._images = []
        for name, (x, y, w, h), label in self.icons.items:
            path, text = _find(name)
            img_h = (label[1] - y) if label else min(w, h)
            size = int(max(16, min(64, img_h - 8)))
            img = _icon_image(path, int(size * dpr))
            if img is not None:
                img.setDevicePixelRatio(dpr)
            self._images.append((QRectF(x + w / 2 - size / 2, y + (img_h - size) / 2, size, size), img,
                                 QRectF(*label) if label else None, text))

    def _layer(self, window) -> QImage:
        """All icons for this window, pre-drawn (labels rendered in software)."""
        dpr = window.devicePixelRatio()
        self._prepare(dpr)
        key = (self._cache_key, window.width(), window.height(), tuple(window.origin))
        cached = self._layers.get(id(window))
        if cached is not None and cached[0] == key:
            return cached[1]
        img = QImage(int(window.width() * dpr), int(window.height() * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        img.setDevicePixelRatio(dpr)
        img.fill(Qt.GlobalColor.transparent)
        ox, oy = window.origin
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        opt = QTextOption(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        p.setFont(QFont())
        for rect, icon, label, text in self._images:
            r = rect.translated(-ox, -oy)
            if icon is not None and not icon.isNull():
                sz = icon.deviceIndependentSize()
                p.drawImage(QRectF(r.center().x() - sz.width() / 2, r.center().y() - sz.height() / 2,
                                   sz.width(), sz.height()), icon)
            if label is not None:
                lr = label.translated(-ox, -oy)
                shown = text[:-8] if text.endswith(".desktop") else text
                for dx, dy, a in ((0, 1, 150), (1, 1, 70), (-1, 1, 70), (0, 2, 40)):
                    p.setPen(QColor(0, 0, 0, a))
                    p.drawText(lr.translated(dx, dy), shown, opt)
                p.setPen(QColor(255, 255, 255))
                p.drawText(lr, shown, opt)
        p.end()
        self._layers[id(window)] = (key, img)
        return img

    def draw(self, window, rugs):
        """Paints the icons where the already-drawn (below-icons) rugs are."""
        if not self.icons.items:
            return
        img = self._layer(window)
        p = QPainter(window)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceAtop)
        p.drawImage(QPointF(0, 0), img)
        p.end()
