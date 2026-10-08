"""The cloth: a grid of particles simulated on the GPU, driven exactly like the
Mac app's ClothSim (same grid, constraints, substeps, solver passes and
constants). Positions are desktop points: x right, y down, z up off the desk."""

import math
import struct

import numpy as np
from OpenGL import GL

from . import gl

KERNELS = ["clearSupport", "rasterSupport", "frameBegin", "integrate", "clearBuckets", "countTriangles",
           "scanBuckets", "fillTriangles", "solveBatch", "limitBatch", "applyTargets", "collide",
           "applyDeltas", "friction", "frameEnd", "refine", "refineNormals", "tether"]

FLAG_GRAB, FLAG_GUIDE, FLAG_FLATTEN, FLAG_PLACE = 1, 2, 4, 8
HASH_SIZE = 8192
REFINE = 3
SUBSTEPS = 6
ITERATIONS = 4
BARRIER = GL.GL_SHADER_STORAGE_BARRIER_BIT | GL.GL_UNIFORM_BARRIER_BIT

_SIM_FMT = "<4I5fI6f3fI2I8fif"

# How the cloth feels. The Mac app's values give a light, sheet-like cloth;
# these knobs make it behave like a heavy wool rug.
FEEL = dict(
    shear_k=0.8,          # diagonal links
    bend_k=1.0,           # skip-one links: fold stiffness (Mac: 0.12)
    bend4_k=0.8,          # skip-three links: broad, rounded folds (Mac: none)
    gravity=4800.0,       # pt/s² (Mac: 2600)
    air_damp=0.975,       # velocity kept per substep while moving (Mac: 0.99), eases to 0.92 at rest
    grab_ease=0.25,       # how fast the grab point follows the cursor (Mac: 0.35)
    grab_stiffness=0.3,   # pull of the cursor on the grabbed patch, per solver pass
    grab_lift=14.0,       # how high a grabbed patch is lifted, pt (Mac: 20)
    grab_headroom=30.0,
    grab_radius=1.6,      # grabbed patch radius, in grid spacings (min 18 pt)
    stick_limit=0.4,      # how far a resting point can be tugged before it slides (Mac: 0.7)
    slide=0.7,            # share of a hard tug that turns into sliding (Mac: 0.5)
    layer_grip=0.15,
    max_stretch=1.01,     # strain limit on the threads (Mac: 1.05)
    limit_passes=6,       # strain-limit passes per substep (Mac: 4)
    limit_shear=True,     # also strain-limit the diagonals: no skewing (Mac: no)
    tether=True,          # the grabbed point drags the whole rug instead of stretching it (Mac: no)
    tether_slack=0.0,
    corners_up=False,     # edges and corners prefer to fold up (on top) rather than tuck under
    edge_k=1.0,           # (corners_up) extra stiffness of the bound border
    corners_collide=True, # (corners_up) edges go on top where they meet the rug side-on
    corner_band=40.0,     # how far in from the edge that preference reaches, pt
)

# The two ends of the Softness setting. 0 = heavy wool rug (the values above),
# 1 = a light bed sheet (softer and floatier than the Mac app's cloth).
HEAVY = {k: FEEL[k] for k in ("shear_k", "bend_k", "bend4_k", "gravity", "air_damp", "grab_ease", "grab_lift",
                              "stick_limit", "slide", "max_stretch", "tether_slack")}
SHEET = dict(shear_k=0.6, bend_k=0.06, bend4_k=0.0, gravity=2200.0, air_damp=0.99, grab_ease=0.4, grab_lift=26.0,
             stick_limit=0.7, slide=0.5, max_stretch=1.04, tether_slack=0.03)
SOFTNESS = {"value": 0.0}


def set_softness(s: float):
    """Blends every feel setting between a heavy rug (0) and a bed sheet (1)."""
    s = min(max(float(s), 0.0), 1.0)
    SOFTNESS["value"] = s
    for k, a in HEAVY.items():
        FEEL[k] = a + (SHEET[k] - a) * s
    FEEL["limit_shear"] = s < 0.6       # sheets may skew a little


def thickness_scale() -> float:
    """Sheets are thinner than rugs."""
    return 1.0 - 0.55 * SOFTNESS["value"]
_SUPPORT_FMT = "<3f3If I".replace(" ", "")


