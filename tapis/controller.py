"""The rug controller: the 60 Hz loop, mouse gestures, selection, layers,
arrangements and saving — the Mac app's RugController, gesture for gesture."""

import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from OpenGL import GL
from PyQt6.QtCore import QObject, QPoint, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor, QGuiApplication

from . import gl, store
from .rug import Rug, rot
from .store import Composition, RugState

DT = 1 / 60
CAM_D = 2200.0
MIN_SIZE = (140.0, 90.0)
CORNER_SIGNS = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
NO_BUMPS = dict(icons=[], height=0.0, half=(0.0, 0.0), radius=0.0, soft=1.0, offset=(0.0, 0.0))


def project(q, c):
    return c + (q[..., :2] - c) * (CAM_D / (CAM_D - q[..., 2:3]))


def default_size(screen_size):
    """A new rug's size: half the screen wide (at most 900 pt), 1.6 : 1."""
    w = min(0.5 * screen_size[0], 900.0)
    return w, math.floor(w / 1.6 + 0.5)


def overlap(a, b, m):
    (amin, amax), (bmin, bmax) = a, b
    return (amin[0] - m < bmax[0] + m and bmin[0] - m < amax[0] + m and
            amin[1] - m < bmax[1] + m and bmin[1] - m < amax[1] + m)


def _render_texture(design, size, w, h):
    try:
        from . import patterns
        img = patterns.render(design, size, w, h)
        return bytes(img.constBits().asstring(img.sizeInBytes())), img.width(), img.height()
    except Exception:
        import traceback
        traceback.print_exc()
        r, g, b = design.palette["field"]
        px = np.empty((h, w, 4), np.uint8)
        px[...] = (int(r * 255), int(g * 255), int(b * 255), 255)
        return px.tobytes(), w, h


def texture_size(size, scale):
    """Texture pixels for a rug: screen pixels per point, long side <= 3072."""
    aspect = size[0] / size[1]
    long = min(max(size[0], size[1]) * scale, 3072.0)
    if aspect >= 1:
        return int(long), max(int(long / aspect), 1)
    return max(int(aspect * long), 1), int(long)


class Drag:
    def __init__(self, kind, rug=None, **kw):
        self.kind = kind
        self.rug = rug
        self.__dict__.update(kw)


