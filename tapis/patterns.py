"""Procedural rug textures (port of the Swift texture builder 0x10005a944).

Public API (used by the app; do not change):

    render(design, size_pts, width, height) -> QImage   (Format_RGBA8888_Premultiplied)
    thumbnail(design, width, height)        -> QImage   (same drawing, 192x120 in the panel)

Both are thread-safe (QImage/QPainter/numpy only) and run on worker threads.

=====================================================================
CONTRACT FOR STYLE FUNCTIONS (incl. patterns_floral.draw_persian/draw_heriz)
=====================================================================

    draw_xxx(p: QPainter, rect: QRectF, pal: dict[str, QColor], rng: Rng) -> None

* Coordinate space ("user space", = the Swift drawing space after the
  dispatcher's setup): origin top-left, y grows DOWN, 1 unit = 1 pixel.
  The space is always LANDSCAPE: L wide (x) by S tall (y), L >= S.
  For a tall bitmap (width < height) the dispatcher has already applied
  translate(W, 0); rotate(90 deg), i.e. user (x, y) -> pixel (W - y, x);
  style code never needs to know.
* `rect` = QRectF(f, 0, L - 2f, S): exactly the rect the Swift dispatcher
  hands to the style function, i.e. the rug body with the fringe excluded
  (f = 0.07 * S when design.fringe else 0).  rect.height() == S and
  `m = min(rect.width(), rect.height())` is the specs' S / m.
* The painter is already clipped to `rect`, has Antialiasing and
  SmoothPixmapTransform on, and its state is otherwise default.  CG's global
  round line cap / round line join are NOT painter state in Qt: style code
  must build its pens with Qt.PenCapStyle.RoundCap / Qt.PenJoinStyle.RoundJoin
  (use `pen()` below).  QPainterPath default fill rule is OddEven: set
  WindingFill for CG non-zero fills (use `wpath()` below).
* `pal` maps the 5 slots 'field','border','accent','light','dark' to opaque
  QColor (from the design's 0..1 sRGB floats).  Derived colours (gold, green,
  paleField, deepBorder) are computed by the style itself.
* `rng` is the shared generator, already advanced past the fringe strands;
  the style must consume it exactly as the spec says, because the finishing
  pass (wear spots) draws from it afterwards.
* The style function must leave the painter's save/restore stack balanced.
"""

from __future__ import annotations

import math
import threading
import traceback

import numpy as np
from PyQt6.QtCore import QLineF, QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QImage, QImageReader, QPainter, QPainterPath, QPen,
                         QRadialGradient)

from .patterns_rng import Rng

PI = math.pi
TWO_PI = 6.283185307179586
SLOTS = ("field", "border", "accent", "light", "dark")

_ROUND = Qt.PenCapStyle.RoundCap
_RJOIN = Qt.PenJoinStyle.RoundJoin


# ----------------------------------------------------------------- helpers
def qcolor(rgb, a: float = 1.0) -> QColor:
    r, g, b = (min(max(float(v), 0.0), 1.0) for v in rgb)
    return QColor.fromRgbF(r, g, b, min(max(a, 0.0), 1.0))


def with_alpha(c: QColor, a: float) -> QColor:
    c = QColor(c)
    c.setAlphaF(min(max(a, 0.0), 1.0))
    return c


def pen(color: QColor, width: float, cap=_ROUND, join=_RJOIN) -> QPen:
    p = QPen(QBrush(color), width, Qt.PenStyle.SolidLine, cap, join)
    return p


def wpath() -> QPainterPath:
    """Empty path with CG's non-zero (winding) fill rule."""
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.WindingFill)
    return path


def inset(r: QRectF, dx: float, dy: float | None = None) -> QRectF:
    if dy is None:
        dy = dx
    return QRectF(r.x() + dx, r.y() + dy, r.width() - 2 * dx, r.height() - 2 * dy)


def mirror4(q):
    return (list(q) + [(x, -y) for (x, y) in reversed(q)] + [(-x, -y) for (x, y) in q]
            + [(-x, y) for (x, y) in reversed(q)])


