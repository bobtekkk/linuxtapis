"""Tapis for Linux: the tray icon, its menu, dialogs and app lifetime
(the Mac app's AppDelegate)."""

import os
import signal
import sys
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QRectF, Qt, QTimer, QUrl
from PyQt6.QtGui import (QAction, QColor, QDesktopServices, QGuiApplication, QIcon, QOffscreenSurface,
                         QOpenGLContext, QPainter, QPixmap, QSurfaceFormat)
from PyQt6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QLabel, QLineEdit, QMenu, QMessageBox,
                             QSystemTrayIcon, QVBoxLayout)

from . import __version__, cloth, store
from .audio import FabricSound
from .controller import Controller
from .icon_layer import IconLayer
from .icons import DesktopIcons
from .tr import tr
from .window import DesktopWindow

AUTOSTART = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "tapis.desktop"
ROOT = Path(__file__).resolve().parent.parent


def display_name(c) -> str:
    return tr("My Rugs") if c.name == "My Rugs" else c.name


def tray_icon() -> QIcon:
    """rectangle.checkered: a rounded frame with a checkerboard."""
    icon = QIcon()
    color = QApplication.palette().windowText().color()
    for size in (16, 22, 24, 32, 48, 64):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = size / 22.0
        r = QRectF(2.5 * s, 4.5 * s, 17 * s, 13 * s)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        cw, ch = r.width() / 4, r.height() / 3
        for j in range(3):
            for i in range(4):
                if (i + j) % 2 == 0:
                    p.drawRect(QRectF(r.x() + i * cw, r.y() + j * ch, cw, ch))
        pen = p.pen()
        from PyQt6.QtGui import QPen
        p.setPen(QPen(color, 1.4 * s))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, 2.2 * s, 2.2 * s)
        p.end()
        icon.addPixmap(pm)
    return icon


class SimContext:
    """An offscreen GL context, shared with every window, where the cloth
    is simulated and textures are uploaded."""

    def __init__(self):
        self.ctx = QOpenGLContext()
        self.ctx.setFormat(QSurfaceFormat.defaultFormat())
        self.ctx.setShareContext(QOpenGLContext.globalShareContext())
        if not self.ctx.create():
            raise RuntimeError("Tapis needs OpenGL 4.6")
        self.surface = QOffscreenSurface()
        self.surface.setFormat(self.ctx.format())
        self.surface.create()

    def make_current(self):
        if QOpenGLContext.currentContext() is not self.ctx:
            self.ctx.makeCurrent(self.surface)


_open_dialogs = []