class Controller(QObject):
    selectionChanged = pyqtSignal(object)        # rug id or None
    designRequested = pyqtSignal(str)
    layersChanged = pyqtSignal()

    def __init__(self, prefs, icons, sound, sim_context):
        super().__init__()
        self.prefs = prefs
        self.icons = icons
        self.sound = sound
        self.ctx = sim_context
        self.rugs: list[Rug] = []
        self.windows = []
        self.selected_id = None
        self.hidden = False
        self.rearranging = False
        bs = prefs.get("bumpStrength")
        self.bump_strength = float(bs) if isinstance(bs, (int, float)) else 1.0
        self.hover_handle = None
        self.drag = None
        self.selected_by_this_press = False
        self.spin = None
        self.redraw = True
        self.overlay_dirty = True
        self.was_awake = set()
        self.long_press = QTimer(self)
        self.long_press.setSingleShot(True)
        self.long_press.setInterval(500)
        self.long_press.timeout.connect(self._long_press_fired)
        self.hint_timer = QTimer(self)
        self.hint_timer.setSingleShot(True)
        self.hint_timer.timeout.connect(self._show_hint)
        self.hint_point = None
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.compositions, self.active_id = store.load_compositions(prefs)
        icons.changed.connect(self._icons_changed)
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        self.mouse_down_global = False

    # ------------------------------------------------------------------ setup
    def start(self):
        self.ctx.make_current()
        active = self.active_composition()
        self.active_id = active.id
        self.apply(active)
        self.timer.start()

    def active_composition(self) -> Composition:
        for c in self.compositions:
            if c.id == self.active_id:
                return c
        return self.compositions[0] if self.compositions else Composition(self.active_id or store.new_id(),
                                                                          "My Rugs", [])

    def apply(self, comp: Composition):
        self.ctx.make_current()
        for r in self.rugs:
            r.delete(lambda t: GL.glDeleteTextures(1, [t]))
        self.rugs = []
        self.set_selection(None)
        for st in comp.rugs:
            self.rugs.append(Rug(st))
        self._partition()
        if not comp.rugs:
            self.add_rug(save=False)
        self.redraw = self.overlay_dirty = True
        self.layersChanged.emit()

    def screen_world_rect(self, screen=None):
        screen = screen or QGuiApplication.primaryScreen()
        g = screen.geometry()
        return np.array([g.x(), g.y()], np.float64), np.array([g.width(), g.height()], np.float64)

    def screen_center(self, p):
        for s in QGuiApplication.screens():
            g = s.geometry()
            if g.x() <= p[0] < g.x() + g.width() and g.y() <= p[1] < g.y() + g.height():
                return np.array([g.x() + g.width() / 2, g.y() + g.height() / 2], np.float64)
        return np.asarray(p, np.float64)

    # ------------------------------------------------------------------ rugs
    def add_rug(self, save=True):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        O, S = self.screen_world_rect(screen)
        w, h = default_size(S)
        off = len(self.rugs) * 40.0
        design = store.RugDesign(seed=store.random_seed())
        st = RugState(id=store.new_id(), cx=O[0] + S[0] / 2 + off, cy=O[1] + S[1] / 2 + off, w=w, h=h,
                      design=design)
        self.ctx.make_current()
        rug = Rug(st, new_rug=True)
        self.rugs.append(rug)
        self._partition()
        if self.hidden:
            self.set_hidden(False)
        self.redraw = self.overlay_dirty = True
        if save:
            self.save()
        return rug

    def selected_rug(self):
        for r in self.rugs:
            if r.id == self.selected_id:
                return r
        return None

    def rug_by_id(self, rid):
        for r in self.rugs:
            if r.id == rid:
                return r
        return None

    def set_selection(self, rid):
        if rid != self.selected_id:
            self.selected_id = rid
            self.overlay_dirty = True
            self.selectionChanged.emit(rid)

    def _partition(self):
        self.rugs = [r for r in self.rugs if r.below_icons] + [r for r in self.rugs if not r.below_icons]

    def bring_to_front(self, rug):
        self.rugs.remove(rug)
        self.rugs.append(rug)
        self._partition()

    def any_below_icons(self):
        return any(r.below_icons for r in self.rugs)

    def set_hidden(self, hidden):
        self.hidden = hidden
        if hidden:
            self.set_selection(None)
        for w in self.windows:
            w.set_visible_layer(not hidden)
        self.redraw = self.overlay_dirty = True

    def toggle_rearranging(self):
        self.rearranging = not self.rearranging
        if not self.rearranging:
            self.set_selection(None)
        self.redraw = self.overlay_dirty = True

    def set_below_icons(self, rug, below):
        if rug.below_icons == below:
            return
        rug.below_icons = below
        self._partition()
        for r in self.rugs:
            r.sim.wake()
        if below and not self.rearranging:
            self.set_selection(None)
        if not self.any_below_icons() and self.rearranging:
            self.rearranging = False
            self.set_selection(None)
        self.redraw = self.overlay_dirty = True
        self.layersChanged.emit()
        self.save()

    def remove_rug(self, rug):
        if rug not in self.rugs:
            return
        self.rugs.remove(rug)
        if self.selected_id == rug.id:
            self.set_selection(None)
        self.ctx.make_current()
        rug.delete(lambda t: GL.glDeleteTextures(1, [t]))
        self.redraw = self.overlay_dirty = True
        self.layersChanged.emit()
        self.save()

    def smooth(self, rug):
        """(Linux port) Smooths the rug out, straight. A rug that fills the screen
        goes back to filling it; any other rug stays where it lies now (dragging
        the cloth moves it, which its stored pose doesn't know) and turns back
        to the angle it was set to (dragging can leave it a bit crooked)."""
        if rug.restore is not None:
            rug.center, rug.angle = self._fill_pose(rug)[0], 0.0
        else:
            self._fit_pose(rug)
            rug.angle = rug.heading
        rug.invalidate()
        rug.sim.flatten(rug.center, rug.size, rug.angle)
        self.overlay_dirty = True

    def _fill_pose(self, rug):
        """Centre and size of the usable part of the screen the rug is on."""
        screen = (QGuiApplication.screenAt(QPoint(int(rug.center[0]), int(rug.center[1])))
                  or QGuiApplication.primaryScreen())
        g = screen.availableGeometry()
        return (np.array([g.x() + g.width() / 2, g.y() + g.height() / 2], np.float64),
                (float(g.width()), float(g.height())))

    def fill_screen(self, rug):
        """(Linux port) A toggle: lays the rug straight over the whole screen;
        again to put it back where it was."""
        if rug.restore is not None:
            center, size, angle = rug.restore
            rug.restore = None
        else:
            self._fit_pose(rug)
            center, size = self._fill_pose(rug)
            if rug.size[0] >= 0.9 * size[0] and rug.size[1] >= 0.9 * size[1]:
                # already about as big as the screen: putting it back gives a normal-size rug
                rug.restore = (center.copy(), default_size(size), 0.0)
            else:
                rug.restore = (rug.center.copy(), rug.size, rug.heading)
            angle = 0.0
        rug.center, rug.size, rug.angle = np.asarray(center, np.float64), size, angle
        rug.heading = angle
        self.ctx.make_current()
        rug.sim.reset(rug.center, rug.size, rug.angle, 24.0)     # drops onto the desk
        rug.refresh_positions()
        rug.texture_dirty = True
        self.redraw = self.overlay_dirty = True
        self.save()

    def start_spin(self, rug):
        if self.spin is not None:
            return
        self.spin = dict(rug=rug, start=rug.angle, heading=rug.heading, snap=rug.snapshot(), frame=0)

    def set_design(self, rid, design):
        rug = self.rug_by_id(rid)
        if rug is None:
            return
        old = rug.design
        rug.design = design.copy()
        if rug.design.pile != old.pile:
            rug.sim.set_thickness(rug.design.pile)
        rug.texture_dirty = True
        self.redraw = True
        self.save_soon()

    # ------------------------------------------------------------------ arrangements
    def save_soon(self):
        """Saves shortly after a burst of edits (colour drags send many)."""
        if not hasattr(self, "_save_timer"):
            self._save_timer = QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.setInterval(400)
            self._save_timer.timeout.connect(self.save)
        self._save_timer.start()

    def save(self):
        states = [r.state() for r in self.rugs]
        for c in self.compositions:
            if c.id == self.active_id:
                c.rugs = states
        store.save_compositions(self.prefs, self.compositions, self.active_id)

    def switch_to(self, cid):
        if cid == self.active_id or not any(c.id == cid for c in self.compositions):
            return
        self.save()
        self.active_id = cid
        self.apply(self.active_composition())
        if self.hidden:
            self.set_hidden(False)
        self.redraw = self.overlay_dirty = True
        self.save()

    def new_composition(self, name):
        self.save()
        c = Composition(store.new_id(), name, [])
        self.compositions.append(c)
        self.active_id = c.id
        self.apply(c)
        self.save()

    def duplicate_composition(self, name):
        self.save()
        rugs = []
        for r in self.rugs:
            st = r.state()
            st.id = store.new_id()
            rugs.append(st)
        c = Composition(store.new_id(), name, rugs)
        self.compositions.append(c)
        self.active_id = c.id
        self.apply(c)
        self.save()

    def rename_composition(self, name):
        self.active_composition().name = name
        self.save()

    def delete_composition(self):
        if len(self.compositions) < 2:
            return
        idx = next(i for i, c in enumerate(self.compositions) if c.id == self.active_id)
        self.compositions.pop(idx)
        self.active_id = self.compositions[min(idx, len(self.compositions) - 1)].id
        self.apply(self.active_composition())
        self.save()

    # ------------------------------------------------------------------ hit testing
    def hit_test(self, p):
        if self.hidden:
            return None
        p = np.asarray(p, np.float64)
        sel = self.selected_rug()
        if sel is not None:
            if np.linalg.norm(p - sel.local_to_world(sel.knob_local())) < 13:
                return ("rotate", sel, None)
            for i, c in enumerate(sel.corners()):
                if np.linalg.norm(p - c) < 12:
                    return ("resize", sel, i)
        c = self.screen_center(p)
        for r in reversed(self.rugs):
            if r.below_icons and not self.rearranging:
                continue
            mn, mx = r.bounds(60)
            if not (mn[0] < p[0] < mx[0] and mn[1] < p[1] < mx[1]):
                continue
            rad = 0.9 * r.sim.spacing if r.sim.cols > 1 else 0.9
            q = r.positions
            d2 = ((project(q, c) - p) ** 2).sum(1)
            cand = np.nonzero(d2 < rad * rad)[0]
            if len(cand):
                return ("cloth", r, int(cand[np.argmax(q[cand, 2])]))
        return None

    # ------------------------------------------------------------------ mouse
    def mouse_down(self, p, click_count):
        self.hint_timer.stop()
        p = np.asarray(p, np.float64)
        hit = self.hit_test(p)
        if hit is None:
            self.set_selection(None)
            self.drag = None
            return
        kind, r, extra = hit
        if kind == "rotate":
            self.drag = Drag("rotate", r, start_angle=r.angle,
                             pointer0=math.atan2(p[1] - r.center[1], p[0] - r.center[0]), snap=r.snapshot())
        elif kind == "resize":
            lo, hi = r.footprint()
            self.drag = Drag("resize", r, corner=extra, lo=lo.copy(), hi=hi.copy(), start_center=r.center.copy(),
                             start_size=np.array(r.size), snap=r.snapshot())
        elif click_count >= 2:
            self.smooth(r)
            self.drag = None
        else:
            sel = self.selected_id == r.id
            self.drag = Drag("pending", r, particle=extra, press=p, selected=sel, cam=self.screen_center(p))
            if not sel:
                self.long_press.start()

    def _long_press_fired(self):
        d = self.drag
        if d is not None and d.kind == "pending" and not d.selected:
            self.set_selection(d.rug.id)
            self.selected_by_this_press = True
            self.redraw = True
            d.selected = True
            self.prefs.set("longPressLearned", True)

    def mouse_dragged(self, p, shift):
        d = self.drag
        if d is None:
            return
        p = np.asarray(p, np.float64)
        r = d.rug
        if d.kind == "pending":
            if np.linalg.norm(p - d.press) <= 3:
                return
            self.long_press.stop()
            if d.selected:
                self.bring_to_front(r)
                self.drag = Drag("move", r, press=d.press, start_center=r.center.copy(), snap=r.snapshot())
            else:
                self.ctx.make_current()
                r.sim.grab(d.particle, r.positions)
                self.drag = Drag("cloth", r, press=d.press, cam=d.cam)
            self.mouse_dragged(p, shift)
            return
        if d.kind == "cloth":
            z = float(r.positions[r.sim.grab_idx, 2].mean()) if len(r.sim.grab_idx) else 0.0
            t = d.cam + (p - d.cam) * (CAM_D - z) / CAM_D
            r.sim.drag_to(float(t[0]), float(t[1]))
        elif d.kind == "move":
            delta = p - d.press
            r.center = d.start_center + delta
            self._guide(r, d.snap[:, :2] + delta)
        elif d.kind == "rotate":
            a = math.atan2(p[1] - r.center[1], p[0] - r.center[0]) + d.start_angle - d.pointer0
            if shift:
                step = 0.2617994
                q = a / step
                a = math.copysign(math.floor(abs(q) + 0.5), q) * step
            r.angle = a
            delta = a - d.start_angle
            self._guide(r, r.center + (d.snap[:, :2] - r.center) @ rot(delta).T)
        elif d.kind == "resize":
            R = rot(r.angle)
            s = np.array(CORNER_SIGNS[d.corner], np.float64)
            anchor = np.where(s < 0, d.hi, d.lo)
            local = R.T @ (p - d.start_center)
            k = s * (local - anchor) / (d.hi - d.lo)
            k = np.maximum(k, np.array(MIN_SIZE) / d.start_size)
            if shift:
                k[:] = k.max()
            r.size = (float(k[0] * d.start_size[0]), float(k[1] * d.start_size[1]))
            r.center = d.start_center + R @ (anchor * (1 - k))
            self.ctx.make_current()
            r.sim.set_size(r.size)
            local_pts = (d.snap[:, :2] - d.start_center) @ R          # R⁻¹ · (q - c)
            self._guide(r, d.start_center + (anchor + k * (local_pts - anchor)) @ R.T)
        self.overlay_dirty = True

    def _guide(self, rug, targets):
        self.ctx.make_current()
        rug.sim.set_guide(np.asarray(targets, np.float32))
        rug.invalidate()

    def mouse_up(self, p):
        self.long_press.stop()
        d, self.drag = self.drag, None
        if d is not None:
            r = d.rug
            if d.kind == "cloth":
                self.ctx.make_current()
                r.sim.release()
            elif d.kind in ("move", "rotate", "resize"):
                r.sim.end_guide()
                if d.kind == "rotate":
                    r.heading = r.angle
                if d.kind == "resize":
                    r.restore = None
                    self.ctx.make_current()
                    if not r.sim.can_resize_in_place(r.size):
                        r.sim.reset(r.center, r.size, r.angle, 0.0)
                        r.refresh_positions()
                    tw, th = r.texture_size
                    if tw == 0 or abs(r.size[0] / tw - 1) > 0.08 or abs(r.size[1] / th - 1) > 0.08:
                        r.texture_dirty = True
                self.save()
            elif d.kind == "pending":
                if d.selected:
                    if not self.selected_by_this_press:
                        self.set_selection(None)
                else:
                    self._maybe_show_hint(np.asarray(p, np.float64))
        self.overlay_dirty = True
        self.selected_by_this_press = False

    def _maybe_show_hint(self, p):
        if self.prefs.get("longPressLearned", False):
            return
        self.hint_point = p
        self.hint_timer.start(QGuiApplication.styleHints().mouseDoubleClickInterval())

    def _show_hint(self):
        for w in self.windows:
            w.show_hint(self.hint_point)

    # ------------------------------------------------------------------ hover / click-through
    def update_mouse(self):
        from . import platform_x11
        buttons = platform_x11.pointer_buttons()
        if self.drag is not None:
            return
        pos = QCursor.pos()
        p = np.array([pos.x(), pos.y()], np.float64)
        if buttons:
            # a click somewhere else (another app or the bare desktop) deselects
            if not self.mouse_down_global:
                self.mouse_down_global = True
                if self.selected_id is not None and not any(w.wants_mouse for w in self.windows):
                    self.set_selection(None)
            return
        self.mouse_down_global = False
        hit = self.hit_test(p)
        hover = None
        if hit is not None and hit[0] == "resize":
            hover = hit[2]
        elif hit is not None and hit[0] == "rotate":
            hover = 4
        if hover != self.hover_handle:
            self.hover_handle = hover
            self.overlay_dirty = True
        for w in self.windows:
            w.set_wants_mouse(hit is not None or w.pill_contains(p))

    # ------------------------------------------------------------------ frame loop
    def _step_spin(self):
        sp = self.spin
        if sp is None:
            return
        r = sp["rug"]
        if r not in self.rugs:
            self.spin = None
            return
        k = sp["frame"] + 1
        t = min(k / 18, 1.0)
        e = t * t * (3 - 2 * t)
        delta = e * math.pi * 0.5
        r.angle = sp["start"] + delta
        self._guide(r, r.center + (sp["snap"][:, :2] - r.center) @ rot(delta).T)
        self.overlay_dirty = True
        # (The Mac app ends the guide on frame 18 before the cloth gets that
        # frame's targets, so each turn stops ~0.8° short and the error adds up.
        # Here the final pose is held for a few more frames.)
        if k >= 18 + 6:
            r.heading = sp["heading"] + math.pi * 0.5
            r.sim.end_guide()
            self.spin = None
            self.save()
        else:
            sp["frame"] = k

    def bumps_for(self, rug):
        s = float(self.icons.icon_size)
        lo, hi = rug.bounds(1.5 * s)
        icons = [p for p in self.icons.positions if lo[0] < p[0] < hi[0] and lo[1] < p[1] < hi[1]]
        if self.bump_strength > 0 and not rug.below_icons and icons:
            return dict(icons=icons, height=s * 0.24 * self.bump_strength * (1 - 0.4 * float(rug.design.pile)),
                        half=(0.42 * s, 0.60 * s), radius=0.28 * s, soft=0.22 * s, offset=(0.0, 0.16 * s))
        return NO_BUMPS

    def _icons_changed(self):
        if any(r.below_icons for r in self.rugs):
            self.redraw = True
        for r in self.rugs:
            if not r.below_icons:
                r.sim.wake()

    def tick(self):
        # Fast path: nothing moving, mouse still, nothing pending -> nothing to do.
        from . import platform_x11
        pos = QCursor.pos()
        quiet_key = (pos.x(), pos.y(), platform_x11.pointer_buttons(), self.selected_id, self.hidden,
                     self.rearranging, len(self.rugs))
        if (self.drag is None and self.spin is None and not self.redraw and not self.overlay_dirty
                and quiet_key == getattr(self, "_quiet_key", None)
                and not any(r.sim.awake or r.texture_dirty or r.texture_busy for r in self.rugs)
                and not self.was_awake and not self.sound._running):
            self._quiet_ticks = getattr(self, "_quiet_ticks", 0) + 1
            if self._quiet_ticks == 60:
                self.timer.setInterval(50)      # nothing going on: poll the mouse less often
            return
        self._quiet_ticks = 0
        if self.timer.interval() != 16:
            self.timer.setInterval(16)
        self._quiet_key = quiet_key
        self.update_mouse()
        self._step_spin()
        self.ctx.make_current()
        changed = False
        motion = impact = softness = 0.0
        stepped = False
        fps = [r.bounds(0) for r in self.rugs]
        for i, r in enumerate(self.rugs):
            if not r.sim.awake:
                continue
            lower = [self.rugs[j].sim for j in range(i)
                     if self.rugs[j].below_icons == r.below_icons and overlap(fps[j], fps[i], 10)]
            r.sim.step(DT, self.bumps_for(r), lower)
            r.refresh_positions()
            if not r.sim.awake:
                changed = True
                continue
            fps[i] = r.bounds(0)
            stepped = changed = True
            self.was_awake.add(r.id)
            for j in range(i + 1, len(self.rugs)):
                o = self.rugs[j]
                if o.below_icons == r.below_icons and overlap(fps[j], fps[i], 10):
                    o.sim.wake()
            if r.sim.motion > motion:
                softness = float(r.design.pile)
            motion = max(motion, r.sim.motion)
            impact += r.sim.impact
        if self.hidden:
            motion = impact = 0.0
        if stepped or self.rugs:
            self.sound.update(motion, impact, softness)
        # rugs that just fell asleep: re-centre on where the cloth actually lies, then save
        settled = False
        for r in self.rugs:
            if r.id in self.was_awake and not r.sim.awake:
                self.was_awake.discard(r.id)
                if self.drag is None or self.drag.rug is not r:
                    self._recentre(r)
                    settled = True
        if settled:
            self.save()
        changed |= self._textures()
        if changed:
            # the windows' contexts wait (on the GPU) for this frame's work
            # (keep a few old fences alive: a window may paint a tick late)
            self.old_fences = getattr(self, "old_fences", [])
            if getattr(self, "frame_fence", None) is not None:
                self.old_fences.append(self.frame_fence)
            while len(self.old_fences) > 4:
                GL.glDeleteSync(self.old_fences.pop(0))
            self.frame_fence = GL.glFenceSync(GL.GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
            GL.glFlush()
        if changed or self.redraw:
            self.redraw = False
            self.overlay_dirty = False
            for w in self.windows:
                w.update()
        elif self.overlay_dirty:
            self.overlay_dirty = False
            for w in self.windows:
                w.update()

    def _recentre(self, r):
        # The Mac app re-centres on the flat part of the cloth when it settles;
        # here the angle is refitted too (a dragged rug turns as well as slides).
        self._fit_pose(r, need_flat=True)

    @staticmethod
    def _face_up(r):
        """Which cloth points have their top side up (folded-over flaps are flipped)."""
        cols, rows = r.sim.cols, r.sim.rows
        g = r.positions.reshape(rows, cols, 3)
        du = np.gradient(g, axis=1)
        dv = np.gradient(g, axis=0)
        n = np.cross(du, dv)
        nz = n[..., 2] / np.maximum(np.linalg.norm(n, axis=-1), 1e-9)
        return (nz > 0.5).ravel()

    def _fit_pose(self, r, need_flat=False):
        """Sets the rug's stored position and angle to where the cloth lies.

        A rug with a few clean folds keeps its flat part where it is (the folds
        open back out from it); a rug that is all bunched up spreads out around
        its own middle. Tiny differences are ignored so exact angles stay exact."""
        p = r.positions
        n = len(p)
        if n == 0 or len(r.sim.uv) != n:
            return

        def fit(mask):
            rest = (r.sim.uv[mask] - 0.5) * np.array(r.size)
            q = p[mask, :2]
            rc, qc = rest.mean(0), q.mean(0)
            A, B = rest - rc, q - qc
            ang = math.atan2(float((A[:, 0] * B[:, 1] - A[:, 1] * B[:, 0]).sum()), float((A * B).sum()))
            R = rot(ang)
            centre = qc - R @ rc
            rms = float(np.sqrt((((centre + rest @ R.T) - q) ** 2).sum(1).mean()))
            return centre, ang, rms

        flat = (p[:, 2] < 1.5) & self._face_up(r)
        pose = None
        if flat.sum() >= 0.25 * n:
            centre, ang, rms = fit(flat)
            if rms < max(6.0, 0.6 * r.sim.spacing):     # the flat part is undistorted
                pose = (centre, ang)
        if pose is None:
            centre, ang, _ = fit(np.ones(n, bool))
            pose = (centre, ang)
        centre, ang = pose
        d_ang = (ang - r.angle + math.pi) % (2 * math.pi) - math.pi
        if abs(d_ang) < math.radians(0.25) and np.linalg.norm(centre - r.center) < 0.5:
            return
        r.angle = r.angle + d_ang
        r.center = centre
        r.invalidate()
        self.overlay_dirty = True

    def _textures(self) -> bool:
        changed = False
        resizing = self.drag.rug if (self.drag is not None and self.drag.kind == "resize") else None
        for r in self.rugs:
            if r.texture_dirty and not r.texture_busy and r is not resizing:
                r.texture_dirty = False
                r.texture_busy = True
                scale = self._scale_for(r)
                w, h = texture_size(r.size, scale)
                r.texture_job = (self.pool.submit(_render_texture, r.design.copy(), r.size, w, h), r.size)
            if r.texture_busy and r.texture_job is not None and r.texture_job[0].done():
                fut, size = r.texture_job
                r.texture_job = None
                r.texture_busy = False
                try:
                    data, w, h = fut.result()
                except Exception:
                    continue
                if r.texture:
                    gl.update_texture(r.texture, w, h, data)
                else:
                    r.texture = gl.texture_rgba(w, h, data)
                r.texture_size = size
                changed = True
        return changed

    def _scale_for(self, rug):
        s = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        return s.devicePixelRatio() if s else 2.0

    def visible_rugs(self, below: bool, origin, size):
        if self.hidden:
            return []
        out = []
        for r in self.rugs:
            if r.below_icons != below or not r.texture:
                continue
            mn, mx = r.bounds(0)
            if origin[0] < mx[0] + 300 and mn[0] - 300 < origin[0] + size[0] and \
               origin[1] < mx[1] + 300 and mn[1] - 300 < origin[1] + size[1]:
                out.append(r)
        return out

    def shutdown(self):
        self.timer.stop()
        try:
            self.save()
        except Exception:
            pass
        self.pool.shutdown(wait=False, cancel_futures=True)