def stepped_diamond(n: int, cx: float, cy: float, hw: float, hh: float) -> QPainterPath:
    """0x10004dd80: closed stepped diamond (winding path)."""
    q = []
    for i in range(n):
        y = (1.0 - i / n) * (-hh)
        q.append(((hw * i) / n, y))
        q.append(((hw * (i + 1)) / n, y))
    q.append((hw, 0.0))
    pts = mirror4(q)
    path = wpath()
    path.moveTo(pts[0][0] + cx, pts[0][1] + cy)
    for x, y in pts[1:]:
        path.lineTo(x + cx, y + cy)
    path.closeSubpath()
    return path


def _ring(r: QRectF, t: float) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    path.addRect(r)
    path.addRect(inset(r, t))
    return path


def _stroke_rect(p: QPainter, r: QRectF, color: QColor, width: float):
    path = QPainterPath()
    path.addRect(r)
    p.strokePath(path, pen(color, width))


# ------------------------------------------------------------------ fringe
def _draw_fringe(p: QPainter, pal, L: float, S: float, f: float, rng: Rng):
    light = pal["light_rgb"]
    dark = pal["dark_rgb"]
    lw = max(S * 0.0035, 1.6)
    band = f * 0.16
    for d in (-1.0, 1.0):
        x0 = f if d < 0 else L - f
        y = S * 0.012
        while y < S * 0.988:
            r1 = rng.rand(); r2 = rng.rand(); r3 = rng.rand()
            length = f * (0.7 + 0.26 * r1)
            dy = (r2 * 0.24 - 0.12) * length
            shade = 0.78 + 0.22 * r3
            col = qcolor((min(light[0] * shade, 1.0), min(light[1] * shade, 1.0),
                          min(light[2] * shade, 1.0)))
            path = QPainterPath()
            path.moveTo(x0, y)
            path.quadTo(x0 + d * length * 0.5, y + dy * 0.2, x0 + d * length, y + dy)
            p.strokePath(path, pen(col, lw))
            y += lw * 1.35
        bx = x0 - band if d < 0 else x0
        p.fillRect(QRectF(bx, 0, band, S), qcolor([min(c * 0.85, 1.0) for c in light]))
        path = QPainterPath()
        if S > 0:
            yy = 0.0
            while True:
                path.moveTo(bx, yy)
                path.lineTo(bx + band, yy + 3 * lw)
                yy += 4 * lw
                if not yy < S:
                    break
        p.strokePath(path, pen(qcolor(dark, 0.25), lw * 0.6))


# ------------------------------------------------------------------ styles
def draw_stripes(p: QPainter, rect: QRectF, pal, rng: Rng):
    minX, minY, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    maxX, midX = minX + w, minX + w * 0.5
    m = min(w, h)
    p.fillRect(rect, pal["field"])
    cols = [pal["field"], pal["light"], pal["border"], pal["accent"], pal["dark"]]
    stripes = []
    x = minX
    while x < midX:
        sw = 0.02 * m + (0.14 * m - 0.02 * m) * rng.rand()
        if midX - x < sw:
            sw = midX - x
        c = cols[rng.next_u64() % 5]
        stripes.append((x, sw, c))
        x += sw
    for x, sw, c in stripes:
        p.fillRect(QRectF(x, minY, sw, h), c)
        p.fillRect(QRectF(maxX - (x - minX) - sw, minY, sw, h), c)
        if rng.rand() < 0.3:
            cx = x + sw * 0.5
            p.fillRect(QRectF(cx - 0.004 * m, minY, 0.008 * m, h), pal["light"])
            p.fillRect(QRectF(maxX - (cx - minX) - 0.004 * m, minY, 0.008 * m, h), pal["light"])