class Kernels:
    """The compute programs, compiled once per GL context."""
    _inst = None

    def __init__(self):
        self.prog = {k: gl.compute_program(k) for k in KERNELS}
        self.loc_batch = {k: GL.glGetUniformLocation(self.prog[k], "batch") for k in ("solveBatch", "limitBatch")}
        self.loc_R = {k: GL.glGetUniformLocation(self.prog[k], "R") for k in ("refine", "refineNormals")}
        tp = self.prog["tether"]
        self.loc_slide = GL.glGetUniformLocation(self.prog["friction"], "slide")
        self.loc_band = GL.glGetUniformLocation(self.prog["collide"], "edgeBand")
        self.loc_tether = tuple(GL.glGetUniformLocation(tp, n) for n in ("anchorIdx", "restSize", "slack"))

    @classmethod
    def get(cls):
        if cls._inst is None:
            cls._inst = Kernels()
        return cls._inst


def grid_for(w: float, h: float):
    sp = min(w, h) / 60.0
    rnd = lambda x: math.floor(x + 0.5)  # half away from zero (positive values)
    return max(rnd(w / sp), 6) + 1, max(rnd(h / sp), 6) + 1


def target_positions(uv: np.ndarray, center, size, angle) -> np.ndarray:
    s, c = math.sin(angle), math.cos(angle)
    X = (uv[:, 0] - 0.5) * size[0]
    Y = (uv[:, 1] - 0.5) * size[1]
    return np.stack([center[0] + c * X - s * Y, center[1] + s * X + c * Y], 1).astype(np.float32)


def grid_triangles(cols: int, rows: int) -> np.ndarray:
    j, i = np.mgrid[0:rows - 1, 0:cols - 1]
    a = (j * cols + i).ravel()
    b, c = a + 1, a + cols
    d = c + 1
    return np.stack([a, b, c, b, d, c], 1).astype(np.uint32).ravel()


