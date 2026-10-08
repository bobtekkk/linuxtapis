"""One rug on the desktop: where it lies, its design, its cloth and texture."""

import math

import numpy as np

from .cloth import ClothSim
from .store import RugState


def rot(angle):
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, -s], [s, c]], np.float64)


class Rug:
    def __init__(self, state: RugState, new_rug: bool = False):
        self.id = state.id
        self.center = np.array([state.cx, state.cy], np.float64)
        self.size = (float(state.w), float(state.h))
        self.angle = float(state.angle)
        self.below_icons = state.below_icons
        self.design = state.design.copy()
        self.sim = ClothSim()
        self.sim.set_thickness(self.design.pile)
        if state.positions is not None and not new_rug:
            self.sim.restore(self.center, self.size, self.angle, state.positions)
        else:
            self.sim.reset(self.center, self.size, self.angle, 30.0 if new_rug else 0.0)
        self.sim.refine_only()
        self.texture = 0
        self.texture_size = (0.0, 0.0)
        self.texture_dirty = True
        self.texture_busy = False
        self.texture_job = None
        self.positions = self.sim.positions()
        self._bounds_cache = None

    # ---- cached geometry (positions are read back after each step)
    def refresh_positions(self):
        latest = self.sim.latest
        self.positions = latest if latest is not None else self.sim.positions()
        self._bounds_cache = None

    def footprint(self):
        """Rug-local min/max of the actual cloth."""
        if self._bounds_cache is None:
            local = (self.positions[:, :2] - self.center) @ rot(self.angle)   # R(-angle) · (p - c)
            self._bounds_cache = (local.min(0), local.max(0))
        return self._bounds_cache

    def invalidate(self):
        self._bounds_cache = None

    def local_to_world(self, l):
        return self.center + rot(self.angle) @ np.asarray(l, np.float64)

    def corners(self):
        lo, hi = self.footprint()
        R = rot(self.angle)
        return [self.center + R @ np.array(c) for c in
                ((lo[0], lo[1]), (hi[0], lo[1]), (hi[0], hi[1]), (lo[0], hi[1]))]

    def bounds(self, pad: float = 0.0):
        cs = np.array(self.corners())
        return cs.min(0) - pad, cs.max(0) + pad

    def knob_local(self):
        lo, hi = self.footprint()
        return np.array([(lo[0] + hi[0]) / 2, lo[1] - 34])

    def stem_base_local(self):
        lo, hi = self.footprint()
        return np.array([(lo[0] + hi[0]) / 2, lo[1]])

    def snapshot(self) -> np.ndarray:
        return self.positions.copy()

    def state(self) -> RugState:
        return RugState(id=self.id, cx=float(self.center[0]), cy=float(self.center[1]), w=self.size[0],
                        h=self.size[1], angle=self.angle, design=self.design.copy(),
                        positions=self.positions.astype(np.float32).copy(), below_icons=self.below_icons)

    def delete(self, gl_delete_texture):
        if self.texture:
            gl_delete_texture(self.texture)
            self.texture = 0
        self.sim.delete()