def draw_bordered(p: QPainter, rect: QRectF, pal, rng: Rng):
    minX, minY, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    maxX, maxY = minX + w, minY + h
    m = min(w, h)
    b = 0.09 * m
    p.fillRect(rect, pal["border"])
    inner = inset(rect, b)
    p.fillRect(inner, pal["field"])
    _stroke_rect(p, inset(rect, 0.045 * m), with_alpha(pal["light"], 0.9), 0.008 * m)
    _stroke_rect(p, inset(inner, 0.03 * m), with_alpha(pal["light"], 0.6), 0.004 * m)
    for cx, cy in ((minX, minY), (maxX, minY), (minX, maxY), (maxX, maxY)):
        x = minX if cx == minX else maxX - b
        y = minY if cy == minY else maxY - b
        p.fillRect(inset(QRectF(x, y, b, b), 0.2 * b), pal["accent"])
    path = stepped_diamond(5, rect.center().x(), rect.center().y(),
                           inner.width() * 0.18, inner.height() * 0.3)
    p.strokePath(path, pen(with_alpha(pal["light"], 0.5), 0.006 * m))


def draw_berber(p: QPainter, rect: QRectF, pal, rng: Rng):
    minX, minY, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    maxX, maxY = minX + w, minY + h
    m = min(w, h)
    lr, lg, lb = pal["light"].redF(), pal["light"].greenF(), pal["light"].blueF()
    p.fillRect(rect, pal["light"])

    def tufts(n):
        width = 0.006 * m
        for _ in range(n):
            x = minX + (maxX - minX) * rng.rand()
            y = minY + (maxY - minY) * rng.rand()
            ang = rng.rand() * TWO_PI + 0.0
            ln = m * (0.005 + 0.009 * rng.rand())
            sh = 0.82 + 0.26 * rng.rand()
            p.setPen(pen(qcolor((min(lr * sh, 1.0), min(lg * sh, 1.0), min(lb * sh, 1.0)), 0.22), width))
            p.drawLine(QLineF(x, y, x + math.cos(ang) * ln, y + math.sin(ang) * ln))
        p.setPen(Qt.PenStyle.NoPen)

    tufts(int(w * h / (m * m) * 2200))

    cell = h / 2.5
    dx = 0.9 * cell
    reach = dx * (h / cell)
    dpen = pen(with_alpha(pal["dark"], 0.85), 0.022 * m)
    x = minX - reach
    while x < maxX + reach:
        for sign in (1.0, -1.0):
            path = QPainterPath()
            jit = 0.0
            for i in range(41):
                y = minY + (i / 40.0) * h
                jit = jit * 0.7 + (2 * rng.rand() - 1) * m * 0.006
                px = x + 2 * ((0.5 * dx) * (sign * (y - minY) / cell)) + jit
                if i == 0:
                    path.moveTo(px, y)
                else:
                    path.lineTo(px, y)
            p.strokePath(path, dpen)
        x += dx

    cpen = pen(with_alpha(pal["dark"], 0.85), 0.012 * m)
    n = int(w / dx * 1.5)
    s = 0.025 * m
    for _ in range(n):
        x = minX + (maxX - minX) * rng.rand()
        y = minY + (maxY - minY) * rng.rand()
        path = QPainterPath()
        path.moveTo(x - s, y - s); path.lineTo(x + s, y + s)
        path.moveTo(x + s, y - s); path.lineTo(x - s, y + s)
        p.strokePath(path, cpen)

    tufts(int(w * h / (m * m) * 1500))


def _radial(p: QPainter, cx, cy, r, color: QColor, a: float):
    if r <= 0:
        return
    g = QRadialGradient(QPointF(cx, cy), r)
    g.setColorAt(0.0, with_alpha(color, a))
    g.setColorAt(1.0, with_alpha(color, 0.0))
    path = QPainterPath()
    path.addEllipse(QPointF(cx, cy), r, r)
    p.fillPath(path, QBrush(g))