def _show_dialog(d):
    """Shows a dialog without blocking anything else (a modal dialog would
    stop the rugs from taking clicks while it is open)."""
    d.setWindowModality(Qt.WindowModality.NonModal)
    d.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    d.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
    _open_dialogs.append(d)
    d.destroyed.connect(lambda *_: _open_dialogs.remove(d) if d in _open_dialogs else None)
    d.adjustSize()
    screen = QGuiApplication.screenAt(__import__("PyQt6.QtGui", fromlist=["QCursor"]).QCursor.pos()) \
        or QGuiApplication.primaryScreen()
    g = screen.availableGeometry()
    d.move(g.center().x() - d.width() // 2, g.center().y() - d.height() // 2)
    d.show()
    d.raise_()
    d.activateWindow()


def prompt(title, message, default, confirm, on_ok):
    """Asks for a name; calls on_ok(name) if confirmed with a non-empty name."""
    d = QDialog()
    d.setWindowTitle(title)
    lay = QVBoxLayout(d)
    lay.addWidget(QLabel(f"<b>{title}</b>"))
    if message:
        m = QLabel(message)
        m.setWordWrap(True)
        lay.addWidget(m)
    field = QLineEdit(default)
    field.setMinimumWidth(260)
    field.selectAll()
    lay.addWidget(field)
    bb = QDialogButtonBox()
    ok = bb.addButton(confirm, QDialogButtonBox.ButtonRole.AcceptRole)
    bb.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
    ok.setDefault(True)
    bb.accepted.connect(d.accept)
    bb.rejected.connect(d.reject)
    lay.addWidget(bb)

    def done(result):
        text = field.text().strip()
        if result == QDialog.DialogCode.Accepted and text:
            on_ok(text)
    d.finished.connect(done)
    _show_dialog(d)
    field.setFocus()


class TapisApp:
    def __init__(self, argv):
        fmt = QSurfaceFormat()
        fmt.setVersion(4, 6)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
        fmt.setAlphaBufferSize(8)
        fmt.setDepthBufferSize(24)
        fmt.setStencilBufferSize(8)
        fmt.setSwapInterval(1)
        QSurfaceFormat.setDefaultFormat(fmt)
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        self.app = QApplication(argv)
        self.app.setApplicationName("Tapis")
        self.app.setApplicationDisplayName("Tapis")
        self.app.setDesktopFileName("tapis")
        self.app.setQuitOnLastWindowClosed(False)
        if "--reset" in argv:
            p = store.config_dir() / "settings.json"
            if p.exists():
                p.unlink()
        self.prefs = store.Prefs()
        cloth.FEEL["corners_up"] = bool(self.prefs.get("cornersFoldUp", False))
        self.sim = SimContext()
        self.icons = DesktopIcons(1500)
        self.icon_layer = IconLayer(self.icons)
        self.sound = FabricSound(self.prefs)
        self.ctrl = Controller(self.prefs, self.icons, self.sound, self.sim)
        self.design_panel = None
        self.ctrl.designRequested.connect(self.show_design)
        self.ctrl.selectionChanged.connect(self._selection_changed)
        self.ctrl.layersChanged.connect(lambda: self._selection_changed(self.ctrl.selected_id))
        self.windows = []
        self.rebuild_windows()
        for sig in ("screenAdded", "screenRemoved", "primaryScreenChanged"):
            getattr(self.app, sig).connect(lambda *_: QTimer.singleShot(300, self.rebuild_windows))

        self.tray = QSystemTrayIcon(tray_icon())
        self.tray.setToolTip("Tapis")
        self.menu = QMenu()
        self.menu.setToolTipsVisible(True)
        self.menu.aboutToShow.connect(self.build_menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

        self.ctrl.start()
        self.icons.refresh()
        for a in argv:
            if a.startswith("--debug-script="):
                # debug: run a script with access to this app (used for automated checks)
                code = Path(a.split("=", 1)[1]).read_text()
                QTimer.singleShot(500, lambda: exec(code, {"app": self, "QTimer": QTimer}))
            if a.startswith("--snapshot="):
                # debug: after a few seconds, save what the first screen draws and quit
                path = a.split("=", 1)[1]
                delay = next((float(x.split("=")[1]) for x in argv if x.startswith("--after=")), 4.0)
                QTimer.singleShot(int(delay * 1000), lambda: self._snapshot(path))
        self.app.aboutToQuit.connect(self.shutdown)
        signal.signal(signal.SIGINT, lambda *_: self.app.quit())
        signal.signal(signal.SIGTERM, lambda *_: self.app.quit())
        self._sig_timer = QTimer()
        self._sig_timer.start(250)
        self._sig_timer.timeout.connect(lambda: None)   # lets Python handle signals

    # ------------------------------------------------------------------ windows
    def rebuild_windows(self):
        layout = [(s.name(), s.geometry().getRect(), s.devicePixelRatio()) for s in QGuiApplication.screens()]
        if getattr(self, "_layout", None) == layout:
            return
        self._layout = layout
        for w in self.windows:
            w.close()
            w.deleteLater()
        self.windows = []
        for s in QGuiApplication.screens():
            w = DesktopWindow(self.ctrl, s, self.icon_layer)
            w.place()
            self.windows.append(w)
        self.ctrl.windows = self.windows
        self.ctrl.redraw = True

    def _tray_activated(self, reason):
        # Left click opens the menu like the Mac menu bar item. KDE doesn't tell
        # apps where their tray icon is, but the pointer is on it right now;
        # Qt then keeps the menu on screen, against the panel.
        # (Handled here for right-click too: the position KDE passes for its own
        # context menu is in device pixels, which lands the menu off on scaled screens.)
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.Context):
            from PyQt6.QtGui import QCursor
            self.menu.popup(QCursor.pos())

    # ------------------------------------------------------------------ menu
    def build_menu(self):
        m, c = self.menu, self.ctrl
        m.clear()
        if not self.icons.available:
            a = m.addAction(tr("Refresh Desktop Icons"))
            a.triggered.connect(self.icons.refresh)
            m.addSeparator()
        a = m.addAction(tr("Add Rug"))
        a.setShortcut("Ctrl+N")
        a.triggered.connect(self.add_rug)
        a = m.addAction(tr("Show Rugs") if c.hidden else tr("Hide Rugs"))
        a.triggered.connect(lambda: c.set_hidden(not c.hidden))
        if c.any_below_icons() and not c.hidden:
            a = m.addAction(tr("Done Rearranging") if c.rearranging else tr("Rearrange Rugs"))
            a.triggered.connect(c.toggle_rearranging)
        m.addSeparator()
        if len(c.compositions) >= 9:
            container = m.addMenu(tr("Arrangements"))
        else:
            m.addSection(tr("Arrangements"))
            container = m
        for comp in c.compositions:
            a = container.addAction(display_name(comp))
            a.setCheckable(True)
            a.setChecked(comp.id == c.active_id)
            a.triggered.connect(lambda _=False, cid=comp.id: c.switch_to(cid))
        if container is not m:
            container.addSeparator()
        container.addAction(tr("New Arrangement…")).triggered.connect(self.new_composition)
        name = display_name(c.active_composition())
        manage = container.addMenu(tr("Manage “%@”").replace("%@", name))
        manage.addAction(tr("Duplicate…")).triggered.connect(self.duplicate_composition)
        manage.addAction(tr("Rename…")).triggered.connect(self.rename_composition)
        d = manage.addAction(tr("Delete…"))
        d.setEnabled(len(c.compositions) > 1)
        d.triggered.connect(self.delete_composition)
        m.addSeparator()
        a = m.addAction(tr("Fabric Sounds"))
        a.setCheckable(True)
        a.setChecked(self.sound.enabled)
        a.triggered.connect(self.sound.toggle)
        a = m.addAction(tr("Corners Fold Up"))
        a.setCheckable(True)
        a.setChecked(bool(self.prefs.get("cornersFoldUp", False)))
        a.setToolTip("Edges and corners curl up and lie on top, instead of folding down and under.")
        a.triggered.connect(self.toggle_corners)
        a = m.addAction(tr("Open at Login"))
        a.setCheckable(True)
        a.setChecked(AUTOSTART.exists())
        a.triggered.connect(self.toggle_login)
        m.addSeparator()
        # The Mac menu swaps About for "Refresh Desktop Icons" while ⌥ is held.
        if QGuiApplication.queryKeyboardModifiers() & Qt.KeyboardModifier.AltModifier:
            m.addAction(tr("Refresh Desktop Icons")).triggered.connect(self.icons.refresh)
        else:
            m.addAction(tr("About Tapis")).triggered.connect(self.about)
        a = m.addAction(tr("Quit Tapis"))
        a.setShortcut("Ctrl+Q")
        a.triggered.connect(self.app.quit)

    # ------------------------------------------------------------------ actions
    def add_rug(self):
        rug = self.ctrl.add_rug(save=True)
        self.ctrl.set_selection(rug.id)

    def new_composition(self):
        prompt(tr("New Arrangement"), tr("Starts with a single rug. Your current arrangement is kept."),
               store.unique_name(self.ctrl.compositions, tr("Arrangement")), tr("Create"),
               self.ctrl.new_composition)

    def duplicate_composition(self):
        dn = display_name(self.ctrl.active_composition())
        prompt(tr("Duplicate “%@”").replace("%@", dn),
               tr("Makes a copy of these rugs that you can change independently."),
               store.unique_name(self.ctrl.compositions, tr("%@ copy").replace("%@", dn)), tr("Duplicate"),
               self.ctrl.duplicate_composition)

    def rename_composition(self):
        dn = display_name(self.ctrl.active_composition())
        prompt(tr("Rename Arrangement"), "", dn, tr("Rename"), self.ctrl.rename_composition)

    def delete_composition(self):
        dn = display_name(self.ctrl.active_composition())
        d = QDialog()
        d.setWindowTitle(tr("Delete “%@”?").replace("%@", dn))
        lay = QVBoxLayout(d)
        lay.addWidget(QLabel(f"<b>{tr('Delete “%@”?').replace('%@', dn)}</b>"))
        m = QLabel(tr("Its rugs will be removed. This can't be undone."))
        m.setWordWrap(True)
        lay.addWidget(m)
        bb = QDialogButtonBox()
        delete = bb.addButton(tr("Delete"), QDialogButtonBox.ButtonRole.DestructiveRole)
        cancel = bb.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        cancel.setDefault(True)
        delete.clicked.connect(lambda: (d.close(), self.ctrl.delete_composition()))
        cancel.clicked.connect(d.close)
        lay.addWidget(bb)
        _show_dialog(d)

    def toggle_corners(self):
        on = not bool(self.prefs.get("cornersFoldUp", False))
        self.prefs.set("cornersFoldUp", on)
        cloth.FEEL["corners_up"] = on
        for r in self.ctrl.rugs:
            r.sim.wake()

    def toggle_login(self):
        if AUTOSTART.exists():
            AUTOSTART.unlink()
            return
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART.write_text(
            "[Desktop Entry]\nType=Application\nName=Tapis\nComment=A rug for your desktop\n"
            f"Exec={ROOT / 'tapis.sh'}\nIcon={ROOT / 'tapis' / 'icon.png'}\n"
            "X-GNOME-Autostart-enabled=true\nTerminal=false\n")

    def about(self):
        d = QDialog()
        d.setWindowTitle(tr("About Tapis"))
        lay = QVBoxLayout(d)
        lay.setContentsMargins(28, 22, 28, 18)
        lay.setSpacing(8)
        icon = ROOT / "tapis" / "icon.png"
        if icon.exists():
            pic = QLabel()
            pic.setPixmap(QPixmap(str(icon)).scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio,
                                                    Qt.TransformationMode.SmoothTransformation))
            pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(pic)
        for html in (f"<b style='font-size:15pt'>Tapis</b>", f"Version {__version__} for Linux",
                     "Original macOS app © 2026 Romain Lods, MIT License.",
                     "Idea: a tweet by Terkel (@terkelg).",
                     '<a href="https://rlods.github.io/tapis/">rlods.github.io/tapis</a>'):
            lbl = QLabel(html)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setOpenExternalLinks(True)
            lay.addWidget(lbl)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        bb.accepted.connect(d.close)
        lay.addWidget(bb)
        d.setMinimumWidth(320)
        _show_dialog(d)

    # ------------------------------------------------------------------ design panel
    def show_design(self, rid):
        rug = self.ctrl.rug_by_id(rid)
        if rug is None:
            return
        if self.design_panel is None:
            try:
                from .design_panel import DesignPanel
            except Exception:
                import traceback
                traceback.print_exc()
                return
            self.design_panel = DesignPanel()
            self.design_panel.designChanged.connect(self.ctrl.set_design)
        self.design_panel.show_for(rid, rug.design)

    def _selection_changed(self, rid):
        # The panel follows the selection to another rug, and closes when its rug is gone.
        panel = self.design_panel
        if panel is None or not panel.isVisible():
            return
        sel = self.ctrl.selected_rug()
        if sel is not None and sel.id != panel.rug_id:
            panel.show_for(sel.id, sel.design)
        elif sel is None and self.ctrl.rug_by_id(panel.rug_id) is None:
            panel.close_panel()

    def _snapshot(self, path):
        img = self.windows[0].grabFramebuffer()
        img.save(path)
        self.app.quit()

    # ------------------------------------------------------------------ lifetime
    def shutdown(self):
        self.ctrl.shutdown()
        self.sound.shutdown()

    def run(self) -> int:
        return self.app.exec()


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    return TapisApp(argv).run()