class ClothSim:
    def __init__(self):
        self.K = Kernels.get()
        self.f = gl.fast()
        self.cols = self.rows = 0
        self.size = (1.0, 1.0)
        self.awake = True
        self.motion = self.impact = 0.0
        self.grab_idx = np.zeros(0, np.int64)
        self.grab_target = np.zeros(3, np.float32)
        self.grab_current = np.zeros(3, np.float32)
        self.guided = False
        self.place_on_surface = False
        self.flatten_frames = 0
        self.calm_frames = 0
        self.idle_frames = 0
        self.thickness = 4.5
        self.topology_version = 0
        self.wake_gen = 0
        self.pending = None
        self.fence = None
        self.readback = 0
        self.readback_size = 0
        self.latest = None
        self.bufs: dict[str, gl.Buffer] = {}
        self.params = gl.Buffer(nbytes=128)
        self.support_info = gl.Buffer(nbytes=32)
        self.support_infos: list[gl.Buffer] = []
        self.support = gl.Buffer(nbytes=16)
        self.icon_buf = gl.Buffer(nbytes=0x200)
        self.icon_cap = 0x200

    # ------------------------------------------------------------------ build
    def build(self, cols: int, rows: int):
        for b in self.bufs.values():
            b.delete()
        self.cols, self.rows = cols, rows
        n = cols * rows
        B = lambda nbytes: gl.Buffer(nbytes=nbytes)
        self.bufs = {
            "pos": B(n * 16), "prev": B(n * 16), "frameStart": B(n * 16), "grabOff": B(n * 16), "delta": B(n * 16),
            "ground": B(n * 4), "contact": B(n * 4), "wasContact": B(n * 4), "pinned": B(n * 4),
            "contactAtStart": B(n * 4), "anchor": B(n * 8), "guide": B(n * 8), "flatten": B(n * 8),
            "stats": B(16), "bucketCount": B(HASH_SIZE * 4), "bucketStart": B((HASH_SIZE + 1) * 4),
        }
        self.render_cols, self.render_rows = REFINE * cols - 2, REFINE * rows - 2
        self.render_count = self.render_cols * self.render_rows
        self.bufs["vertex"] = B(self.render_count * 36)
        self.bufs["contact"].upload(np.ones(n, np.uint32))
        k = np.arange(n)
        self.uv = np.stack([(k % cols) / (cols - 1), (k // cols) / (rows - 1)], 1).astype(np.float32)
        self.bufs["uv"] = gl.Buffer(self.uv)
        self._build_constraints()
        self.indices = grid_triangles(cols, rows)
        self.tri_count = len(self.indices) // 3
        self.bufs["index"] = gl.Buffer(self.indices)
        self.render_indices = grid_triangles(self.render_cols, self.render_rows)
        self.render_index_buf = gl.Buffer(self.render_indices)
        self._build_rim()
        self.item_capacity = self.tri_count * 25
        self.bufs["bucketItems"] = B(self.tri_count * 100)
        self.grab_idx = np.zeros(0, np.int64)
        self.guided = False
        self.flatten_frames = 0
        self.topology_version += 1
        if self.fence is not None:
            GL.glDeleteSync(self.fence)
        self.fence = None
        self.pending = None
        self.latest = None

    def _build_constraints(self):
        cols, rows = self.cols, self.rows
        j, i = np.mgrid[0:rows, 0:cols]
        p = j * cols + i
        fams = []

        def add(mask, a, b, k, batch):
            fams.append((a[mask], b[mask], np.full(mask.sum(), k, np.float32), batch[mask]))
        add(i + 1 < cols, p, p + 1, 1.0, i % 2)                                  # structural H
        add(j + 1 < rows, p, p + cols, 1.0, 2 + j % 2)                           # structural V
        m = (j + 1 < rows) & (i + 1 < cols)
        add(m, p, p + cols + 1, FEEL["shear_k"], 4 + i % 2)                      # shear \
        add(m, p + 1, p + cols, FEEL["shear_k"], 6 + i % 2)                      # shear /
        add(i + 2 < cols, p, p + 2, FEEL["bend_k"], 8 + (i // 2) % 2)              # bend H
        add(j + 2 < rows, p, p + 2 * cols, FEEL["bend_k"], 10 + (j // 2) % 2)      # bend V
        if FEEL["corners_up"] and FEEL["edge_k"] > 0:
            # a stiffer bound border (skip-two links across and along the edge band),
            # so corners and edges don't roll under
            band = max(2, math.ceil(FEEL["corner_band"] / max(min(self.size) / 60.0, 1.0)))
            ev = np.minimum(np.minimum(i, cols - 1 - i), np.minimum(j, rows - 1 - j))
            nearH = (np.minimum(j, rows - 1 - j) < band) | (np.minimum(i, cols - 4 - i) < band)
            nearV = (np.minimum(i, cols - 1 - i) < band) | (np.minimum(j, rows - 4 - j) < band)
            add((i + 3 < cols) & nearH, p, p + 3, FEEL["edge_k"], 16 + (i // 3) % 2)
            add((j + 3 < rows) & nearV, p, p + 3 * cols, FEEL["edge_k"], 18 + (j // 3) % 2)
        if FEEL["bend4_k"] > 0:
            add(i + 4 < cols, p, p + 4, FEEL["bend4_k"], 12 + (i // 4) % 2)        # long bend H
            add(j + 4 < rows, p, p + 4 * cols, FEEL["bend4_k"], 14 + (j // 4) % 2)  # long bend V
        a = np.concatenate([f[0] for f in fams]).astype(np.uint32)
        b = np.concatenate([f[1] for f in fams]).astype(np.uint32)
        k = np.concatenate([f[2] for f in fams])
        batch = np.concatenate([f[3] for f in fams])
        order = np.argsort(batch, kind="stable")
        self.cA, self.cB, self.cK, self.cBatch = a[order], b[order], k[order], batch[order]
        counts = np.bincount(self.cBatch, minlength=20)
        offs = np.concatenate([[0], np.cumsum(counts)[:-1]])
        self.batches = [(int(o), int(c)) for o, c in zip(offs, counts)]
        self.bufs["cons"] = gl.Buffer(nbytes=len(a) * 16)

    def _build_rim(self):
        rc, rr = self.render_cols, self.render_rows
        ring = [(i, 0) for i in range(rc)]
        ring += [(rc - 1, j) for j in range(1, rr)]
        ring += [(i, rr - 1) for i in range(rc - 2, -1, -1)]
        ring += [(0, j) for j in range(rr - 2, 0, -1)]
        m = len(ring)
        inner = lambda i, j: (min(max(i, 1), rc - 2), min(max(j, 1), rr - 2))
        rim = np.empty((m, 4), np.uint32)
        for k in range(m):
            ia, ja = ring[k]
            ib, jb = ring[(k + 1) % m]
            ia2, ja2 = inner(ia, ja)
            ib2, jb2 = inner(ib, jb)
            rim[k] = (ja * rc + ia, jb * rc + ib, ja2 * rc + ia2, jb2 * rc + ib2)
        self.rim_buf = gl.Buffer(rim)
        self.rim_segments = m

    def refresh_constraints(self):
        """Rebuilds the links (after a feel setting changed), keeping the cloth where it is."""
        self.bufs["cons"].delete()
        self._build_constraints()
        self.set_size(self.size)
        self.wake()

    def set_size(self, size):
        self.size = (float(size[0]), float(size[1]))
        sz = np.array(self.size, np.float32)
        length = np.linalg.norm((self.uv[self.cA] - self.uv[self.cB]) * sz, axis=1).astype(np.float32)
        cons = np.empty(len(self.cA), dtype=[("a", "<u4"), ("b", "<u4"), ("len", "<f4"), ("k", "<f4")])
        cons["a"], cons["b"], cons["len"], cons["k"] = self.cA, self.cB, length, self.cK
        self.bufs["cons"].upload(cons)

    @property
    def spacing(self) -> float:
        return self.size[0] / (self.cols - 1) if self.cols >= 2 else 1.0

    def set_thickness(self, pile: float):
        t = (float(pile) * 18 * 0.85 + 4.5) * thickness_scale()
        self.pile = float(pile)
        if t != self.thickness:
            self.thickness = t
            self.wake()

    def wake(self):
        self.wake_gen += 1
        self.awake = True
        self.calm_frames = self.idle_frames = 0

    # ------------------------------------------------------------------ placement
    def reset(self, center, size, angle, z):
        self.build(*grid_for(*size))
        self.set_size(size)
        xy = target_positions(self.uv, center, size, angle)
        self.upload(np.column_stack([xy, np.full(len(xy), z, np.float32)]))
        self.place_on_surface = True

    def restore(self, center, size, angle, saved) -> bool:
        cols, rows = grid_for(*size)
        if saved is not None and len(saved) == cols * rows:
            self.build(cols, rows)
            self.set_size(size)
            self.upload(saved)
            return True
        self.reset(center, size, angle, 0.0)
        return False

    def upload(self, xyz: np.ndarray):
        n = self.cols * self.rows
        p = np.zeros((n, 4), np.float32)
        p[:, :3] = xyz[:n]
        self.bufs["pos"].upload(p)
        self.bufs["prev"].upload(p)
        self.bufs["wasContact"].clear()
        self.wake()

    def positions(self) -> np.ndarray:
        return self.bufs["pos"].read(np.float32).reshape(-1, 4)[: self.cols * self.rows, :3].copy()

    def target_positions(self, center, size, angle):
        return target_positions(self.uv, center, size, angle)

    # ------------------------------------------------------------------ interaction
    def grab(self, c: int, pos: np.ndarray | None = None):
        if pos is None:
            pos = self.positions()
        R = max(self.spacing * FEEL["grab_radius"], 18.0) if self.cols >= 2 else 18.0
        sz = np.array(self.size, np.float32)
        d = np.linalg.norm((self.uv - self.uv[c]) * sz, axis=1)
        idx = np.nonzero(d < R)[0]
        self.grab_idx = idx
        self.grab_center = c
        n = self.cols * self.rows
        off = np.zeros((n, 4), np.float32)
        off[idx, :3] = pos[idx] - pos[c]
        self.bufs["grabOff"].upload(off)
        pinned = self.bufs["pinned"].read(np.uint32, n)
        pinned[idx] = 1
        self.bufs["pinned"].upload(pinned)
        self.grab_target = pos[c].astype(np.float32).copy()
        self.grab_current = self.grab_target.copy()
        self.grab_pos = pos
        self.flatten_frames = 0
        self.wake()

    def mean_grab_z(self) -> float:
        if len(self.grab_idx) == 0:
            return 0.0
        pos = self.bufs["pos"].read(np.float32).reshape(-1, 4)
        return float(pos[self.grab_idx, 2].mean())

    def drag_to(self, x: float, y: float):
        self.grab_target = np.array([x, y, FEEL["grab_lift"]], np.float32)
        self.wake()

    def release(self):
        if len(self.grab_idx):
            self.bufs["pinned"].clear()
        self.grab_idx = np.zeros(0, np.int64)
        self.wake()

    def set_guide(self, targets: np.ndarray):
        self.bufs["guide"].upload(np.ascontiguousarray(targets, np.float32))
        self.guided = True
        self.bufs["wasContact"].clear()
        self.wake()

    def end_guide(self):
        self.guided = False
        self.wake()

    def flatten(self, center, size, angle):
        self.bufs["flatten"].upload(target_positions(self.uv, center, size, angle))
        self.flatten_frames = 80
        self.wake()

    def can_resize_in_place(self, size) -> bool:
        nc, nr = grid_for(*size)
        lim = lambda k: k // 4 if k > 11 else 3
        return abs(nc - self.cols) <= lim(self.cols) and abs(nr - self.rows) <= lim(self.rows)

    # ------------------------------------------------------------------ step
    def step(self, dt: float, bumps: dict, lower: list["ClothSim"]):
        """Queues one frame on the GPU. Its stats and positions are read one
        frame later (collect), so the CPU never waits for the GPU."""
        self.collect()
        n = self.cols * self.rows
        if not self.awake or n < 1:
            self.motion = self.impact = 0.0
            return
        F, K, B = self.f, self.K, self.bufs
        grabbing = len(self.grab_idx) > 0
        if grabbing:
            self.grab_current += (self.grab_target - self.grab_current) * FEEL["grab_ease"]
        flatten_old = self.flatten_frames
        if self.flatten_frames - 1 >= 0:
            self.flatten_frames -= 1
        idle_t = min(self.idle_frames / 60.0, 1.0)

        icons = np.asarray(bumps["icons"], np.float32).reshape(-1, 2)
        if len(icons):
            if self.icon_cap < len(icons) * 8:
                self.icon_cap = len(icons) * 16
                self.icon_buf.alloc(self.icon_cap)
            self.icon_buf.write(icons)
        h = dt / SUBSTEPS
        air = FEEL["air_damp"] - (FEEL["air_damp"] - 0.92) * idle_t
        B["stats"].clear()
        spacing = self.spacing
        if self.cols >= 2:
            ring = max(math.ceil(self.thickness / max(spacing, 1.0)), 1)
            max_push = spacing * 0.25
        else:
            ring, max_push = max(math.ceil(self.thickness), 1), 0.25
        flags = ((FLAG_GRAB if grabbing else 0) | (FLAG_GUIDE if self.guided else 0)
                 | (FLAG_FLATTEN if flatten_old > 0 else 0) | (FLAG_PLACE if self.place_on_surface else 0))
        gx, gy, gz = (float(v) for v in self.grab_current)
        params = struct.pack(
            _SIM_FMT, n, self.cols, self.rows, self.tri_count,
            spacing, self.thickness, FEEL["gravity"] * h * h, air, min(air, 0.99),
            flags,
            FEEL["grab_stiffness"], FEEL["grab_headroom"], gx, gy, gz, 0.5,
            FEEL["stick_limit"], FEEL["layer_grip"], FEEL["max_stretch"],
            HASH_SIZE,
            self.item_capacity, len(icons),
            bumps["height"], bumps["half"][0], bumps["half"][1], bumps["radius"], max(bumps["soft"], 0.001),
            bumps["offset"][0], bumps["offset"][1], 2.0,
            ring, max_push)
        self.params.write(np.frombuffer(params, np.uint8))
        self.place_on_surface = False

        self.params.ubo(0)
        for name, binding in (("pos", 0), ("prev", 1), ("ground", 2), ("contact", 3), ("wasContact", 4),
                              ("anchor", 5), ("pinned", 6), ("grabOff", 7), ("guide", 8), ("flatten", 9),
                              ("delta", 10), ("cons", 11), ("index", 12), ("bucketCount", 13),
                              ("bucketItems", 14), ("frameStart", 17), ("contactAtStart", 18), ("stats", 19),
                              ("vertex", 21), ("bucketStart", 23)):
            B[name].ssbo(binding)
        self.icon_buf.ssbo(20)

        # ---- support from rugs below
        if lower:
            pos = (self.latest if self.latest is not None and len(self.latest) == n else self.positions())[:, :2]
            lo, hi = pos.min(0), pos.max(0)
            margin = 4 * spacing if self.cols >= 2 else 4.0
            origin = lo - margin
            ext = (hi + margin) - origin
            cell = max(float(max(ext[0], ext[1])) / 400.0, 3.0)
            dim_x, dim_y = math.ceil(ext[0] / cell), math.ceil(ext[1] / cell)
            if self.support.size < dim_x * dim_y * 4:
                self.support.alloc(dim_x * dim_y * 4)
            self.support.ssbo(24)
            self._support(origin, cell, dim_x, dim_y, 0, 0.0, self.support_info)
            self.support_info.ubo(1)
            F.useProgram(K.prog["clearSupport"])
            F.dispatch(gl.groups(dim_x * dim_y), 1, 1)
            F.barrier(BARRIER)
            while len(self.support_infos) < len(lower):
                self.support_infos.append(gl.Buffer(nbytes=32))
            F.useProgram(K.prog["rasterSupport"])
            for L, info in zip(lower, self.support_infos):
                self._support(origin, cell, dim_x, dim_y, L.tri_count, L.thickness, info)
                info.ubo(1)
                L.bufs["pos"].ssbo(26)
                L.bufs["index"].ssbo(27)
                F.dispatch(gl.groups(L.tri_count), 1, 1)
            F.barrier(BARRIER)
            self._support(origin, cell, dim_x, dim_y, 0, 0.0, self.support_info)
        else:
            self._support((0.0, 0.0), 4.0, 0, 0, 0, 0.0, self.support_info)
            self.support.ssbo(24)
        self.support_info.ubo(1)

        ng = gl.groups(n)
        tg = gl.groups(self.tri_count)
        P = K.prog

        def run(name, groups):
            F.useProgram(P[name])
            F.dispatch(groups, 1, 1)
            F.barrier(BARRIER)

        band = max(2, math.ceil(FEEL["corner_band"] / max(spacing, 1.0))) if FEEL["corners_up"] else 0
        GL.glProgramUniform1i(P["collide"], K.loc_band, band if FEEL["corners_collide"] else 0)
        run("frameBegin", ng)
        solve, limit = P["solveBatch"], P["limitBatch"]
        loc_s, loc_l = K.loc_batch["solveBatch"], K.loc_batch["limitBatch"]
        batches = [b for b in self.batches if b[1] > 0]
        structural = [b for b in self.batches[:8 if FEEL["limit_shear"] else 4] if b[1] > 0]
        tether = FEEL["tether"] and grabbing
        if tether:
            self.bufs["uv"].ssbo(22)
            anchor = int(self.grab_center)
        for _ in range(SUBSTEPS):
            run("integrate", ng)
            run("clearBuckets", gl.groups(HASH_SIZE))
            run("countTriangles", tg)
            run("scanBuckets", 1)
            run("fillTriangles", tg)
            for _ in range(ITERATIONS):
                F.useProgram(solve)
                for off, cnt in batches:
                    F.uniform2ui(loc_s, off, cnt)
                    F.dispatch(gl.groups(cnt), 1, 1)
                    F.barrier(BARRIER)
                run("applyTargets", ng)
                if tether:
                    F.useProgram(P["tether"])
                    GL.glUniform1ui(K.loc_tether[0], anchor)
                    GL.glUniform2f(K.loc_tether[1], *self.size)
                    GL.glUniform1f(K.loc_tether[2], FEEL["tether_slack"])
                    F.dispatch(ng, 1, 1)
                    F.barrier(BARRIER)
                run("collide", ng)
                run("applyDeltas", ng)
                run("collide", ng)
                run("applyDeltas", ng)
            F.useProgram(limit)
            for _ in range(FEEL["limit_passes"]):
                for off, cnt in structural:
                    F.uniform2ui(loc_l, off, cnt)
                    F.dispatch(gl.groups(cnt), 1, 1)
                    F.barrier(BARRIER)
            F.useProgram(P["friction"])
            GL.glUniform1f(K.loc_slide, FEEL["slide"])
            F.dispatch(ng, 1, 1)
            F.barrier(BARRIER)
        run("frameEnd", ng)
        rg = gl.groups(self.render_count)
        F.useProgram(P["refine"])
        F.uniform4ui(K.loc_R["refine"], self.render_cols, self.render_rows, REFINE, self.render_count)
        F.dispatch(rg, 1, 1)
        F.barrier(BARRIER)
        F.useProgram(P["refineNormals"])
        F.uniform4ui(K.loc_R["refineNormals"], self.render_cols, self.render_rows, REFINE, self.render_count)
        F.dispatch(rg, 1, 1)
        F.barrier(BARRIER | GL.GL_VERTEX_ATTRIB_ARRAY_BARRIER_BIT | GL.GL_ELEMENT_ARRAY_BARRIER_BIT)
        F.useProgram(0)

        self._ensure_readback(n)
        GL.glCopyNamedBufferSubData(B["stats"].id, self.readback, 0, 0, 16)
        GL.glCopyNamedBufferSubData(B["pos"].id, self.readback, 0, 16, n * 16)
        self.fence = GL.glFenceSync(GL.GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
        self.pending = (n, grabbing or flatten_old > 0 or self.guided, self.wake_gen)

    def _ensure_readback(self, n):
        import ctypes
        need = 16 + n * 16
        if self.readback and self.readback_size >= need:
            return
        if self.readback:
            GL.glUnmapNamedBuffer(self.readback)
            GL.glDeleteBuffers(1, [self.readback])
        flags = GL.GL_MAP_READ_BIT | GL.GL_MAP_PERSISTENT_BIT | GL.GL_MAP_COHERENT_BIT
        ids = np.zeros(1, np.uint32)
        GL.glCreateBuffers(1, ids)
        self.readback = int(ids[0])
        GL.glNamedBufferStorage(self.readback, need, None, flags)
        ptr = GL.glMapNamedBufferRange(self.readback, 0, need, flags)
        self.readback_size = need
        raw = (ctypes.c_uint8 * need).from_address(ptr if isinstance(ptr, int) else ctypes.cast(ptr, ctypes.c_void_p).value)
        self.readback_view = np.ctypeslib.as_array(raw)

    def collect(self):
        """Reads the results of the last queued frame (waits only if the GPU
        is still busy with it) and decides whether the cloth can sleep."""
        if self.pending is None:
            return
        n, busy, gen = self.pending
        self.pending = None
        GL.glClientWaitSync(self.fence, GL.GL_SYNC_FLUSH_COMMANDS_BIT, 1_000_000_000)
        GL.glDeleteSync(self.fence)
        self.fence = None
        view = self.readback_view
        stats = view[:16].view(np.uint32)
        max_move2 = float(view[:4].view(np.float32)[0])
        self.latest = view[16:16 + n * 16].view(np.float32).reshape(n, 4)[:, :3].copy()
        self.motion = stats[1] / 1000.0 / n
        self.impact = stats[2] / 1000.0 / n
        if gen != self.wake_gen:
            return          # woken (grab, move, design change...) since that frame
        if busy:
            self.calm_frames = self.idle_frames = 0
        else:
            self.idle_frames += 1
            thr = 0.04 if self.idle_frames > 90 else 0.0004
            self.calm_frames = self.calm_frames + 1 if max_move2 < thr else 0
            if self.idle_frames > 300 or self.calm_frames >= 31:
                self.awake = False

    def _support(self, origin, cell, dim_x, dim_y, tri_count, lower_thickness, buf):
        buf.write(np.frombuffer(struct.pack(_SUPPORT_FMT, float(origin[0]), float(origin[1]), float(cell),
                                            int(dim_x), int(dim_y), int(tri_count), float(lower_thickness), 0),
                                np.uint8))

    def refine_only(self):
        """Rebuilds the render mesh from the current positions (after a load)."""
        B, F, K = self.bufs, self.f, self.K
        self.params.write(np.frombuffer(struct.pack(_SIM_FMT, self.cols * self.rows, self.cols, self.rows,
                                                    self.tri_count, *([0.0] * 5), 0, *([0.0] * 6), *([0.0] * 3),
                                                    HASH_SIZE, 0, 0, *([0.0] * 8), 1, 0.0), np.uint8))
        self.params.ubo(0)
        B["pos"].ssbo(0)
        B["ground"].ssbo(2)
        B["vertex"].ssbo(21)
        rg = gl.groups(self.render_count)
        for name in ("refine", "refineNormals"):
            F.useProgram(K.prog[name])
            F.uniform4ui(K.loc_R[name], self.render_cols, self.render_rows, REFINE, self.render_count)
            F.dispatch(rg, 1, 1)
            F.barrier(BARRIER | GL.GL_VERTEX_ATTRIB_ARRAY_BARRIER_BIT)
        F.useProgram(0)

    def delete(self):
        if self.readback:
            GL.glUnmapNamedBuffer(self.readback)
            GL.glDeleteBuffers(1, [self.readback])
            self.readback = 0
        for b in list(self.bufs.values()) + [self.params, self.support_info, self.support, self.icon_buf,
                                              getattr(self, "rim_buf", None), getattr(self, "render_index_buf", None),
                                              *self.support_infos]:
            if b is not None:
                b.delete()
        self.bufs = {}