def draw_shag(p: QPainter, rect: QRectF, pal, rng: Rng):
    minX, minY, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    maxX, maxY = minX + w, minY + h
    m = min(w, h)
    p.fillRect(rect, pal["field"])
    for _ in range(40):
        cx = minX + (maxX - minX) * rng.rand()
        cy = minY + (maxY - minY) * rng.rand()
        r = m * (0.08 + 0.22 * rng.rand())
        c = pal["light"] if rng.rand() < 0.5 else pal["border"]
        a = 0.15 + 0.2 * rng.rand()
        _radial(p, cx, cy, r, c, a)
    n = int(w * h / (m * m) * 1400)
    half_pi = PI / 2
    for _ in range(n):
        x = minX + (maxX - minX) * rng.rand()
        y = minY + (maxY - minY) * rng.rand()
        ang = rng.rand() * TWO_PI + 0.0
        ln = m * (0.015 + 0.025 * rng.rand())
        bend = -0.8 + 1.6 * rng.rand()
        t = rng.rand()
        c = pal["light"] if t < 0.4 else (pal["accent"] if t < 0.75 else pal["border"])
        a = 0.06 + 0.1 * rng.rand()
        lw = m * (0.008 + 0.008 * rng.rand())
        ex, ey = x + math.cos(ang) * ln, y + math.sin(ang) * ln
        mx, my = (x + ex) * 0.5, (y + ey) * 0.5
        path = QPainterPath()
        path.moveTo(x, y)
        path.quadTo(mx + bend * (ln * math.cos(ang + half_pi)), my + bend * (ln * math.sin(ang + half_pi)), ex, ey)
        p.strokePath(path, pen(with_alpha(c, a), lw))


