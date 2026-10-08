"""One transparent desktop-layer window per screen. It draws the rugs (the
"below icons" layer, then the "above icons" layer) and the overlay, and
takes the mouse only while the pointer is over a rug, a handle or the
toolbar; everywhere else clicks fall through to the desktop."""

import time

import numpy as np
from OpenGL import GL
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QPainter
from PyQt6.QtOpenGL import QOpenGLWindow
from PyQt6.QtWidgets import QToolTip

from . import gl, overlay, platform_x11
from .renderer import Renderer


class DesktopWindow(QOpenGLWindow):
    def __init__(self, controller, screen, icon_layer=None):
        super().__init__(QOpenGLWindow.UpdateBehavior.NoPartialUpdate)
        self.ctrl = controller
        self.icon_layer = icon_layer
        self.setScreen(screen)
        self.setFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnBottomHint
                      | Qt.WindowType.WindowDoesNotAcceptFocus | Qt.WindowType.NoDropShadowWindowHint)
        self.setTitle("Tapis")
        self.setGeometry(screen.geometry())
        self.origin = np.array([screen.geometry().x(), screen.geometry().y()], np.float64)
        self.renderer = None
        self.wants_mouse = None
        self.pill_hover = None
        self.pill_pressed = None
        self.pill_press_target = None
        self.hint_rect = None
        self.hint_start = 0.0
        self.hint_timer = QTimer(self)
        self.hint_timer.setInterval(16)
        self.hint_timer.timeout.connect(self._hint_tick)
        self.tip_timer = QTimer(self)
        self.tip_timer.setSingleShot(True)
        self.tip_timer.setInterval(700)
        self.tip_timer.timeout.connect(self._show_tip)

    # ------------------------------------------------------------------ platform
    def place(self):
        self.show()
        wid = int(self.winId())
        platform_x11.make_desktop_layer(wid)
        self.set_wants_mouse(False)

    def set_wants_mouse(self, wants: bool):
        if wants == self.wants_mouse:
            return
        self.wants_mouse = wants
        dpr = self.devicePixelRatio()
        w, h = int(self.width() * dpr) + 1, int(self.height() * dpr) + 1
        platform_x11.set_input_region(int(self.winId()), [(0, 0, w, h)] if wants else [])
        if not wants:
            self.pill_hover = None
            QToolTip.hideText()

    def set_visible_layer(self, visible: bool):
        self.update()

    # ------------------------------------------------------------------ geometry
    def world(self, pos) -> np.ndarray:
        return self.origin + np.array([pos.x(), pos.y()], np.float64)

    def pill_rect(self):
        """The toolbar's rect in window coordinates, or None."""
        c = self.ctrl
        r = c.selected_rug()
        if r is None or c.hidden:
            return None
        mn, mx = r.bounds(0)
        w, h = overlay.pill_size()
        bw, bh = self.width(), self.height()
        O = self.origin
        x = (mn[0] + mx[0]) / 2 - O[0] - w / 2
        y = mx[1] - O[1] + 22
        if bh - 12 < y + h:
            y = mn[1] - O[1] - h - 22 - 34
        x = min(max(x, 12), bw - w - 12)
        y = min(max(y, 12), bh - h - 12)
        visible = mx[0] > O[0] and mn[0] < O[0] + bw and O[1] < mx[1] and mn[1] < O[1] + bh
        return QRectF(x, y, w, h) if visible else None

    def pill_contains(self, p) -> bool:
        r = self.pill_rect()
        if r is None:
            return False
        q = np.asarray(p) - self.origin
        return r.adjusted(-4, -4, 4, 4).contains(QPointF(q[0], q[1]))

    def _pill_button_at(self, pos):
        r = self.pill_rect()
        if r is None or not r.contains(pos):
            return None
        for name, br in overlay.button_rects(r).items():
            if br.contains(pos):
                return name
        return None

    # ------------------------------------------------------------------ GL
    def initializeGL(self):
        self.renderer = Renderer()

    def resizeGL(self, w, h):
        pass

    def paintGL(self):
        c = self.ctrl
        dpr = self.devicePixelRatio()
        W, H = max(int(self.width() * dpr), 1), max(int(self.height() * dpr), 1)
        size = (float(self.width()), float(self.height()))
        fbo = self.defaultFramebufferObject()
        fence = getattr(c, "frame_fence", None)
        if fence is not None:
            gl.fast().wait_sync(fence)
        below = c.visible_rugs(True, self.origin, size)
        above = c.visible_rugs(False, self.origin, size)
        self.renderer.draw_group(below, self.origin, size, (W, H), fbo, clear_color=True)
        if below and self.icon_layer is not None and not c.rearranging and not c.hidden:
            self.icon_layer.draw(self, below)
        self.renderer.draw_group(above, self.origin, size, (W, H), fbo, clear_color=False)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
        self._paint_overlay()

    def _paint_overlay(self):
        c = self.ctrl
        sel = c.selected_rug()
        pill = self.pill_rect()
        hint_alpha = self._hint_alpha()
        if sel is None and hint_alpha <= 0:
            return
        p = QPainter(self)
        if sel is not None and not c.hidden:
            overlay.draw_selection(p, sel, self.origin, c.hover_handle)
            if pill is not None:
                overlay.draw_pill(p, pill, sel.below_icons, self.pill_hover, self.pill_pressed,
                                  getattr(sel, "restore", None) is not None)
        if hint_alpha > 0 and self.hint_rect is not None:
            overlay.draw_hint(p, self.hint_rect, hint_alpha)
        p.end()

    # ------------------------------------------------------------------ hint
    def show_hint(self, p):
        q = np.asarray(p) - self.origin
        if not (0 <= q[0] < self.width() and 0 <= q[1] < self.height()):
            return
        w, h = overlay.hint_size()
        x = min(max(q[0] - w / 2, 12), self.width() - w - 12)
        y = max(q[1] - h - 28, 12)
        self.hint_rect = QRectF(x, y, w, h)
        self.hint_start = time.monotonic()
        self.hint_timer.start()

    def _hint_alpha(self):
        if self.hint_rect is None:
            return 0.0
        t = time.monotonic() - self.hint_start
        if t < 0.15:
            return t / 0.15
        if t < 2.15:
            return 1.0
        if t < 2.45:
            return 1.0 - (t - 2.15) / 0.3
        return 0.0

    def _hint_tick(self):
        if self._hint_alpha() <= 0 and time.monotonic() - self.hint_start > 2.5:
            self.hint_rect = None
            self.hint_timer.stop()
        self.update()

    def cancel_hint(self):
        if self.hint_rect is not None:
            self.hint_rect = None
            self.hint_timer.stop()
            self.update()

    # ------------------------------------------------------------------ mouse
    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        QToolTip.hideText()
        self.tip_timer.stop()
        b = self._pill_button_at(e.position())
        if b is not None:
            self.pill_pressed = self.pill_press_target = b
            self.update()
            return
        if self.pill_contains(self.world(e.position())):
            return
        self.ctrl.mouse_down(self.world(e.position()), 1)

    def mouseDoubleClickEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._pill_button_at(e.position()) is not None:
            return self.mousePressEvent(e)
        self.ctrl.mouse_down(self.world(e.position()), 2)

    def mouseMoveEvent(self, e):
        if self.pill_press_target is not None:
            now = self._pill_button_at(e.position())
            pressed = self.pill_press_target if now == self.pill_press_target else None
            if pressed != self.pill_pressed:
                self.pill_pressed = pressed
                self.update()
            return
        if e.buttons() & Qt.MouseButton.LeftButton:
            self.ctrl.mouse_dragged(self.world(e.position()),
                                    bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            return
        hover = self._pill_button_at(e.position())
        if hover != self.pill_hover:
            self.pill_hover = hover
            QToolTip.hideText()
            self.tip_timer.start() if hover else self.tip_timer.stop()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self.pill_press_target is not None:
            target, self.pill_press_target = self.pill_press_target, None
            hit = self._pill_button_at(e.position()) == target
            self.pill_pressed = None
            self.update()
            if hit:
                self._pill_action(target)
            return
        self.ctrl.mouse_up(self.world(e.position()))

    def _show_tip(self):
        r = self.pill_rect()
        sel = self.ctrl.selected_rug()
        if self.pill_hover is None or r is None or sel is None:
            return
        br = overlay.button_rects(r)[self.pill_hover]
        tip = overlay.pill_tooltips(sel.below_icons, getattr(sel, "restore", None) is not None)[self.pill_hover]
        QToolTip.showText(self.mapToGlobal(br.bottomLeft().toPoint()), tip)

    def _pill_action(self, name):
        c = self.ctrl
        r = c.selected_rug()
        if r is None:
            return
        if name == "design":
            c.designRequested.emit(r.id)
        elif name == "smooth":
            c.smooth(r)
        elif name == "rotate":
            c.start_spin(r)
        elif name == "fill":
            c.fill_screen(r)
        elif name == "layer":
            c.set_below_icons(r, not r.below_icons)
        elif name == "remove":
            c.remove_rug(r)
        c.overlay_dirty = True
