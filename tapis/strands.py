"""The shag strand texture (256×256 RGBA), built exactly like the Mac app:
R = strand length (clumped), G = distance from the strand's centre (0 centre
... 1 outside), B = per-strand random, A = tuft direction (smooth field)."""

import numpy as np

_G = 0x9E3779B97F4A7C15
_M = (1 << 64) - 1


def _splitmix(seed: int):
    k = 1
    while True:
        z = (seed + (k + 1) * _G) & _M
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _M
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _M
        z ^= z >> 31
        yield np.float32((z >> 11) * 2.0 ** -53)
        k += 1


def _vnoise(N, x, y):
    ix, iy = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
    fx, fy = x - ix, y - iy
    sx, sy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
    at = lambda i, j: N[(j & 7) * 8 + (i & 7)]
    a = at(ix, iy) + (at(ix + 1, iy) - at(ix, iy)) * sx
    b = at(ix, iy + 1) + (at(ix + 1, iy + 1) - at(ix, iy + 1)) * sx
    return a + (b - a) * sy


def make_strands() -> bytes:
    rng = _splitmix(42)
    nxt = lambda: float(next(rng))
    noise_a = np.array([nxt() for _ in range(64)], np.float32)
    noise_b = np.array([nxt() for _ in range(64)], np.float32)
    pts = np.zeros((4096, 2), np.float32)
    length = np.zeros(4096, np.float32)
    radius = np.zeros(4096, np.float32)
    rnd = np.zeros(4096, np.float32)
    for y in range(64):
        for x in range(64):
            c = y * 64 + x
            px = 4 * x + (nxt() * 3 + 0.5)
            py = 4 * y + (nxt() * 3 + 0.5)
            pts[c] = (px, py)
            v = float(_vnoise(noise_a, np.array(px / 256 * 8), np.array(py / 256 * 8)))
            length[c] = min(v * 0.4 + 0.45 + nxt() * 0.25, 1.0)
            radius[c] = nxt() + 2.0
            rnd[c] = nxt()
    yy, xx = np.mgrid[0:256, 0:256]
    p = np.stack([xx + 0.5, yy + 0.5], -1).astype(np.float32)
    cx, cy = xx >> 2, yy >> 2
    best = np.full((256, 256), 99.0, np.float32)
    bi = np.full((256, 256), -1, np.int64)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            jx, jy = cx + dx, cy + dy
            cell = (jy & 63) * 64 + (jx & 63)
            off = np.stack([(jx - (jx & 63)) * 4, (jy - (jy & 63)) * 4], -1)
            d = np.linalg.norm(p - (pts[cell] + off), axis=-1) / radius[cell]
            better = d < best
            best = np.where(better, d, best)
            bi = np.where(better, cell, bi)
    inside = best < 1.0
    R = np.where(inside, (length[bi] * 255).astype(np.int64), 0)
    G = np.where(inside, (best * 255).astype(np.int64), 255)
    B = (rnd[bi] * 255).astype(np.int64)
    A = (_vnoise(noise_b, xx / 256 * 8, yy / 256 * 8) * 255).astype(np.int64)
    return np.stack([R, G, B, A], -1).clip(0, 255).astype(np.uint8).tobytes()