def draw_kilim(p: QPainter, rect: QRectF, pal, rng: Rng):
    minX, minY, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    m = min(w, h)
    p.fillRect(rect, pal["field"])
    b = 0.075 * m
    p.fillPath(_ring(rect, b), pal["dark"])
    I1 = inset(rect, b)
    x0_, y0_, x1_, y1_ = I1.left(), I1.top(), I1.right(), I1.bottom()
    segs = [((x0_, y0_), (x1_, y0_)), ((x1_, y0_), (x1_, y1_)),
            ((x1_, y1_), (x0_, y1_)), ((x0_, y1_), (x0_, y0_))]
    teeth = wpath()
    for (x0, y0), (x1, y1) in segs:
        dx, dy = x1 - x0, y1 - y0
        L = math.hypot(dx, dy)
        if L <= 0:
            continue
        n = max(int(L / (0.9 * b)), 2)
        ox, oy = 0.7 * b * dx / L, 0.7 * b * (-dy / L)
        for k in range(n):
            t0, t1 = k / n, (k + 1) / n
            tm = (t0 + t1) * 0.5
            teeth.moveTo(x0 + dx * t0, y0 + dy * t0)
            teeth.lineTo(x0 + dx * t1, y0 + dy * t1)
            teeth.lineTo(x0 + dx * tm - oy, y0 + dy * tm - ox)
            teeth.closeSubpath()
    p.fillPath(teeth, pal["light"])

    I2 = inset(rect, 1.2 * b)
    K = [pal["border"], pal["light"], pal["accent"], pal["field"], pal["dark"]]
    mh = I2.height() * 0.62
    i2minX, i2minY, i2h = I2.left(), I2.top(), I2.height()
    i2maxX, i2midX, i2midY = I2.right(), I2.center().x(), I2.center().y()
    stop = i2midX - mh * 0.5
    gap = 0.03 * m
    bands = []
    x = i2minX
    k = 0
    while x < stop - gap:
        bw = 0.05 * m + (0.16 * m - 0.05 * m) * rng.rand()
        if stop - x < bw:
            bw = stop - x
        kind = rng.next_u64() & 3
        bands.append((x, bw, k % 5, kind))
        k += (rng.next_u64() & 1) + 1
        x += bw
    p.save()
    p.setClipRect(I2, Qt.ClipOperation.IntersectClip)
    for x, bw, ci, kind in bands:
        bg = K[ci]
        fg = K[(ci + 2) % 5]
        for mirrored in (False, True):
            bx = x if not mirrored else i2maxX - (x - i2minX) - bw
            B = QRectF(bx, i2minY, bw, i2h)
            p.fillRect(B, bg)
            bminX, bminY, bmaxX, bh = B.left(), B.top(), B.right(), B.height()
            bmidX = B.center().x()
            if kind == 0:
                n = max(int(bh / (bw * 1.1)), 1)
                ch = bh / n
                for j in range(n):
                    cy = bminY + ch * (j + 0.5)
                    p.fillPath(stepped_diamond(3, bmidX, cy, 0.38 * bw, 0.42 * ch), fg)
                    p.fillPath(stepped_diamond(2, bmidX, cy, 0.14 * bw, 0.16 * ch), pal["dark"])
            elif kind == 1:
                n = max(int(bh / (bw * 0.6)), 2)
                ins = 0.2 * bw
                path = QPainterPath()
                path.moveTo(bminX + ins, bminY + bh * 0 / n)
                for j in range(1, n + 1):
                    px = (bmaxX - ins) if (j & 1) else (bminX + ins)
                    path.lineTo(px, bminY + bh * j / n)
                zp = pen(fg, 0.18 * bw, Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.MiterJoin)
                zp.setMiterLimit(5.0)   # CG miter limit 10 (ratio to full width) == Qt 5
                p.strokePath(path, zp)
            elif kind == 2:
                for fr, c in ((0.25, fg), (0.5, pal["dark"]), (0.75, fg)):
                    p.fillRect(QRectF(bminX + fr * bw - 0.04 * bw, bminY, 0.08 * bw, bh), c)
            else:
                n = max(int(bh / (bw * 0.7)), 2)
                ch = bh / n
                path = wpath()
                for j in range(n):
                    y0 = bminY + ch * j
                    path.moveTo(bminX + 0.15 * bw, y0 + 0.15 * ch)
                    path.lineTo(bmaxX - 0.15 * bw, y0 + 0.5 * ch)
                    path.lineTo(bminX + 0.15 * bw, y0 + 0.85 * ch)
                    path.closeSubpath()
                p.fillPath(path, fg)
    M = QRectF(i2midX - mh / 2, i2minY, mh, i2h)
    p.fillRect(M, pal["field"])
    ch = i2h / 3
    g = 0.03 * m
    light = pal["light"]
    for j in range(3):
        cx = M.center().x()
        cy = i2minY + ch * (j + 0.5)
        p.fillPath(stepped_diamond(6, cx, cy, 0.46 * mh, 0.50 * ch), pal["dark"])
        p.fillPath(stepped_diamond(6, cx, cy, 0.42 * mh, 0.45 * ch), pal["border"])
        p.fillPath(stepped_diamond(4, cx, cy, 0.28 * mh, 0.30 * ch), light)
        p.fillPath(stepped_diamond(3, cx, cy, 0.14 * mh, 0.15 * ch), pal["accent"])
        xl = cx - 0.44 * mh
        p.fillRect(QRectF(xl - 0.15 * g, cy - g, 0.3 * g, 2 * g), light)
        p.fillRect(QRectF(xl - 0.8 * g, cy - g, 0.8 * g, 0.3 * g), light)
        p.fillRect(QRectF(xl, cy + 0.7 * g, 0.8 * g, 0.3 * g), light)
        xr = cx + 0.44 * mh
        p.fillRect(QRectF(xr - 0.15 * g, cy - g, 0.3 * g, 2 * g), light)
        p.fillRect(QRectF(xr, cy - g, 0.8 * g, 0.3 * g), light)
        p.fillRect(QRectF(xr - 0.8 * g, cy + 0.7 * g, 0.8 * g, 0.3 * g), light)
    p.restore()


def _load_image(path) -> QImage | None:
    if not path:
        return None
    try:
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        img = reader.read()
    except Exception:
        return None
    if img is None or img.isNull() or img.width() < 1 or img.height() < 1:
        return None
    return img


