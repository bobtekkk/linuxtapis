"""The "Rug Design" panel: a small floating tool window to change one rug's
pattern, colours and finish. Every edit is sent live to the rug.

Layout follows the Mac app's SwiftUI panel (DesignPanelController):
380 pt wide, 18 pt margins, three sections 16 pt apart:
  Pattern  - 3-column grid of the 8 styles (thumbnail + name), "Reset" -> Persian
  Colours  - 7 palette swatches (pick = all 5 colours), 5 colour wells, "Reset" -> Crimson
  Finish   - Fringe checkbox, Pile and Wear sliders (0..1), "Shuffle pattern" (new seed)
"""

import shutil
import threading
import uuid
from pathlib import Path

from PyQt6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPalette, QPen
from PyQt6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel,
                             QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget)

from . import store
from .palettes import PALETTES
from .tr import tr

STYLE_NAMES = {"persian": "Persian", "heriz": "Heriz", "kilim": "Kilim", "beniOurain": "Berber", "shag": "Shag",
               "stripes": "Stripes", "bordered": "Bordered", "image": "Image"}
WELLS = [("Field", "field"), ("Border", "border"), ("Accent", "accent"), ("Ivory", "light"), ("Ink", "dark")]

PANEL_WIDTH = 380
THUMB_W, THUMB_H = 192, 120      # pixels, shown at 96x60 pt (aspect 1.6) on the Mac
THUMB_ASPECT = 1.6


# ---------------------------------------------------------------- helpers

def _qcolor(rgb) -> QColor:
    r, g, b = rgb
    return QColor.fromRgbF(max(0.0, min(1.0, r)), max(0.0, min(1.0, g)), max(0.0, min(1.0, b)))


def _same_palette(a: dict, b: dict) -> bool:
    try:
        return all(abs(a[k][i] - b[k][i]) < 1e-4 for k in store.SLOTS for i in range(3))
    except Exception:
        return False


def _accent() -> QColor:
    return QApplication.palette().color(QPalette.ColorRole.Highlight)


def _primary() -> QColor:
    return QApplication.palette().color(QPalette.ColorRole.WindowText)


def _with_alpha(c: QColor, a: float) -> QColor:
    c = QColor(c)
    c.setAlphaF(a)
    return c


def _secondary() -> QColor:
    return _with_alpha(_primary(), 0.55)


def _font(delta: float = 0.0, bold: bool = False) -> QFont:
    f = QFont(QApplication.font())
    if f.pointSizeF() > 0:
        f.setPointSizeF(max(6.0, f.pointSizeF() + delta))
    f.setBold(bold)
    return f


