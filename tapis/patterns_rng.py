"""SplitMix64 random generator, bit-exact with the original Swift code.

Spec: patterns_a.md §0 / patterns_b.md §0.1.

    G  = 0x9E3779B97F4A7C15
    state0 = seed &+ G           (NOTE: extra +G at construction)
    next:   state &+= G; z = state
            z = (z ^ (z >> 30)) &* 0xBF58476D1CE4E5B9
            z = (z ^ (z >> 27)) &* 0x94D049BB133111EB
            return z ^ (z >> 31)

So the very first output of ``Rng(seed)`` is mix(seed + 2G).

One instance is shared (in this order) by: fringe strands -> style function
-> finishing pass (wear spots).  Order of calls matters.
"""

from __future__ import annotations

import math

G = 0x9E3779B97F4A7C15
M1 = 0xBF58476D1CE4E5B9
M2 = 0x94D049BB133111EB
MASK = (1 << 64) - 1
_INV53 = 2.0 ** -53
TWO_PI = 6.283185307179586


class Rng:
    """SplitMix64 as used by Tapis.

    Draw helpers (each documents how many raw 64-bit values it consumes):

    * ``next_u64()``      -> int in [0, 2**64)          (1 draw)
    * ``next_double()``   -> float in [0, 1)            (1 draw) = (u64 >> 11) * 2**-53
      (alias ``rnd()`` / ``rand()`` — the names used in the specs)
    * ``uniform(a, b)``   -> a + (b - a) * next_double() (1 draw) — the exact
      Swift form ``a + (b-a)*r`` used for ranged doubles in the specs
    * ``angle()``         -> next_double() * 2*pi + 0.0 (1 draw)
    * ``below(n)``        -> next_u64() % n             (1 draw) — used e.g. for
      ``u64() % 5`` colour picks and ``u64() % count`` colour indices
    """

    __slots__ = ("state",)

    def __init__(self, seed: int):
        # seed is a Swift Int (Int64); use its two's-complement bit pattern.
        self.state = (int(seed) + G) & MASK

    def next_u64(self) -> int:
        self.state = s = (self.state + G) & MASK
        z = ((s ^ (s >> 30)) * M1) & MASK
        z = ((z ^ (z >> 27)) * M2) & MASK
        return z ^ (z >> 31)

    def next_double(self) -> float:
        """Double in [0, 1): ``Double(u64 >> 11) * 2^-53`` (ucvtf #53)."""
        return (self.next_u64() >> 11) * _INV53

    # spec names
    rnd = next_double
    rand = next_double

    def uniform(self, a: float, b: float) -> float:
        """``a + (b - a) * next_double()`` (one draw)."""
        return a + (b - a) * self.next_double()

    def angle(self) -> float:
        """``next_double() * 2*pi + 0.0`` (one draw)."""
        return self.next_double() * TWO_PI + 0.0

    def below(self, n: int) -> int:
        """``next_u64() % n`` (one draw, unsigned modulo, n >= 1)."""
        return self.next_u64() % n


def round_half_away(x: float) -> float:
    """Swift ``.rounded()`` (half away from zero), returned as float."""
    return math.copysign(math.floor(abs(x) + 0.5), x)