def draw_image(p: QPainter, rect: QRectF, path) -> bool:
    """0x100059e08: aspect-fill photo, centred, upright in user space."""
    img = _load_image(path)
    if img is None:
        return False
    iw, ih = img.width(), img.height()
    s = max(rect.width() / iw, rect.height() / ih)
    dw, dh = iw * s, ih * s
    dx = rect.center().x() - dw / 2
    dy = rect.center().y() - dh / 2
    tw, th = max(1, int(round(dw))), max(1, int(round(dh)))
    if s < 1.0:   # good-quality downscale first (QPainter alone would alias)
        img = img.scaled(tw, th, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    p.save()
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    p.drawImage(QRectF(dx, dy, dw, dh), img, QRectF(0, 0, img.width(), img.height()))
    p.restore()
    return True


def _field_fill(p, rect, pal, rng):
    p.fillRect(rect, pal["field"])


_floral_warned = set()


def _floral(name):
    try:
        from . import patterns_floral
        return getattr(patterns_floral, name)
    except Exception:
        if name not in _floral_warned:
            _floral_warned.add(name)
            traceback.print_exc()
        return None


# ------------------------------------------------------------ noise images
_noise_lock = threading.Lock()
_noise_cache: dict = {}


def _noise(w: int, h: int, seed: int) -> np.ndarray:
    """0x10005a08c: h x w float32 grey values in 0..1 (row 0 = first data row)."""
    key = (w, h, seed)
    with _noise_lock:
        arr = _noise_cache.get(key)
        if arr is None:
            rng = Rng(seed)
            px = np.empty(w * h, np.uint8)
            rand = rng.rand
            for i in range(w * h):
                v = (rand() + rand() - 1.0) * 160.0 + 128.0
                px[i] = 0 if v < 0 else (255 if v >= 255 else int(v))
            arr = (px.reshape(h, w).astype(np.float32) / 255.0)
            _noise_cache[key] = arr
    return arr


def _resample_weights(n_out: int, start: float, length: float, n_in: int, origin: int = 0) -> np.ndarray:
    """(n_out x n_in) bilinear resampling matrix (edge clamped): output pixel i
    (centre origin+i+0.5 in user space) samples an n_in-pixel image stretched
    over [start, start+length].  Bilinear matched the reference renders better
    than a cubic kernel."""
    centers = (np.arange(n_out, dtype=np.float64) + origin + 0.5 - start) / length * n_in - 0.5
    base = np.floor(centers)
    frac = centers - base
    Wm = np.zeros((n_out, n_in), np.float64)
    rows = np.arange(n_out)
    for k, wk in ((0, 1.0 - frac), (1, frac)):
        idx = np.clip(base + k, 0, n_in - 1).astype(np.int64)
        np.add.at(Wm, (rows, idx), wk)
    return Wm


def _soft_light(C, Cs, a):
    """In place: C += a * (SoftLight(C, Cs) - C).  C (h,w,3), Cs (h,w), a (w,) or scalar."""
    k = (2.0 * Cs - 1.0)[..., None]
    D = np.where(C <= 0.25, ((16.0 * C - 12.0) * C + 4.0) * C, np.sqrt(C))
    delta = np.where(k <= 0, C * (1.0 - C), D - C)
    delta *= k
    delta *= a
    C += delta


def _overlay(C, Cs, a):
    Cs3 = Cs[..., None]
    B = np.where(C <= 0.5, 2.0 * Cs3 * C, 1.0 - 2.0 * (1.0 - Cs3) * (1.0 - C))
    B -= C
    B *= a
    C += B


def _finish_pixels(U: np.ndarray, rx: float, rw: float, S: int, design, light_rgb, texture: bool, rng: Rng,
                   bgr: bool):
    """Numpy part of 0x10005a2a8 on user-space pixel array U (S x L x 4 uint8, premultiplied).

    Grain (soft-light A, B; overlay C tiled), wear spots, saturation fade.
    Edge line is drawn afterwards with QPainter."""
    L = U.shape[1]
    m = min(rw, float(S))
    i0 = max(int(math.floor(rx)), 0)
    i1 = min(int(math.ceil(rx + rw)), L)
    if i1 <= i0 or S < 1:
        # still consume the wear RNG draws for consistency
        _consume_wear(design, rng)
        return
    cols = np.arange(i0, i1, dtype=np.float64)
    cov = np.clip(np.minimum(cols + 1, rx + rw) - np.maximum(cols, rx), 0.0, 1.0).astype(np.float32)
    covc = cov[None, :, None]

    sub = U[:, i0:i1]
    order = [2, 1, 0] if bgr else [0, 1, 2]
    A = sub[..., 3].astype(np.float32) * (1.0 / 255.0)
    C = sub[..., order].astype(np.float32) * (1.0 / 255.0)
    inv = np.where(A > 0, 1.0 / np.maximum(A, 1e-6), 0.0).astype(np.float32)
    C *= inv[..., None]
    np.clip(C, 0.0, 1.0, out=C)

    nw = i1 - i0
    if texture:
        # A: 48x2 stretched over rect (image row 0 lands at user maxY -> flip)
        a_alpha = 0.0 if design.style == "shag" else 0.25
        if a_alpha > 0:
            img = _noise(48, 2, 3)[::-1]
            Wx = _resample_weights(nw, rx, rw, 48, origin=i0)
            Wy = _resample_weights(S, 0.0, float(S), 2)
            Cs = (Wy @ img.astype(np.float64) @ Wx.T).astype(np.float32)
            np.clip(Cs, 0, 1, out=Cs)
            _soft_light(C, Cs, covc * a_alpha)
        img = _noise(24, 24, 2)[::-1]
        Wx = _resample_weights(nw, rx, rw, 24, origin=i0)
        Wy = _resample_weights(S, 0.0, float(S), 24)
        Cs = (Wy @ img.astype(np.float64) @ Wx.T).astype(np.float32)
        np.clip(Cs, 0, 1, out=Cs)
        _soft_light(C, Cs, covc * 0.25)
        # C: 128x128 tiled, tile t x t from (rect.minX, rect.minY), nearest, mirrored per tile
        t = max(m * 0.09, 32.0)
        img = _noise(128, 128, 1)
        fx = np.mod((cols + 0.5 - rx) / t, 1.0)
        ix = np.minimum((fx * 128).astype(np.int64), 127)
        fy = np.mod((np.arange(S, dtype=np.float64) + 0.5) / t, 1.0)
        iy = 127 - np.minimum((fy * 128).astype(np.int64), 127)
        Cs = img[iy[:, None], ix[None, :]]
        _overlay(C, Cs, covc * 0.32)

    w = float(design.wear)
    if w > 0.01:
        n = int(w * 16.0 + 5.0)
        lc = np.array(light_rgb, np.float32)
        a0 = 0.28 * w
        for _ in range(n):
            cx = rx + rw * rng.rand()
            cy = 0.0 + float(S) * rng.rand()
            r = m * (0.06 + 0.24 * rng.rand())
            if r <= 0:
                continue
            xa = max(int(math.floor(cx - r)), i0)
            xb = min(int(math.ceil(cx + r)) + 1, i1)
            ya = max(int(math.floor(cy - r)), 0)
            yb = min(int(math.ceil(cy + r)) + 1, S)
            if xa >= xb or ya >= yb:
                continue
            xs = np.arange(xa, xb, dtype=np.float32) + 0.5
            ys = np.arange(ya, yb, dtype=np.float32) + 0.5
            d = np.sqrt((xs[None, :] - cx) ** 2 + (ys[:, None] - cy) ** 2) / r
            al = np.where(d < 1.0, a0 * (1.0 - d), 0.0).astype(np.float32)
            al *= cov[xa - i0:xb - i0][None, :]
            blk = C[ya:yb, xa - i0:xb - i0]
            blk += (lc[None, None, :] - blk) * al[..., None]
        # saturation fade with 50% grey: C += a * (Lum - C)
        fa = 0.35 * w
        lum = C[..., 0] * 0.3 + C[..., 1] * 0.59 + C[..., 2] * 0.11
        C += (lum[..., None] - C) * (covc * fa)

    np.clip(C, 0.0, 1.0, out=C)
    C *= A[..., None]
    out = (C * 255.0 + 0.5).astype(np.uint8)
    sub[..., order] = out


def _consume_wear(design, rng):
    w = float(design.wear)
    if w > 0.01:
        for _ in range(int(w * 16.0 + 5.0) * 3):
            rng.rand()


def _buffer(img: QImage) -> np.ndarray:
    ptr = img.bits()
    ptr.setsize(img.sizeInBytes())
    arr = np.frombuffer(ptr, np.uint8).reshape(img.height(), img.bytesPerLine())
    return arr[:, :img.width() * 4].reshape(img.height(), img.width(), 4)


# --------------------------------------------------------------- dispatcher
_STYLES = {
    "kilim": draw_kilim,
    "beniOurain": draw_berber,
    "shag": draw_shag,
    "stripes": draw_stripes,
    "bordered": draw_bordered,
}


def _begin(img: QImage, W: int, H: int) -> QPainter:
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    if W < H:
        p.translate(W, 0)
        p.rotate(90.0)
    return p


def _draw_style(p: QPainter, img: QImage, W, H, rect: QRectF, design, pal, rng: Rng) -> QPainter:
    style = design.style
    fn = None
    if style == "image":
        if draw_image(p, rect, design.image_path):
            return p
        style = "persian"
    if style in ("persian", "heriz"):
        fn = _floral("draw_persian" if style == "persian" else "draw_heriz")
        if fn is None:
            fn = _field_fill
    else:
        fn = _STYLES.get(style, _field_fill)
        if style not in _STYLES:
            fl = _floral("draw_persian")
            fn = fl or _field_fill
    p.save()
    try:
        fn(p, rect, pal, rng)
        p.restore()
    except Exception:
        traceback.print_exc()
        p.end()           # discard any unbalanced painter state
        p = _begin(img, W, H)
        p.setClipRect(rect)
        p.fillRect(rect, pal["field"])
    return p


def _render(design, W: int, H: int, texture: bool = True) -> QImage:
    W, H = int(W), int(H)
    if W < 1 or H < 1:
        return QImage()
    img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    if W < H:
        S, L = W, H
    else:
        S, L = H, W
    pal = {}
    for k in SLOTS:
        rgb = tuple(float(v) for v in design.palette.get(k, (0.5, 0.5, 0.5)))
        pal[k] = qcolor(rgb)
        pal[k + "_rgb"] = rgb
    style_pal = {k: pal[k] for k in SLOTS}

    rng = Rng(int(design.seed))
    f = 0.07 * min(S, L) if design.fringe else 0.0
    p = _begin(img, W, H)
    if f > 0:
        _draw_fringe(p, pal, float(L), float(S), f, rng)
    rect = QRectF(f, 0.0, L - 2 * f, float(S))
    p.setClipRect(rect)
    p = _draw_style(p, img, W, H, rect, design, style_pal, rng)
    p.end()

    # finishing: per-pixel part in numpy (user space view of the bitmap)
    P = _buffer(img)
    U = np.rot90(P) if W < H else P
    _finish_pixels(U, f, L - 2 * f, S, design, pal["light_rgb"], texture, rng, bgr=True)

    # edge line (clip to rect still active in Swift)
    p = _begin(img, W, H)
    p.setClipRect(rect)
    m = min(rect.width(), rect.height())
    _stroke_rect(p, inset(rect, -0.001 * m, 0.002 * m), qcolor(pal["dark_rgb"], 0.35), max(m * 0.006, 2.0))
    p.end()
    return img.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)


def render(design, size_pts, width: int, height: int) -> QImage:
    """Rug texture, width x height pixels, RGBA8888 premultiplied.

    Pixel (0,0) is the rug's top-left; transparent outside the rug (fringe gaps).
    `size_pts` is not used by the original texture code (only the pixel size)."""
    return _render(design, width, height, True)


def thumbnail(design, width: int, height: int) -> QImage:
    """Design-panel preview: the same render at the panel's pixel size
    (original: render(d, 192, 120, texture=1)).  For the Image style without a
    loadable picture a null QImage is returned (the panel shows a blank cell)."""
    if design.style == "image" and _load_image(design.image_path) is None:
        return QImage()
    return _render(design, width, height, True)