def _fallback_thumb(design, w, h):
    """Plain preview used when tapis.patterns is not available: field with a
    border band and an accent line, or the picked image for the Image style."""
    if design.style == "image":
        if not design.image_path:
            return None
        src = QImage(design.image_path)
        if src.isNull():
            return None
        src = src.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                         Qt.TransformationMode.SmoothTransformation)
        return src.copy((src.width() - w) // 2, (src.height() - h) // 2, w, h)
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    pal = design.palette
    p = QPainter(img)
    p.fillRect(0, 0, w, h, _qcolor(pal["border"]))
    m = int(h * 0.14)
    p.fillRect(m, m, w - 2 * m, h - 2 * m, _qcolor(pal["field"]))
    p.setPen(QPen(_qcolor(pal["accent"]), max(1, h // 40)))
    p.drawRect(QRectF(m * 0.5, m * 0.5, w - m, h - m))
    p.setPen(QPen(_qcolor(pal["light"]), max(1, h // 60)))
    p.drawRect(QRectF(m + 3, m + 3, w - 2 * m - 6, h - 2 * m - 6))
    p.end()
    return img


def _render_thumb(design, w, h):
    try:
        from .patterns import thumbnail
    except Exception:
        thumbnail = None
    if thumbnail is not None:
        try:
            img = thumbnail(design, w, h)
            if img is not None and not img.isNull():
                return img
            if design.style == "image":
                return None
        except Exception:
            import traceback
            traceback.print_exc()
    try:
        return _fallback_thumb(design, w, h)
    except Exception:
        return None


class _ThumbBus(QObject):
    """Carries thumbnails from the worker thread back to the UI thread."""
    ready = pyqtSignal(int, str, object)     # token, style, QImage | None


# ---------------------------------------------------------------- small widgets

class _LinkButton(QPushButton):
    """The "Reset" link-style button in a section header."""

    def __init__(self, text):
        super().__init__(text)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFont(_font(-0.5))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def sizeHint(self):
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self.text()) + 4, fm.height() + 2)

    def paintEvent(self, e):
        p = QPainter(self)
        c = _accent() if self.isEnabled() else _with_alpha(_primary(), 0.3)
        if self.isDown():
            c = c.darker(130)
        p.setPen(c)
        p.setFont(self.font())
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.text())


class _StyleCell(QWidget):
    """One pattern in the grid: rounded thumbnail with a selection ring, name below."""
    clicked = pyqtSignal(str)

    def __init__(self, style):
        super().__init__()
        self.style_key = style
        self.image = None
        self.selected = False
        self._down = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip("")

    def _caption_h(self):
        return self.fontMetrics().height()

    def _thumb_h_for(self, w):
        return w / THUMB_ASPECT

    def sizeHint(self):
        w = 108
        return QSize(w, int(self._thumb_h_for(w) + 4 + self._caption_h()) + 2)

    def minimumSizeHint(self):
        return QSize(40, self.sizeHint().height())

    def set_image(self, img):
        self.image = img
        self.update()

    def set_selected(self, sel):
        if sel != self.selected:
            self.selected = sel
            self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._down = True
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._down:
            self._down = False
            self.update()
            if self.rect().contains(e.position().toPoint()):
                self.clicked.emit(self.style_key)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self._down:
            p.setOpacity(0.7)
        w = self.width()
        th = self._thumb_h_for(w - 3)
        r = QRectF(1.5, 1.5, w - 3, th)
        path = QPainterPath()
        path.addRoundedRect(r, 5, 5)
        p.save()
        p.setClipPath(path)
        if self.image is not None and not self.image.isNull():
            # fill (aspect-preserving crop) like Image.resizable() in a 1.6 frame
            iw, ih = self.image.width(), self.image.height()
            s = max(r.width() / iw, r.height() / ih)
            dw, dh = iw * s, ih * s
            p.drawImage(QRectF(r.center().x() - dw / 2, r.center().y() - dh / 2, dw, dh), self.image)
        else:
            p.fillRect(r, _with_alpha(QColor(128, 128, 128), 0.2))
        p.restore()
        if self.selected:
            p.setPen(QPen(_accent(), 2.5))
        else:
            p.setPen(QPen(_with_alpha(_primary(), 0.1), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 5, 5)
        p.setOpacity(1.0 if not self._down else 0.7)
        p.setFont(self.font())
        p.setPen(_primary() if self.selected else _secondary())
        p.drawText(QRectF(0, r.bottom() + 4, w, self._caption_h() + 2),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, tr(STYLE_NAMES[self.style_key]))


class _PaletteSwatch(QWidget):
    """A 40x22 swatch of a built-in palette (field | border | ivory bands)."""
    clicked = pyqtSignal(int)

    def __init__(self, index, name, colours):
        super().__init__()
        self.index = index
        self.colours = colours
        self.selected = False
        self._down = False
        self.setFixedSize(40 + 4, 22 + 4)
        self.setToolTip(tr(name))
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_selected(self, sel):
        if sel != self.selected:
            self.selected = sel
            self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._down = True
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._down:
            self._down = False
            self.update()
            if self.rect().contains(e.position().toPoint()):
                self.clicked.emit(self.index)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._down:
            p.setOpacity(0.7)
        r = QRectF(2, 2, 40, 22)
        path = QPainterPath()
        path.addRoundedRect(r, 5, 5)
        p.save()
        p.setClipPath(path)
        bands = [self.colours["field"], self.colours["border"], self.colours["light"]]
        bw = r.width() / 3
        for i, c in enumerate(bands):
            p.fillRect(QRectF(r.x() + i * bw, r.y(), bw + 0.5, r.height()), _qcolor(c))
        p.restore()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(_accent(), 2) if self.selected else QPen(_with_alpha(_primary(), 0.15), 1))
        p.drawRoundedRect(r, 5, 5)


class _ColourWell(QWidget):
    """A colour well: shows the slot's colour; click opens a colour chooser
    that updates the rug live while you pick."""
    picked = pyqtSignal(str, object)       # slot, (r, g, b)

    def __init__(self, slot, label):
        super().__init__()
        self.slot = slot
        self.label = label
        self.colour = (0.0, 0.0, 0.0)
        self._down = False
        self._dialog = None
        self.setFixedSize(44, 24)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(tr(label))

    def set_colour(self, rgb):
        self.colour = tuple(rgb)
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._down = True
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._down:
            self._down = False
            self.update()
            if self.rect().contains(e.position().toPoint()):
                self.open_dialog()

    def open_dialog(self):
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        start = self.colour
        dlg = QColorDialog(_qcolor(start), self.window())
        dlg.setWindowTitle(tr(self.label))
        dlg.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        def live(c):
            if c.isValid():
                rgb = (c.redF(), c.greenF(), c.blueF())
                self.set_colour(rgb)
                self.picked.emit(self.slot, rgb)

        def finished(result):
            if result != QColorDialog.DialogCode.Accepted and tuple(self.colour) != tuple(start):
                self.set_colour(start)
                self.picked.emit(self.slot, start)
            self._dialog = None

        dlg.currentColorChanged.connect(live)
        dlg.colorSelected.connect(live)
        dlg.finished.connect(finished)
        self._dialog = dlg
        dlg.open()

    def close_dialog(self):
        if self._dialog is not None:
            d, self._dialog = self._dialog, None
            try:
                d.finished.disconnect()
            except Exception:
                pass
            d.close()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # bezel like the macOS colour well
        outer = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.setPen(QPen(_with_alpha(_primary(), 0.25), 1))
        p.setBrush(QApplication.palette().color(QPalette.ColorRole.Button))
        if self._down:
            p.setBrush(QApplication.palette().color(QPalette.ColorRole.Button).darker(115))
        p.drawRoundedRect(outer, 5, 5)
        inner = outer.adjusted(4, 4, -4, -4)
        p.setPen(QPen(_with_alpha(_primary(), 0.2), 1))
        p.setBrush(_qcolor(self.colour))
        p.drawRoundedRect(inner, 2.5, 2.5)


# ---------------------------------------------------------------- the panel

class DesignPanel(QWidget):
    designChanged = pyqtSignal(str, object)   # (rug_id, new RugDesign), emitted live on every edit

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setWindowTitle(tr("Rug Design"))
        self.rug_id = None
        self.design = store.RugDesign()
        self._placed = False
        self._updating = False
        self._thumbs = {}
        self._thumb_key = None
        self._token = 0
        self._bus = _ThumbBus()
        self._bus.ready.connect(self._thumb_ready)
        self._thumb_timer = QTimer(self)
        self._thumb_timer.setSingleShot(True)
        self._thumb_timer.setInterval(90)
        self._thumb_timer.timeout.connect(self._start_thumbnails)
        self._build()

    # -------------------------------------------------------- building
    def _section(self, title, reset=None):
        box = QVBoxLayout()
        box.setSpacing(8)
        box.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(tr(title))
        lab.setFont(_font(0, bold=True))
        head.addWidget(lab)
        head.addStretch(1)
        btn = None
        if reset is not None:
            btn = _LinkButton(tr("Reset"))
            btn.clicked.connect(reset)
            head.addWidget(btn)
        box.addLayout(head)
        self._root.addLayout(box)
        return box, btn

    def _build(self):
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(18, 18, 18, 18)
        self._root.setSpacing(16)

        # Pattern
        box, self._pattern_reset = self._section("Pattern", self._reset_pattern)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self._cells = {}
        cap = _font(-2)
        for i, style in enumerate(store.STYLES):
            cell = _StyleCell(style)
            cell.setFont(cap)
            cell.clicked.connect(self._select_style)
            grid.addWidget(cell, i // 3, i % 3)
            self._cells[style] = cell
        for c in range(3):
            grid.setColumnStretch(c, 1)
        box.addLayout(grid)

        # Colours
        box, self._colours_reset = self._section("Colours", self._reset_palette)
        row = QHBoxLayout()
        row.setSpacing(8 - 4)                  # swatches carry 2 px of ring room on each side
        self._swatches = []
        for i, (name, cols) in enumerate(PALETTES):
            sw = _PaletteSwatch(i, name, cols)
            sw.clicked.connect(self._select_palette)
            row.addWidget(sw)
            self._swatches.append(sw)
        row.addStretch(1)
        box.addLayout(row)
        wells = QHBoxLayout()
        wells.setSpacing(14)
        self._wells = {}
        small = _font(-3)
        for label, slot in WELLS:
            col = QVBoxLayout()
            col.setSpacing(3)
            well = _ColourWell(slot, label)
            well.picked.connect(self._set_colour)
            col.addWidget(well, 0, Qt.AlignmentFlag.AlignHCenter)
            lab = QLabel(tr(label))
            lab.setFont(small)
            pal = lab.palette()
            pal.setColor(QPalette.ColorRole.WindowText, _secondary())
            lab.setPalette(pal)
            lab.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(lab, 0, Qt.AlignmentFlag.AlignHCenter)
            wells.addLayout(col)
            self._wells[slot] = well
        wells.addStretch(1)
        box.addLayout(wells)

        # Finish
        box, _ = self._section("Finish")
        self._fringe = QCheckBox(tr("Fringe"))
        self._fringe.toggled.connect(self._set_fringe)
        box.addWidget(self._fringe)
        sliders = QGridLayout()
        sliders.setHorizontalSpacing(8)
        sliders.setVerticalSpacing(6)
        self._pile = self._slider(sliders, 0, "Pile", self._set_pile)
        self._wear = self._slider(sliders, 1, "Wear", self._set_wear)
        box.addLayout(sliders)
        shuffle = QPushButton(tr("Shuffle pattern"))
        shuffle.clicked.connect(self._shuffle)
        shuffle.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        box.addWidget(shuffle, 0, Qt.AlignmentFlag.AlignLeft)

        self.setFixedWidth(PANEL_WIDTH)

    def _slider(self, grid, row, label, slot):
        lab = QLabel(tr(label))
        s = QSlider(Qt.Orientation.Horizontal)
        s.setRange(0, 1000)                   # continuous 0...1
        s.setSingleStep(10)
        s.setPageStep(100)
        s.valueChanged.connect(lambda v: slot(v / 1000.0))
        grid.addWidget(lab, row, 0)
        grid.addWidget(s, row, 1)
        grid.setColumnStretch(1, 1)
        return s

    # -------------------------------------------------------- public API
    def show_for(self, rug_id: str, design) -> None:
        new_rug = rug_id != self.rug_id
        for w in self._wells.values():
            if new_rug:
                w.close_dialog()
        self.rug_id = rug_id
        self.design = design.copy() if hasattr(design, "copy") else store.RugDesign.from_json(design.to_json())
        self._refresh()
        self._schedule_thumbnails(immediate=True)
        self.adjustSize()
        self.setFixedHeight(self.sizeHint().height())
        if not self._placed:
            self._placed = True
            scr = QApplication.primaryScreen()
            if scr is not None:
                g = scr.availableGeometry()
                self.move(g.center().x() - PANEL_WIDTH // 2, g.center().y() - self.height() // 2)
        self.show()
        self.raise_()
        self.activateWindow()

    def close_panel(self) -> None:
        for w in self._wells.values():
            w.close_dialog()
        self.hide()

    def closeEvent(self, e):
        for w in self._wells.values():
            w.close_dialog()
        super().closeEvent(e)

    # -------------------------------------------------------- state -> widgets
    def _refresh(self):
        d = self.design
        self._updating = True
        try:
            for style, cell in self._cells.items():
                cell.set_selected(d.style == style)
            self._pattern_reset.setEnabled(d.style != "persian")
            self._colours_reset.setEnabled(not _same_palette(d.palette, store.DEFAULT_PALETTE))
            for sw in self._swatches:
                sw.set_selected(_same_palette(sw.colours, d.palette))
            for slot, well in self._wells.items():
                well.set_colour(d.palette[slot])
            self._fringe.setChecked(bool(d.fringe))
            self._pile.setValue(round(max(0.0, min(1.0, d.pile)) * 1000))
            self._wear.setValue(round(max(0.0, min(1.0, d.wear)) * 1000))
        finally:
            self._updating = False

    def _apply(self, old):
        """Send the new design out and refresh; thumbnails only depend on
        palette, seed, fringe and image (not style, wear or pile)."""
        self._refresh()
        if self.rug_id is not None:
            self.designChanged.emit(self.rug_id, self.design.copy())
        d = self.design
        if (not _same_palette(d.palette, old.palette) or d.seed != old.seed or d.fringe != old.fringe
                or d.image_path != old.image_path):
            self._schedule_thumbnails()

    # -------------------------------------------------------- edits
    def _select_style(self, style):
        d = self.design
        if style == "image" and (not d.image_path or d.style == "image"):
            self._pick_image()
            return
        old = d.copy()
        d.style = style
        if style == "shag" and d.pile < 0.4:
            d.pile = 0.85
        self._apply(old)

    def _pick_image(self):
        exts = "*.png *.jpg *.jpeg *.gif *.bmp *.webp *.tif *.tiff *.heic *.heif *.avif *.svg"
        path, _ = QFileDialog.getOpenFileName(self, tr("Image"), str(Path.home() / "Pictures")
                                              if (Path.home() / "Pictures").is_dir() else str(Path.home()),
                                              f"{tr('Image')} ({exts})")
        self.raise_()
        self.activateWindow()
        if not path:
            return
        try:
            src = Path(path)
            dst_dir = store.config_dir() / "images"
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / (str(uuid.uuid4()).upper() + (src.suffix if src.suffix else ""))
            shutil.copyfile(src, dst)
        except Exception:
            QApplication.beep()
            return
        old = self.design.copy()
        self.design.image_path = str(dst)
        self.design.style = "image"
        self._apply(old)

    def _reset_pattern(self):
        old = self.design.copy()
        self.design.style = "persian"
        self._apply(old)

    def _select_palette(self, index):
        old = self.design.copy()
        self.design.palette = dict(PALETTES[index][1])
        self._apply(old)

    def _reset_palette(self):
        old = self.design.copy()
        self.design.palette = dict(store.DEFAULT_PALETTE)
        self._apply(old)

    def _set_colour(self, slot, rgb):
        old = self.design.copy()
        self.design.palette = dict(self.design.palette)
        self.design.palette[slot] = tuple(float(x) for x in rgb)
        self._apply(old)

    def _set_fringe(self, on):
        if self._updating:
            return
        old = self.design.copy()
        self.design.fringe = bool(on)
        self._apply(old)

    def _set_pile(self, v):
        if self._updating:
            return
        old = self.design.copy()
        self.design.pile = v
        self._apply(old)

    def _set_wear(self, v):
        if self._updating:
            return
        old = self.design.copy()
        self.design.wear = v
        self._apply(old)

    def _shuffle(self):
        old = self.design.copy()
        self.design.seed = store.random_seed()
        self._apply(old)

    # -------------------------------------------------------- thumbnails
    def _schedule_thumbnails(self, immediate=False):
        if immediate:
            self._thumb_timer.stop()
            self._start_thumbnails()
        else:
            self._thumb_timer.start()

    def _start_thumbnails(self):
        d = self.design
        key = (tuple(tuple(round(c, 5) for c in d.palette[k]) for k in store.SLOTS), d.seed, d.fringe, d.image_path)
        if key == self._thumb_key:
            return
        self._thumb_key = key
        self._token += 1
        token = self._token
        snapshot = d.copy()
        bus = self._bus
        alive = lambda: self._token == token   # noqa: E731  (int read is thread-safe)

        def work():
            for style in store.STYLES:
                if not alive():
                    return
                dd = snapshot.copy()
                dd.style = style
                img = _render_thumb(dd, THUMB_W, THUMB_H)
                if not alive():
                    return
                bus.ready.emit(token, style, img)

        threading.Thread(target=work, name="tapis-thumbs", daemon=True).start()

    def _thumb_ready(self, token, style, img):
        if token != self._token:
            return
        self._thumbs[style] = img
        cell = self._cells.get(style)
        if cell is not None:
            cell.set_image(img)
