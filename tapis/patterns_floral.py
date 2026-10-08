"""Persian and Heriz rug styles (ported from Tapis 0.2.7, MIT).

Specs: patterns_a.md (§2 derived palette, §3 Persian main, §4/5 guard stripe +
border band, F2 field quarter, F3 motifs + links, F4 shared helpers H1..H6) and
patterns_b.md (§11..§14 Heriz).

Entry points (called by the dispatcher in ``patterns.py``)::

    draw_persian(p: QPainter, rect: QRectF, pal: dict[str, QColor], rng)
    draw_heriz(p, rect, pal, rng)

``rect`` is the pattern rect in the dispatcher's user space (top-left origin,
y down, x along the long side).  ``pal`` holds the 5 slots field, border,
accent, light, dark.  ``rng`` is the shared SplitMix64 (only ``next_double()``
and ``next_u64()`` are used, in the original consumption order; the only RNG
user of both styles is ``_place_motifs``).

Core Graphics semantics are emulated by the small ``_Ctx`` wrapper: a current
fill/stroke colour, line width, cap and join that persist until changed and
are saved/restored with the graphics state; ``fill_stroke`` = CGContextDrawPath
(.fillStroke) = non-zero fill then stroke.  Pure function of its inputs, only
paints through the given QPainter (thread-safe on a QImage).
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QTransform

PI = math.pi
TWO_PI = 6.283185307179586

_CAP_ROUND = Qt.PenCapStyle.RoundCap
_CAP_SQUARE = Qt.PenCapStyle.SquareCap
_JOIN_ROUND = Qt.PenJoinStyle.RoundJoin
_JOIN_MITER = Qt.PenJoinStyle.MiterJoin
_WINDING = Qt.FillRule.WindingFill
_EVEN_ODD = Qt.FillRule.OddEvenFill
_INTERSECT = Qt.ClipOperation.IntersectClip
# CG miter limit 10 (miter length / line width); Qt measures from the join
# point in pen widths, i.e. half of that.
_MITER_LIMIT = 5.0


# --------------------------------------------------------------------------
# small numeric helpers
# --------------------------------------------------------------------------

def _rha(x: float) -> int:
    """Swift ``.rounded()`` / fcvtas: round half away from zero."""
    return int(math.copysign(math.floor(abs(x) + 0.5), x))


def _rgb(c) -> tuple:
    if isinstance(c, QColor):
        r, g, b, _a = c.getRgbF()
        return (r, g, b)
    return (float(c[0]), float(c[1]), float(c[2]))


def _lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def _qc(rgb, a=1.0) -> QColor:
    def cl(v):
        return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)
    return QColor.fromRgbF(cl(rgb[0]), cl(rgb[1]), cl(rgb[2]), cl(a))


def _inset(r, d):
    """CGRectInset(r, d, d) on (x, y, w, h) tuples."""
    return (r[0] + d, r[1] + d, r[2] - 2 * d, r[3] - 2 * d)


def _derive(pal) -> dict:
    """Derived palette 0x100043f64 (patterns_a §2)."""
    field, border, accent = _rgb(pal["field"]), _rgb(pal["border"]), _rgb(pal["accent"])
    light, dark = _rgb(pal["light"]), _rgb(pal["dark"])
    return {
        "field": field, "border": border, "accent": accent, "light": light, "dark": dark,
        "gold": _lerp(light, (200 / 255, 144 / 255, 46 / 255), 0.72),
        "green": _lerp(accent, (85 / 255, 122 / 255, 78 / 255), 0.65),
        "paleField": _lerp(field, light, 0.45),
        "deepBorder": _lerp(border, dark, 0.30),
    }


# --------------------------------------------------------------------------
# path helpers
# --------------------------------------------------------------------------

def _poly(pts, close=True) -> QPainterPath:
    """CGMutablePath.addLines(between:) (+ closeSubpath)."""
    path = QPainterPath()
    path.setFillRule(_WINDING)
    path.moveTo(pts[0][0], pts[0][1])
    for x, y in pts[1:]:
        path.lineTo(x, y)
    if close:
        path.closeSubpath()
    return path


def _ellipse_path(*rects) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(_WINDING)
    for r in rects:
        path.addEllipse(QRectF(*r))
    return path


def _rect_path(*rects) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(_WINDING)
    for r in rects:
        path.addRect(QRectF(*r))
    return path


def _mirror4(P):
    """H1 0x100046eec: quarter outline -> full closed outline (4n points)."""
    R = list(P)
    R += [(x, -y) for (x, y) in reversed(P)]
    R += [(-x, -y) for (x, y) in P]
    R += [(-x, y) for (x, y) in reversed(P)]
    return R


def _scallop_path(pts, step, amp) -> QPainterPath:
    """H2 0x100047150: closed path with outward quad bumps."""
    path = QPainterPath()
    path.setFillRule(_WINDING)
    path.moveTo(pts[0][0], pts[0][1])
    n = len(pts)
    for i in range(n):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy)
        if L <= 0.5:
            continue
        nx, ny = -dy / L, dx / L
        mx, my = (ax + bx) * 0.5, (ay + by) * 0.5
        if mx * nx + my * ny < 0:
            nx, ny = -nx, -ny
        segs = max(1, _rha(L / step))
        bump = amp * (L / segs)
        ox, oy = nx * bump, ny * bump
        for k in range(segs):
            t0 = k / segs
            t1 = (k + 1) / segs
            p0x, p0y = ax + dx * t0, ay + dy * t0
            p1x, p1y = ax + dx * t1, ay + dy * t1
            path.quadTo(ox + (p0x + p1x) * 0.5, oy + (p0y + p1y) * 0.5, p1x, p1y)
    path.closeSubpath()
    return path


def _stepped_diamond(n, cx, cy, hw, hh) -> QPainterPath:
    """H4 0x10004dd80: closed stepped diamond centred at (cx, cy)."""
    Q = []
    for i in range(n):
        y = (1.0 - i / n) * (-hh)
        Q.append(((hw * i) / n, y))
        Q.append(((hw * (i + 1)) / n, y))
    Q.append((hw, 0.0))
    R = _mirror4(Q)
    return _poly([(x + cx, y + cy) for (x, y) in R], True)


def _stairify(P, closed, step):
    """H6 0x10004ea08: turn diagonals into stairs (horizontal first)."""
    out = [P[0]]
    n = len(P)
    segs = n if closed else n - 1
    for i in range(segs):
        ax, ay = P[i]
        bx, by = P[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        adx, ady = abs(dx), abs(dy)
        if adx < 0.5 or ady < 0.5:
            out.append((bx, by))
            continue
        k = _rha(max(adx, ady) / step)
        if k < 1:
            k = 1
        for j in range(k):
            t0 = j / k
            t1 = (j + 1) / k
            out.append((ax + dx * t1, ay + dy * t0))
            out.append((ax + dx * t1, ay + dy * t1))
    return out


def _bumped_line(A, B, ts, bump, toward):
    """Persian §3.1: straight line A->B with rectangular notches toward `toward`."""
    dx, dy = B[0] - A[0], B[1] - A[1]
    L = math.hypot(dx, dy)
    nx, ny = -dy / L, dx / L
    mx, my = (A[0] + B[0]) * 0.5, (A[1] + B[1]) * 0.5
    if (toward[0] - mx) * nx + (toward[1] - my) * ny < 0:
        nx, ny = -nx, -ny
    ox, oy = nx * bump, ny * bump
    pts = [A]
    for t0, t1 in ts:
        p0 = (A[0] + dx * t0, A[1] + dy * t0)
        p1 = (A[0] + dx * t1, A[1] + dy * t1)
        pts += [p0, (p0[0] + ox, p0[1] + oy), (p1[0] + ox, p1[1] + oy), p1]
    pts.append(B)
    return pts


# --------------------------------------------------------------------------
# CGContext emulation
# --------------------------------------------------------------------------

class _Ctx:
    """Minimal CGContext-like state machine over a QPainter."""

    __slots__ = ("p", "fill", "stroke", "lw", "cap", "join", "_stack")

    def __init__(self, p: QPainter):
        self.p = p
        self.fill = QColor(0, 0, 0)
        self.stroke = QColor(0, 0, 0)
        self.lw = 1.0
        self.cap = _CAP_ROUND          # builder: lineCap round, lineJoin round
        self.join = _JOIN_ROUND
        self._stack = []

    # state
    def save(self):
        self.p.save()
        self._stack.append((self.fill, self.stroke, self.lw, self.cap, self.join))

    def restore(self):
        self.p.restore()
        self.fill, self.stroke, self.lw, self.cap, self.join = self._stack.pop()

    def translate(self, x, y):
        self.p.translate(x, y)

    def rotate(self, a):
        if a:
            self.p.rotate(math.degrees(a))

    def scale(self, sx, sy):
        self.p.scale(sx, sy)

    def set_fill(self, rgb, a=1.0):
        self.fill = _qc(rgb, a)

    def set_stroke(self, rgb, a=1.0):
        self.stroke = _qc(rgb, a)

    def set_lw(self, w):
        self.lw = w

    def _pen(self) -> QPen:
        pen = QPen(self.stroke)
        pen.setWidthF(self.lw)
        pen.setCapStyle(self.cap)
        pen.setJoinStyle(self.join)
        pen.setMiterLimit(_MITER_LIMIT)
        return pen

    # drawing
    def fill_path(self, path: QPainterPath, even_odd=False):
        path.setFillRule(_EVEN_ODD if even_odd else _WINDING)
        self.p.fillPath(path, self.fill)

    def stroke_path(self, path: QPainterPath):
        self.p.strokePath(path, self._pen())

    def fill_stroke(self, path: QPainterPath):
        self.fill_path(path)
        self.stroke_path(path)

    def fill_rect(self, r):
        self.fill_path(_rect_path(r))

    def stroke_rect(self, r):
        self.stroke_path(_rect_path(r))

    def fill_ellipse(self, r):
        self.fill_path(_ellipse_path(r))

    def clip_rect(self, r):
        self.p.setClipRect(QRectF(*r), _INTERSECT)

    def clip_path(self, path: QPainterPath, even_odd=False):
        path.setFillRule(_EVEN_ODD if even_odd else _WINDING)
        self.p.setClipPath(path, _INTERSECT)

    def ring(self, R, t, rgb, a=1.0):
        """addRect(R) + addRect(inset(R, t)), even-odd fill."""
        self.set_fill(rgb, a)
        self.fill_path(_rect_path(R, _inset(R, t)), even_odd=True)


# --------------------------------------------------------------------------
# shared: motif placement (H3) and links (F3)
# --------------------------------------------------------------------------

class _Motif:
    __slots__ = ("x", "y", "r", "zone", "kind", "ci", "angle")

    def __init__(self, x, y, r, zone, kind, ci, angle):
        self.x, self.y, self.r = x, y, r
        self.zone, self.kind, self.ci, self.angle = zone, kind, ci, angle


def _zone_of(zones, x, y):
    pt = QPointF(x, y)
    for i, z in enumerate(zones):
        path = z[0]
        if path is None or path.contains(pt):
            return i
    return len(zones) - 1


_PROBE = [(math.cos(k * PI / 4), math.sin(k * PI / 4)) for k in range(8)]


def _place_motifs(weights, zones, W, H, rMin, rMax, rng):
    """H3 0x100047300: seeded dart throwing.  zones = [(path, fill, colours, outline)].
    3 draws per rejected attempt, 6 per accepted; stops after 1500 consecutive fails."""
    for z in zones:
        if z[0] is not None:
            z[0].setFillRule(_WINDING)
    cell = rMax * 2.4
    gx = int(W / cell)
    gy = int(H / cell)
    nx = gx + 1
    buckets = [[] for _ in range(nx * (gy + 1))]
    total = 0.0
    for w in weights:
        total += w
    nw = len(weights)
    placed = []
    fails = 0
    rnd = rng.next_double
    while True:
        x = 0.0 + W * rnd()
        y = 0.0 + H * rnd()
        r = rMin + (rMax - rMin) * rnd()
        ok = (x < W - r * 0.9) and (y < H - r * 0.9)
        cx = min(int(x / cell), gx)
        cy = min(int(y / cell), gy)
        if ok:
            for ix in range(max(cx - 1, 0), min(cx + 1, gx) + 1):
                for iy in range(max(cy - 1, 0), min(cy + 1, gy) + 1):
                    for j in buckets[iy * nx + ix]:
                        p = placed[j]
                        if math.hypot(p.x - x, p.y - y) < (r + p.r) * 1.04:
                            ok = False
                            break
                    if not ok:
                        break
                if not ok:
                    break
        zi = -1
        cols = ()
        if ok:
            zi = _zone_of(zones, x, y)
            cols = zones[zi][2]
            ok = len(cols) != 0
            if ok:
                rr = r * 1.25
                for c, s in _PROBE:
                    if _zone_of(zones, x + c * rr, y + s * rr) != zi:
                        ok = False
                        break
        if not ok:
            fails += 1
            if fails == 1500:
                break
            continue
        u4 = rnd()
        kind = 0
        if nw >= 2:
            t = total * u4
            kind = nw - 1
            for i in range(nw - 1):
                if weights[i] > t:
                    kind = i
                    break
                t -= weights[i]
        buckets[cy * nx + cx].append(len(placed))
        z5 = rng.next_u64()
        ci = z5 % max(len(cols), 1)
        angle = rnd() * TWO_PI + 0.0
        placed.append(_Motif(x, y, r, zi, kind, ci, angle))
        fails = 0
    return placed


def _make_links(m, k):
    """F3 0x100047dd0: nearest same-zone neighbour pairs, deduped, insertion order."""
    links = []
    seen = set()
    n = len(m)
    for i in range(n):
        mi = m[i]
        best = -1
        bestd = math.inf
        for j in range(n):
            mj = m[j]
            if j == i or mj.zone != mi.zone:
                continue
            d = math.hypot(mj.x - mi.x, mj.y - mi.y)
            if d < bestd:
                bestd, best = d, j
        if best < 0:
            continue
        if not (bestd < k * (mi.r + m[best].r)):
            continue
        a, b = min(i, best), max(i, best)
        key = a * 100000 + b
        if key not in seen:
            seen.add(key)
            links.append((a, b))
    return links


# ==========================================================================
# PERSIAN
# ==========================================================================

_PERSIAN_WEIGHTS = [3.0, 1.5, 2.0, 1.5, 1.0, 1.0]
_PENTA = [(math.cos(math.radians(a)), math.sin(math.radians(a))) for a in (0, 72, 144, 216, 288)]


def _fan_path(k) -> QPainterPath:
    """Palmette/pomegranate fan body: 57-point scalloped fan, closed."""
    A = QPainterPath()
    A.setFillRule(_WINDING)
    A.moveTo(0.08 * k, -0.08 * k)
    for j in range(57):
        u = j / 56.0
        ang = u * 4.39822972 - 2.19911486
        rr = math.sqrt(abs(math.sin(u * 7.0 * PI))) * 0.18 + 0.82
        A.lineTo(0.5 * k + 0.4 * k * math.cos(ang) * rr, 0.4 * k * math.sin(ang) * rr)
    A.lineTo(0.08 * k, 0.08 * k)
    A.closeSubpath()
    return A


def _palmette_body(ctx: _Ctx, k, dark, tip_fill, body_fill, inner_fill, seed_fill):
    """Shared palmette drawing in local coords (+x = pointing direction).
    Stroke colour / width must already be set by the caller."""
    A = _fan_path(k)
    tip = QPainterPath()
    tip.moveTo(0.78 * k, -0.07 * k)
    tip.quadTo(0.98 * k, -0.06 * k, 1.08 * k, 0.0)
    tip.quadTo(0.98 * k, 0.06 * k, 0.78 * k, 0.07 * k)
    ctx.set_fill(tip_fill)
    ctx.fill_stroke(tip)
    ctx.set_fill(body_fill)
    ctx.fill_stroke(A)
    inner = QTransform(0.62, 0.0, 0.0, 0.62, 0.5 * k - 0.62 * 0.45 * k, 0.0).map(A)
    ctx.set_fill(inner_fill)
    ctx.fill_stroke(inner)
    ctx.set_fill(seed_fill)
    for a in (-0.72, -0.36, 0.0, 0.36, 0.72):
        ctx.save()
        ctx.translate(0.26 * k, 0.0)
        ctx.rotate(a)
        ctx.fill_ellipse((0.04 * k, -0.035 * k, 0.26 * k, 0.07 * k))
        ctx.restore()
    ctx.set_fill(dark)
    ctx.fill_ellipse((0.2 * k, -0.045 * k, 0.09 * k, 0.09 * k))


def _p_rosette(ctx: _Ctx, D, c, k, dist, r, petal_fill, eye_r, eye_fill, lw, a0):
    """Border-band rosette (patterns_a §5)."""
    ctx.set_lw(lw)
    ctx.set_stroke(D["dark"], 0.85)
    ctx.set_fill(petal_fill)
    rects = []
    for j in range(k):
        a = a0 + j * TWO_PI / k
        rects.append((c[0] + dist * math.cos(a) - r, c[1] + dist * math.sin(a) - r, 2 * r, 2 * r))
    ctx.fill_stroke(_ellipse_path(*rects))
    ctx.set_fill(eye_fill)
    ctx.fill_stroke(_ellipse_path((c[0] - eye_r, c[1] - eye_r, 2 * eye_r, 2 * eye_r)))


def _band_segments(R, b):
    x, y, w, h = _inset(R, b / 2)
    x0, y0, x1, y1 = x, y, x + w, y + h
    return [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]


def _persian_guard(ctx: _Ctx, R, D, b):
    """0x10004406c: light ring, green sine vine, 5-petal flowers."""
    ctx.ring(R, b, D["light"])
    ctx.save()
    ctx.clip_path(_rect_path(R, _inset(R, b)), even_odd=True)
    u = 0.26 * b
    petal_r = 0.42 * u
    petal_d = 0.52 * u
    eye_r = 0.30 * u
    flower_lw = max(0.09 * u, 0.6)
    for (p0x, p0y), (p1x, p1y) in _band_segments(R, b):
        dx, dy = p1x - p0x, p1y - p0y
        length = math.hypot(dx, dy)
        n = max(_rha(length / (1.3 * b)), 2)
        N = 8 * n
        nx, ny = -dy / length, dx / length
        vine = QPainterPath()
        for i in range(N + 1):
            t = i / N
            a = b * math.sin(t * PI * n + t * PI * n) * 0.2
            px, py = p0x + dx * t + nx * a, p0y + dy * t + ny * a
            if i == 0:
                vine.moveTo(px, py)
            else:
                vine.lineTo(px, py)
        ctx.set_stroke(D["green"], 1.0)
        ctx.set_lw(0.05 * b)
        ctx.stroke_path(vine)
        for i in range(n):
            t = (i + 0.5) / n
            cx, cy = p0x + dx * t, p0y + dy * t
            ctx.set_lw(flower_lw)
            ctx.set_stroke(D["dark"], 0.85)
            ctx.set_fill(D["field"] if i % 2 == 0 else D["deepBorder"])
            ctx.fill_stroke(_ellipse_path(*[
                (cx + petal_d * co - petal_r, cy + petal_d * si - petal_r, 2 * petal_r, 2 * petal_r)
                for co, si in _PENTA]))
            ctx.set_fill(D["gold"])
            ctx.fill_stroke(_ellipse_path((cx - eye_r, cy - eye_r, 2 * eye_r, 2 * eye_r)))
    ctx.restore()
    ctx.set_stroke(D["dark"], 0.9)
    ctx.set_lw(0.08 * b)
    ctx.stroke_rect(_inset(R, 0.04 * b))
    ctx.set_stroke(D["dark"], 0.9)
    ctx.set_lw(0.08 * b)
    ctx.stroke_rect(_inset(R, 0.96 * b))


def _persian_border(ctx: _Ctx, R, D, b):
    """0x100044aa8: deep-border band with vine, palmettes, rosettes, leaves."""
    vine_c = _lerp(D["field"], D["deepBorder"], 0.2)
    ctx.set_fill(D["deepBorder"])
    ctx.fill_path(_rect_path(R, _inset(R, b)), even_odd=True)
    ctx.save()
    ctx.clip_path(_rect_path(R, _inset(R, b)), even_odd=True)
    dark = D["dark"]
    for (p0x, p0y), (p1x, p1y) in _band_segments(R, b):
        dx, dy = p1x - p0x, p1y - p0y
        L = math.hypot(dx, dy)
        n0 = _rha(L / (1.1 * b))
        n = 2 if n0 < 2 else n0 + (n0 & 1)
        ux, uy = dx / L, dy / L
        nx, ny = -uy, ux
        theta = math.atan2(uy, ux)
        thetaN = math.atan2(ux, -uy)

        N = 16 * n
        pts = []
        for i in range(N + 1):
            t = i / N
            a = b * math.sin(t * PI * n) * 0.3
            pts.append((p0x + dx * t - uy * a, p0y + dy * t + ux * a))
        ctx.set_stroke(vine_c, 1.0)
        ctx.set_lw(0.022 * b)
        ctx.stroke_path(_poly(pts, close=False))

        for i in range(n):
            t = i / n
            bx, by = p0x + dx * t, p0y + dy * t
            ctx.save()
            if i % 2 == 0:
                s = 0.72 * b
                ctx.translate(bx - 0.36 * b * nx, by - 0.36 * b * ny)
                ctx.rotate(thetaN)
                fan_fill, ray_fill = D["light"], D["gold"]
            else:
                s = 0.58 * b
                ctx.translate(bx + 0.30 * b * nx, by + 0.30 * b * ny)
                ctx.rotate(thetaN + PI)
                fan_fill, ray_fill = D["gold"], D["light"]
            ctx.set_lw(max(0.035 * s, 0.6))
            ctx.set_stroke(dark, 0.9)
            _palmette_body(ctx, s, dark, D["field"], fan_fill, D["field"], ray_fill)
            ctx.restore()

            tm = t + 0.5 / n
            mx, my = p0x + dx * tm, p0y + dy * tm
            _p_rosette(ctx, D, (mx - 0.27 * b * nx, my - 0.27 * b * ny), 5, 0.052 * b, 0.042 * b,
                       D["field"], 0.03 * b, D["gold"], max(0.009 * b, 0.6), 0.3)
            _p_rosette(ctx, D, (mx + 0.27 * b * nx, my + 0.27 * b * ny), 6, 0.052 * b, 0.042 * b,
                       D["paleField"], 0.03 * b, D["deepBorder"], max(0.009 * b, 0.6), 0.0)
            _p_rosette(ctx, D, (mx, my), 5, 0.0416 * b, 0.0336 * b,
                       D["light"], 0.024 * b, D["field"], max(0.0072 * b, 0.6), 0.0)
            for dt, dn, col in ((-0.27, -0.4, D["paleField"]), (0.27, 0.4, D["field"]),
                                (-0.27, 0.4, D["gold"]), (0.27, -0.4, D["accent"])):
                tt = tm + dt / n
                cx, cy = p0x + dx * tt, p0y + dy * tt
                off = b * dn
                _p_rosette(ctx, D, (cx + nx * off, cy + ny * off), 5, 0.0286 * b, 0.0231 * b,
                           col, 0.0165 * b, dark, max(0.00495 * b, 0.6), 0.0)
            for sgn, ang in ((1.0, theta), (-1.0, theta + PI)):
                ctx.save()
                ctx.translate(mx + sgn * 0.08 * b * ux, my + sgn * 0.08 * b * uy)
                ctx.rotate(ang)
                leaf = QPainterPath()
                leaf.moveTo(0.0, 0.0)
                leaf.quadTo(0.081 * b, -0.056 * b, 0.18 * b, 0.0)
                leaf.quadTo(0.081 * b, 0.056 * b, 0.0, 0.0)
                ctx.set_fill(D["green"])
                ctx.set_stroke(dark, 0.9)
                ctx.set_lw(max(0.0063 * b, 0.6))
                ctx.fill_stroke(leaf)
                rib = QPainterPath()
                rib.moveTo(0.018 * b, 0.0)
                rib.lineTo(0.153 * b, 0.0)
                ctx.stroke_path(rib)
                ctx.restore()
    ctx.restore()


def _leaf_at(ctx: _Ctx, px, py, rot, Lf, W, col, O):
    ctx.save()
    ctx.translate(px, py)
    ctx.rotate(rot)
    path = QPainterPath()
    path.moveTo(0.0, 0.0)
    path.quadTo(0.45 * Lf, -1.6 * W, Lf, 0.0)
    path.quadTo(0.45 * Lf, 1.6 * W, 0.0, 0.0)
    ctx.set_fill(col)
    ctx.set_stroke(O, 0.9)
    ctx.set_lw(max(W * 0.18, 0.6))
    ctx.fill_stroke(path)
    rib = QPainterPath()
    rib.moveTo(0.1 * Lf, 0.0)
    rib.lineTo(0.85 * Lf, 0.0)
    ctx.stroke_path(rib)
    ctx.restore()


_HEX = (0.0, 1.04719755, 2.0943951, 3.14159265, 4.1887902, 5.23598776)


def _persian_motif(ctx: _Ctx, m: _Motif, colors, O):
    """0x100047fa4: one Persian field motif (6 kinds)."""
    n = len(colors)
    ci = m.ci
    c1 = colors[ci % n]
    c2 = colors[(ci + 3) % n]
    x, y, r, ang = m.x, m.y, m.r, m.angle
    s, c = math.sin(ang), math.cos(ang)
    kind = m.kind
    if kind == 0:
        ctx.set_lw(max(0.09 * r, 0.6))
        ctx.set_stroke(O, 0.85)
        ctx.set_fill(c1)
        cnt = 5 + (ci % 2)
        rects = []
        for k in range(cnt):
            a = ((k / cnt) * 2) * PI + ang
            cx = x + (r * math.cos(a)) * 0.52
            cy = y + (r * math.sin(a)) * 0.52
            rects.append((cx - 0.42 * r, cy - 0.42 * r, 0.84 * r, 0.84 * r))
        ctx.fill_stroke(_ellipse_path(*rects))
        ctx.set_fill(c2)
        ctx.fill_stroke(_ellipse_path((x - 0.3 * r, y - 0.3 * r, 0.6 * r, 0.6 * r)))
    elif kind == 1:
        L = 1.9 * r
        ctx.save()
        ctx.translate(x - 0.95 * r * c, y - 0.95 * r * s)
        ctx.rotate(ang)
        ctx.set_lw(max(L * 0.035, 0.6))
        ctx.set_stroke(O, 0.9)
        _palmette_body(ctx, L, O, c2, c1, c2, O)
        ctx.restore()
    elif kind == 2:
        px, py = x - 0.8 * r * c, y - 0.8 * r * s
        Lf, W = 1.8 * r, 0.3 * r
        _leaf_at(ctx, px, py, ang - 0.45, Lf, W, c1, O)
        _leaf_at(ctx, px, py, ang + 0.45, Lf, W, c2, O)
    elif kind == 3:
        px, py = x - r * c, y - r * s
        _leaf_at(ctx, px, py, ang, 2 * r, 0.55 * r, c1, O)
        _leaf_at(ctx, px + 0.5 * r * c, py + 0.5 * r * s, ang, 1.1 * r, 0.25 * r, c2, O)
    elif kind == 4:
        ctx.set_lw(max(0.08 * r, 0.6))
        ctx.set_stroke(O, 0.85)
        ctx.set_fill(c1)
        rects = []
        for a_off in _HEX:
            a = ang + a_off
            cx = x + (r * math.cos(a)) * 0.66
            cy = y + (r * math.sin(a)) * 0.66
            rects.append((cx - 0.24 * r, cy - 0.24 * r, 0.48 * r, 0.48 * r))
        ctx.fill_stroke(_ellipse_path(*rects))
        ctx.set_fill(c2)
        ctx.fill_ellipse((x - 0.3 * r, y - 0.3 * r, 0.6 * r, 0.6 * r))
    else:
        a, b = r, 0.9 * r
        P = QPainterPath()
        P.moveTo(x + r, y)
        for i in range(1, 240):
            th = (i / 240.0 + i / 240.0) * PI
            f = math.sqrt(abs(math.cos(th * 8.0 * 0.5))) * 0.25 + 0.75
            sn, cs = math.sin(th), math.cos(th)
            e = 1.0 / abs(math.sqrt((abs(cs) / a) ** 2 + (abs(sn) / b) ** 2))
            P.lineTo(x + f * (cs * e), y + f * (sn * e))
        P.closeSubpath()
        ctx.set_fill(c1)
        ctx.set_stroke(O, 0.85)
        ctx.set_lw(max(0.09 * r, 0.6))
        ctx.fill_stroke(P)
        q = 0.55 * r
        ctx.save()
        ctx.translate(x, y)
        ctx.set_lw(max(0.08 * q, 0.6))
        ctx.set_stroke(O, 0.9)
        ctx.set_fill(c2)
        for rot in _HEX:
            ctx.save()
            ctx.rotate(rot)
            ctx.fill_stroke(_ellipse_path((0.18 * q, -0.2 * q, 0.82 * q, 0.4 * q)))
            ctx.restore()
        ctx.set_fill(c1)
        ctx.fill_stroke(_ellipse_path((-0.36 * q, -0.36 * q, 0.72 * q, 0.72 * q)))
        ctx.set_fill(O)
        ctx.fill_ellipse((-0.15 * q, -0.15 * q, 0.3 * q, 0.3 * q))
        ctx.restore()


def _persian_field_quarter(ctx: _Ctx, layers, links, motifs, D,
                           pathA, pathB, pathC, pathD, pathE, U, a, b, c):
    """0x100049904: one mirrored quarter of the Persian field."""
    dark, light, field, gold = D["dark"], D["light"], D["field"], D["gold"]
    paleField, deepBorder = D["paleField"], D["deepBorder"]

    for L in reversed(layers):
        if L[0] is None:
            continue
        ctx.set_fill(L[1])
        ctx.fill_path(L[0])

    ctx.set_lw(U * 0.0022)
    for i, (ia, ib) in enumerate(links):
        p, q = motifs[ia], motifs[ib]
        dx, dy = q.x - p.x, q.y - p.y
        kk = 0.22 if i % 2 == 0 else -0.22
        mx, my = (p.x + q.x) * 0.5, (p.y + q.y) * 0.5
        path = QPainterPath()
        path.moveTo(p.x, p.y)
        path.quadTo(mx - kk * dy, my + kk * dx, q.x, q.y)
        ctx.set_stroke(layers[p.zone][3], 0.85)
        ctx.stroke_path(path)

    for m in motifs:
        _persian_motif(ctx, m, layers[m.zone][2], dark)

    def stroke(path, col, w):
        ctx.set_stroke(col, 1.0)
        ctx.set_lw(w)
        ctx.stroke_path(path)

    stroke(pathA, dark, U * 0.005)
    stroke(pathB, dark, U * 0.015)
    stroke(pathB, light, U * 0.008)
    stroke(pathC, dark, U * 0.012)
    stroke(pathC, light, U * 0.0055)
    stroke(pathD, dark, U * 0.010)
    stroke(pathD, light, U * 0.005)

    def palmette(tx, ty, rot, k, petal, body):
        ctx.save()
        ctx.translate(tx, ty)
        ctx.rotate(rot)
        ctx.set_lw(max(k * 0.035, 0.6))
        ctx.set_stroke(dark, 0.9)
        _palmette_body(ctx, k, dark, petal, body, petal, gold)
        ctx.restore()

    palmette(a - 0.055 * b, 0.0, 0.0, 0.11 * b, field, light)
    palmette(0.7 * b, 0.0, 0.0, 0.06 * b, deepBorder, light)

    stroke(pathE, dark, U * 0.02)
    stroke(pathE, gold, U * 0.013)
    ctx.set_fill(light)
    ctx.fill_path(pathE)

    palmette(0.35 * c, 0.0, 0.0, 1.45 * c, paleField, field)
    palmette(0.0, 0.35 * c, PI / 2, 1.45 * c, paleField, deepBorder)

    # centre rosette 0x10004bf44
    r = 0.5 * c
    ctx.save()
    ctx.translate(0.0, 0.0)
    ctx.set_lw(max(r * 0.08, 0.6))
    ctx.set_stroke(dark, 0.9)
    ctx.set_fill(field)
    for j in range(8):
        ctx.save()
        ctx.rotate(j * PI / 4)
        ctx.fill_stroke(_ellipse_path((0.18 * r, -0.2 * r, 0.82 * r, 0.4 * r)))
        ctx.restore()
    ctx.set_fill(gold)
    ctx.fill_stroke(_ellipse_path((-0.36 * r, -0.36 * r, 0.72 * r, 0.72 * r)))
    ctx.set_fill(dark)
    ctx.fill_ellipse((-0.15 * r, -0.15 * r, 0.3 * r, 0.3 * r))
    ctx.restore()


def draw_persian(p: QPainter, rect: QRectF, pal: dict, rng) -> None:
    """Persian style 0x10004c3f0 (patterns_a §3)."""
    ctx = _Ctx(p)
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    try:
        _persian(ctx, (rect.x(), rect.y(), rect.width(), rect.height()), _derive(pal), rng)
    finally:
        p.restore()


def _persian(ctx: _Ctx, rect, D, rng):
    S = rect[3] if rect[3] < rect[2] else rect[2]
    field, light, dark, deepBorder = D["field"], D["light"], D["dark"], D["deepBorder"]

    ctx.set_fill(field)
    ctx.fill_rect(rect)
    r1 = _inset(rect, 0.012 * S)
    _persian_guard(ctx, r1, D, 0.03 * S)
    r2 = _inset(r1, 0.03 * S)
    _persian_border(ctx, r2, D, 0.12 * S)
    r3 = _inset(r2, 0.12 * S)
    _persian_guard(ctx, r3, D, 0.03 * S)
    r4 = _inset(r3, 0.03 * S)
    ctx.ring(r4, 0.008 * S, field)
    r5 = _inset(r4, 0.008 * S)
    ctx.ring(r5, 0.004 * S, light)
    r6 = _inset(r5, 0.004 * S)
    ctx.set_fill(field)
    ctx.fill_rect(r6)
    hw = r6[2] / 2
    hh = r6[3] / 2

    # centre quatrefoil
    r = 0.11 * hh
    P_quatre = QPainterPath()
    P_quatre.setFillRule(_WINDING)
    for ex, ey in ((r, 0.0), (-r, 0.0), (0.0, r), (0.0, -r)):
        P_quatre.addEllipse(QRectF(ex - r, ey - r, 2 * r, 2 * r))
    P_quatre.addRect(QRectF(-r, -r, 2 * r, 2 * r))

    # pendant: lobed super-ellipse
    cx = 0.52 * hw
    a = 0.075 * hw
    b = 0.15 * hh
    inv = 1.0 / 1.3

    def rad(t):
        return 1.0 / ((abs(math.cos(t)) / a) ** 1.3 + (abs(math.sin(t)) / b) ** 1.3) ** inv

    P_pend = QPainterPath()
    P_pend.setFillRule(_WINDING)
    P_pend.moveTo(cx + rad(0.0), 0.0)
    for i in range(1, 240):
        t = (i / 240 + i / 240) * PI
        mm = math.sqrt(abs(math.cos(8 * t * 0.5))) * 0.12 + 0.88
        rr = rad(t)
        P_pend.lineTo(cx + rr * math.cos(t) * mm, rr * math.sin(t) * mm)
    P_pend.closeSubpath()

    stepS = 0.032 * S
    ptsA = _bumped_line((0.0, 0.62 * hh), (0.40 * hw, 0.0), [(0.28, 0.38), (0.60, 0.70)],
                        0.03 * S, (0.0, 0.0))
    pathA = _scallop_path(_mirror4(ptsA), stepS, 0.2)
    ptsB = _bumped_line((0.0, 0.92 * hh), (0.70 * hw, 0.0), [(0.20, 0.30), (0.55, 0.65)],
                        0.04 * S, (0.0, 0.0))
    pathB = _scallop_path(_mirror4(ptsB), stepS, 0.2)

    # corner piece
    P_corner = QPainterPath()
    P_corner.setFillRule(_WINDING)
    P_corner.moveTo(hw + 1, hh + 1)
    pts = _bumped_line((0.45 * hw, hh + 1), (hw + 1, 0.26 * hh), [(0.22, 0.34), (0.58, 0.70)],
                       0.035 * S, (hw, hh))
    P_corner.lineTo(pts[0][0], pts[0][1])
    n_pts = len(pts)
    for i in range(n_pts - 1):
        px, py = pts[i]
        qx, qy = pts[(i + 1) % n_pts]
        dx, dy = qx - px, qy - py
        L = math.hypot(dx, dy)
        if L <= 0.5:
            continue
        nx, ny = -dy / L, dx / L
        mx, my = (px + qx) * 0.5, (py + qy) * 0.5
        if (mx - hw) * nx + (my - hh) * ny < 0:
            nx, ny = -nx, -ny
        k = max(1, _rha(L / (0.032 * S)))
        amp = (L / k) * 0.22
        for j in range(k):
            ax, ay = px + dx * (j / k), py + dy * (j / k)
            bx, by = px + dx * ((j + 1) / k), py + dy * ((j + 1) / k)
            P_corner.quadTo(nx * amp + (ax + bx) * 0.5, ny * amp + (ay + by) * 0.5, bx, by)
    P_corner.closeSubpath()

    gold, green, accent, paleField = D["gold"], D["green"], D["accent"], D["paleField"]
    C8 = [deepBorder, deepBorder, paleField, light, gold, green, accent, dark]
    C6 = [field, paleField, light, gold, accent, field]
    C7 = [field, deepBorder, paleField, gold, accent, green, field]
    layers = [
        (P_quatre, light, [], dark),
        (P_pend, deepBorder, [], dark),
        (pathA, deepBorder, C6, _lerp(field, deepBorder, 0.3)),
        (pathB, field, C8, dark),
        (P_corner, light, C7, _lerp(field, dark, 0.3)),
        (None, field, C8, dark),
    ]

    motifs = _place_motifs(_PERSIAN_WEIGHTS, layers, hw, hh, 0.0075 * S, 0.015 * S, rng)
    links = _make_links(motifs, 2.6)

    midx, midy = r6[0] + r6[2] / 2, r6[1] + r6[3] / 2
    ctx.save()
    ctx.clip_rect(r6)
    for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        ctx.save()
        ctx.translate(midx, midy)
        ctx.scale(sx, sy)
        ctx.clip_rect((-0.5, -0.5, r6[2] * 0.5 + 1, r6[3] * 0.5 + 1))
        _persian_field_quarter(ctx, layers, links, motifs, D,
                               P_corner, pathB, pathA, P_pend, P_quatre,
                               S, 0.52 * hw, hw, 0.11 * hh)
        ctx.restore()
    ctx.restore()


# ==========================================================================
# HERIZ
# ==========================================================================

_HERIZ_WEIGHTS = [2.0, 2.0, 2.4, 1.4, 1.0, 1.2]
# latch-hook half outline (static table 0x100086b08 / 0x100086c28)
_K0 = [(0.0, -0.12), (0.22, -0.12), (0.22, -0.46), (0.42, -0.46), (0.42, -0.38), (0.6, -0.38),
       (0.6, -0.26), (0.76, -0.26), (0.76, -0.12), (0.92, -0.12), (0.92, 0.0)]
_DIAG = (PI / 4, 3 * PI / 4, 5 * PI / 4, 7 * PI / 4)


def _latch_points(s):
    K = [(x * s, y * s) for x, y in _K0]
    K += [(x, -y) for (x, y) in reversed(K)]
    return K


def _latch_hook(ctx: _Ctx, s, outer_fill, inner_fill, dark):
    """Latch hook (heriz_field step 5 / heriz_border (d)); stroke dark set here.
    Caller has translated/rotated and set MITER join."""
    K = _latch_points(s)
    ctx.set_fill(outer_fill)
    ctx.set_stroke(dark, 1.0)
    ctx.set_lw(max(s * 0.04, 0.8))
    ctx.fill_stroke(_poly(K, True))
    c0 = 0.32 * s
    ctx.set_fill(inner_fill)
    ctx.fill_stroke(_poly([(c0 + (x - c0) * 0.55, y * 0.55) for x, y in K], True))
    ctx.set_fill(dark)
    ctx.fill_rect((0.3 * s, -0.05 * s, 0.1 * s, 0.1 * s))


def _star16(rr) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(_WINDING)
    path.moveTo(rr, 0.0)
    for i in range(1, 16):
        R = rr if i % 2 == 0 else 0.76 * rr
        path.lineTo(R * math.cos(i * PI / 8), R * math.sin(i * PI / 8))
    path.closeSubpath()
    return path


def _star_rosette(ctx: _Ctx, rr, fill1, fill2, dark):
    """16-point star rosette with stepped diamond and dark square (alpha-1 dark stroke)."""
    ctx.save()
    ctx.translate(0.0, 0.0)
    ctx.join = _JOIN_MITER
    ctx.set_stroke(dark, 1.0)
    ctx.set_lw(max(0.08 * rr, 0.8))
    ctx.set_fill(fill1)
    ctx.fill_stroke(_star16(rr))
    ctx.set_fill(fill2)
    ctx.fill_stroke(_stepped_diamond(2, 0.0, 0.0, 0.5 * rr, 0.5 * rr))
    ctx.set_fill(dark)
    ctx.fill_rect((-0.12 * rr, -0.12 * rr, 0.24 * rr, 0.24 * rr))
    ctx.restore()


def _heriz_guard(ctx: _Ctx, R, D, b):
    """H5 0x10004e220: light ring with a chain of small stepped diamonds."""
    ctx.ring(R, b, D["light"])
    ctx.save()
    ctx.clip_path(_rect_path(R, _inset(R, b)), even_odd=True)
    for (x0, y0), (x1, y1) in _band_segments(R, b):
        dx, dy = x1 - x0, y1 - y0
        n = max(_rha(math.hypot(dx, dy) / (b * 1.1)), 2)
        for j in range(n):
            t = (j + 0.5) / n
            d = _stepped_diamond(2, x0 + dx * t, y0 + dy * t, b * 0.36, b * 0.36)
            ctx.set_fill(D["field"] if j % 2 == 0 else D["deepBorder"])
            ctx.set_stroke(D["dark"], 1.0)
            ctx.set_lw(b * 0.06)
            ctx.fill_stroke(d)
            t2 = (j + 1) / n
            px, py = x0 + dx * t2, y0 + dy * t2
            ctx.set_fill(D["gold"])
            ctx.fill_rect((px - b * 0.08, py - b * 0.08, b * 0.16, b * 0.16))
    ctx.restore()
    ctx.set_stroke(D["dark"], 0.9)
    ctx.set_lw(b * 0.08)
    ctx.stroke_rect(_inset(R, b * 0.04))
    ctx.set_stroke(D["dark"], 0.9)
    ctx.set_lw(b * 0.08)
    ctx.stroke_rect(_inset(R, b * 0.96))


def _heriz_border(ctx: _Ctx, R, X, bw):
    """0x10004ec58: wide band with stepped vine dashes, diamonds and latch-hook flowers."""
    dark = X["dark"]
    ctx.set_fill(X["deepBorder"])
    ctx.fill_path(_rect_path(R, _inset(R, bw)), even_odd=True)
    ctx.save()
    ctx.clip_path(_rect_path(R, _inset(R, bw)), even_odd=True)
    ctx.join = _JOIN_MITER
    s = 0.26 * bw
    rr = 0.20 * bw
    for (x0, y0), (x1, y1) in _band_segments(R, bw):
        dx, dy = x1 - x0, y1 - y0
        L = math.hypot(dx, dy)
        n = _rha(L / (1.3 * bw))
        n = n + (n & 1)
        n = 2 if n < 2 else n
        c = L / n
        ang = math.atan2(dy / L, dx / L)
        for k in range(n):
            ctx.save()
            ctx.translate(x0 + dx * k / n, y0 + dy * k / n)
            ctx.rotate(ang)
            # (a) stepped diagonal vine dash
            v = _stairify([(0.25 * c, -0.28 * bw), (0.75 * c, 0.28 * bw)], False, 0.09 * bw)
            ctx.set_stroke(dark, 1.0)
            ctx.set_lw(0.07 * bw)
            ctx.stroke_path(_poly(v, close=False))
            ctx.set_stroke(X["accent"] if k % 4 < 2 else X["gold"], 1.0)
            ctx.set_lw(0.04 * bw)
            ctx.stroke_path(_poly(v, close=False))
            # (b) two tiny diamonds, one path
            two = _stepped_diamond(2, 0.5 * c, -0.36 * bw, 0.07 * bw, 0.07 * bw)
            two.addPath(_stepped_diamond(2, 0.5 * c, 0.36 * bw, 0.07 * bw, 0.07 * bw))
            ctx.set_fill(X["field"])
            ctx.set_stroke(dark, 1.0)
            ctx.set_lw(0.015 * bw)
            ctx.fill_stroke(two)
            if k % 2 == 0:
                # (c) nested stepped diamond
                ctx.set_fill(X["light"])
                ctx.fill_path(_stepped_diamond(4, 0.0, 0.0, 0.48 * bw, 0.34 * bw))
                ctx.set_fill(X["field"])
                ctx.fill_stroke(_stepped_diamond(4, 0.0, 0.0, 0.44 * bw, 0.30 * bw))
                ctx.set_fill(X["accent"])
                ctx.fill_stroke(_stepped_diamond(3, 0.0, 0.0, 0.26 * bw, 0.17 * bw))
                ctx.set_fill(X["light"])
                ctx.fill_stroke(_stepped_diamond(2, 0.0, 0.0, 0.10 * bw, 0.07 * bw))
            else:
                # (d) 4 latch hooks + rosette
                pc = X["green"] if k % 4 == 1 else X["gold"]
                for a in _DIAG:
                    ctx.save()
                    ctx.translate(bw * math.cos(a) * 0.12, bw * math.sin(a) * 0.12)
                    ctx.rotate(a)
                    ctx.join = _JOIN_MITER
                    _latch_hook(ctx, s, pc, X["light"], dark)
                    ctx.restore()
                _star_rosette(ctx, rr, X["light"], X["field"], dark)
            ctx.restore()
    ctx.restore()


def _heriz_motif(ctx: _Ctx, m: _Motif, cols, dark):
    """0x10004fe64: one small geometric Heriz motif."""
    n = len(cols)
    ci = m.ci
    c1 = cols[ci % n]
    c2 = cols[(ci + 3) % n]
    r = m.r
    ctx.save()
    ctx.translate(m.x, m.y)
    ctx.rotate(_rha(m.angle / (PI / 2)) * (PI / 2))
    ctx.join = _JOIN_MITER
    ctx.set_stroke(dark, 0.9)
    lw0 = max(0.11 * r, 0.7)
    ctx.set_lw(lw0)
    kind = m.kind
    if kind == 0:
        ctx.set_fill(c1)
        ctx.fill_stroke(_stepped_diamond(3, 0.0, 0.0, r, 0.85 * r))
        ctx.set_fill(c2)
        ctx.fill_stroke(_stepped_diamond(2, 0.0, 0.0, 0.45 * r, 0.40 * r))
    elif kind == 1:
        if ci % 2 == 0:
            ctx.rotate(PI / 4)
        pts = _stairify([(-1.15 * r, 0.0), (0.0, -0.42 * r), (1.15 * r, 0.0), (0.0, 0.42 * r)],
                        True, 0.22 * r)
        ctx.set_fill(c1)
        ctx.fill_stroke(_poly(pts, True))
        bar = QPainterPath()
        bar.moveTo(-0.8 * r, 0.0)
        bar.lineTo(0.8 * r, 0.0)
        ctx.stroke_path(bar)
    elif kind == 2:
        if ci & 1:
            ctx.scale(-1, 1)
        pts = [(-r, 0.3 * r), (0.7 * r, 0.3 * r), (0.7 * r, -0.55 * r), (0.1 * r, -0.55 * r)]
        ctx.cap = _CAP_SQUARE
        ctx.set_stroke(dark, 1.0)
        ctx.set_lw(0.48 * r)
        ctx.stroke_path(_poly(pts, close=False))
        ctx.set_stroke(c1, 1.0)
        ctx.set_lw(0.26 * r)
        ctx.stroke_path(_poly(pts, close=False))
    elif kind == 3:
        _star_rosette(ctx, r, c1, c2, dark)
    elif kind == 4:
        ctx.cap = _CAP_SQUARE
        prongs = QPainterPath()
        for x0 in (-0.45 * r, 0.0, 0.45 * r):
            prongs.moveTo(x0, 0.0)
            prongs.lineTo(x0, -0.62 * r)
            prongs.lineTo(x0 + 0.22 * r, -0.62 * r)
            prongs.moveTo(x0, 0.0)
            prongs.lineTo(x0, 0.62 * r)
            prongs.lineTo(x0 + 0.22 * r, 0.62 * r)
        ctx.set_stroke(dark, 1.0)
        ctx.set_lw(0.3 * r)
        ctx.stroke_path(prongs)
        ctx.set_fill(c1)
        ctx.set_lw(lw0)
        ctx.fill_stroke(_rect_path((-0.7 * r, -0.3 * r, 1.4 * r, 0.6 * r)))
        ctx.set_fill(c2)
        ctx.fill_ellipse((-0.14 * r, -0.14 * r, 0.28 * r, 0.28 * r))
    else:
        q = 0.18 * r
        s = 0.36 * r
        ctx.set_fill(c2)
        ctx.fill_stroke(_rect_path((-q, -q, s, s)))
        for cx, cy in ((0.6 * r, 0.0), (-0.6 * r, 0.0), (0.0, 0.6 * r), (0.0, -0.6 * r)):
            ctx.set_fill(c1)
            ctx.fill_stroke(_rect_path((cx - q, cy - q, s, s)))
    ctx.restore()


def _heriz_field(ctx: _Ctx, F, zones, links, motifs, m, X, H, Fc, G, A, C, B, E, D, rD, mw, e, hw, hh):
    """0x100050ba0: the 4 mirrored quarters of the Heriz field."""
    dark, light, gold = X["dark"], X["light"], X["gold"]
    midx, midy = F[0] + F[2] / 2, F[1] + F[3] / 2

    def stroke(path, col, w):
        ctx.set_stroke(col, 1.0)
        ctx.set_lw(w)
        ctx.stroke_path(path)

    for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        ctx.save()
        ctx.translate(midx, midy)
        ctx.scale(sx, sy)
        ctx.clip_rect((-0.5, -0.5, F[2] * 0.5 + 1, F[3] * 0.5 + 1))
        # 1. zone fills, reverse order
        for z in reversed(zones):
            if z[0] is not None:
                ctx.set_fill(z[1])
                ctx.fill_path(z[0])
        # 2. orthogonal L links
        ctx.join = _JOIN_MITER
        for k, (i, j) in enumerate(links):
            p, q = motifs[i], motifs[j]
            corner = (q.x, p.y) if k % 2 == 0 else (p.x, q.y)
            poly = [(p.x, p.y), corner, (q.x, q.y)]
            stroke(_poly(poly, close=False), dark, 0.006 * m)
            stroke(_poly(poly, close=False), zones[p.zone][3], 0.0035 * m)
        # 3. motifs
        for mo in motifs:
            _heriz_motif(ctx, mo, zones[mo.zone][2], dark)
        # 4. outlines
        stroke(H, dark, 0.005 * m)
        stroke(Fc, dark, 0.005 * m)
        stroke(G, dark, 0.004 * m)
        stroke(A, dark, 0.016 * m)
        stroke(A, gold, 0.008 * m)
        stroke(C, dark, 0.005 * m)
        stroke(B, dark, 0.011 * m)
        stroke(B, light, 0.005 * m)
        ctx.set_fill(X["deepBorder"])
        ctx.fill_path(E)
        stroke(E, dark, 0.010 * m)
        stroke(E, light, 0.005 * m)
        # 5. latch-hook finial beyond the pendant
        s = e * 0.5
        ctx.save()
        ctx.translate(mw + 0.38 * e, 0.0)
        ctx.rotate(0.0)
        ctx.join = _JOIN_MITER
        _latch_hook(ctx, s, X["field"], gold, dark)
        ctx.restore()
        # 6. small stepped diamond at the tip
        ctx.set_fill(X["deepBorder"])
        ctx.set_stroke(light, 1.0)
        ctx.set_lw(0.004 * m)
        ctx.fill_stroke(_stepped_diamond(3, mw + 1.12 * e, 0.0, 0.035 * hw, 0.07 * hh))
        # 7. centre star D
        stroke(D, dark, 0.010 * m)
        stroke(D, gold, 0.005 * m)
        ctx.set_fill(light)
        ctx.fill_path(D)
        # 8. centre rosette
        _star_rosette(ctx, rD * 0.55, X["field"], gold, dark)
        ctx.restore()


def draw_heriz(p: QPainter, rect: QRectF, pal: dict, rng) -> None:
    """Heriz style 0x100054a30 (patterns_b §11..§14)."""
    ctx = _Ctx(p)
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    try:
        _heriz(ctx, (rect.x(), rect.y(), rect.width(), rect.height()), _derive(pal), rng)
    finally:
        p.restore()


def _heriz(ctx: _Ctx, rect, X, rng):
    m = rect[3] if rect[3] < rect[2] else rect[2]
    field, light, dark, gold = X["field"], X["light"], X["dark"], X["gold"]
    accent, green, deepBorder = X["accent"], X["green"], X["deepBorder"]

    # 11.3 frame and field
    ctx.set_fill(field)
    ctx.fill_rect(rect)
    R = _inset(rect, 0.012 * m)
    g = 0.026 * m
    _heriz_guard(ctx, R, X, g)
    R = _inset(R, g)
    _heriz_border(ctx, R, X, 0.12 * m)
    R = _inset(R, 0.12 * m)
    _heriz_guard(ctx, R, X, g)
    R = _inset(R, g)
    ctx.ring(R, 0.007 * m, field)
    R = _inset(R, 0.007 * m)
    ctx.ring(R, 0.004 * m, gold)
    F = _inset(R, 0.004 * m)
    ctx.set_fill(field)
    ctx.fill_rect(F)
    hw, hh = F[2] * 0.5, F[3] * 0.5
    st = 0.011 * m
    mw, mh = 0.34 * hw, 0.62 * hh

    # 11.4 quarter shapes
    A = _poly(_stairify(_mirror4([(0.0, mh), (0.36 * mw, 0.64 * mh), (0.64 * mw, 0.64 * mh),
                                  (0.64 * mw, 0.36 * mh), (mw, 0.0)]), True, st), True)
    pts = []
    for i in range(16):
        a = i * PI * 0.125
        r = 0.74 if i % 4 == 0 else (0.44 if i % 2 == 0 else 0.30)
        pts.append((r * (mw * math.cos(a)), r * (mh * math.sin(a))))
    B = _poly(_stairify(pts, True, st), True)
    C = _rect_path((0.42 * mw, 0.42 * mh, 0.17 * mw, 0.17 * mh))
    rD = min(mh, mw) * 0.28
    D = _poly(_stairify([(math.cos(i * PI / 16) * (rD if i % 2 == 0 else 0.72 * rD),
                          math.sin(i * PI / 16) * (rD if i % 2 == 0 else 0.72 * rD))
                         for i in range(32)], True, 0.6 * st), True)
    e = 0.22 * hw
    q = [(0.9 * mw, -0.04 * hh), (mw + 0.4 * e, -0.04 * hh), (mw + 0.4 * e, -0.15 * hh),
         (mw + 0.56 * e, -0.15 * hh), (mw + 0.56 * e, -0.08 * hh), (mw + e, 0.0)]
    E = _poly(_stairify(q + [(x, -y) for (x, y) in reversed(q[:5])], True, st), True)
    a_, b_ = 0.17 * hw, 0.3 * hh
    Fc = _poly([(hw - a_, hh + 1), (hw - a_, hh - 0.55 * b_), (hw - 0.55 * a_, hh - 0.55 * b_),
                (hw - 0.55 * a_, hh - b_), (hw + 1, hh - b_), (hw + 1, hh + 1)], True)
    G = _rect_path((hw - 0.4 * a_, hh - 0.4 * b_, 0.4 * a_ + 1, 0.4 * b_ + 1))
    P0 = (0.16 * hw, hh + 1)
    P1 = (hw + 1, 0.2 * hh)
    ddx, ddy = P1[0] - P0[0], P1[1] - P0[1]
    Ln = math.hypot(ddx, ddy)
    nx, ny = -ddy / Ln, ddx / Ln
    midx, midy = (P0[0] + P1[0]) / 2, (P0[1] + P1[1]) / 2
    if (0.0 - midx) * nx + (0.0 - midy) * ny < 0:
        nx, ny = -nx, -ny
    ox, oy = 0.045 * m * nx, 0.045 * m * ny

    def Q(t):
        return (P0[0] + ddx * t, P0[1] + ddy * t)

    def Qo(t):
        x, y = Q(t)
        return (x + ox, y + oy)

    line = [P0, Q(.3), Qo(.3), Qo(.38), Q(.38), Q(.62), Qo(.62), Qo(.7), Q(.7), P1]
    H = _poly([(hw + 1, hh + 1)] + _stairify(line, False, 2.2 * st), True)

    # 11.5 zones
    L_ = [light, deepBorder, accent, gold, green, light, deepBorder]
    zones = [
        (D, light, [], dark),
        (B, field, [light, deepBorder, gold, green, accent], light),
        (C, accent, [light, field, dark, gold], light),
        (A, deepBorder, [field, light, accent, gold, green, field], field),
        (E, deepBorder, [], dark),
        (G, green, [light, field, gold], light),
        (Fc, field, [light, deepBorder, gold, green], light),
        (H, light, [field, deepBorder, accent, gold, green, field],
         _lerp(accent, deepBorder, 0.4)),
        (None, field, L_, _lerp(light, field, 0.15)),
    ]
    motifs = _place_motifs(_HERIZ_WEIGHTS, zones, hw, hh, 0.008 * m, 0.017 * m, rng)
    links = _make_links(motifs, 2.4)
    ctx.save()
    ctx.clip_rect(F)
    _heriz_field(ctx, F, zones, links, motifs, m, X, H, Fc, G, A, C, B, E, D, rD, mw, e, hw, hh)
    ctx.restore()
