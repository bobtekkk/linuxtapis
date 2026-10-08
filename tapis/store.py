"""Saved state: arrangements of rugs and a few preferences, in the same JSON
shape the Mac app keeps in its user defaults. Lives in ~/.config/tapis/."""

import base64
import json
import os
import random
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

STYLES = ["persian", "heriz", "kilim", "beniOurain", "shag", "stripes", "bordered", "image"]
SLOTS = ["field", "border", "accent", "light", "dark"]

DEFAULT_PALETTE = {
    "field": (156 / 255, 46 / 255, 31 / 255),
    "border": (28 / 255, 36 / 255, 67 / 255),
    "accent": (111 / 255, 134 / 255, 166 / 255),
    "light": (234 / 255, 220 / 255, 194 / 255),
    "dark": (20 / 255, 22 / 255, 38 / 255),
}


def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / "tapis"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class RugDesign:
    style: str = "persian"
    palette: dict = field(default_factory=lambda: dict(DEFAULT_PALETTE))
    fringe: bool = False
    wear: float = 0.2
    seed: int = 7
    image_path: str | None = None
    pile: float = 0.0

    def to_json(self):
        d = {"style": self.style,
             "palette": {k: {"r": v[0], "g": v[1], "b": v[2]} for k, v in self.palette.items()},
             "fringe": self.fringe, "wear": self.wear, "seed": self.seed, "pile": self.pile}
        if self.image_path:
            d["imagePath"] = self.image_path
        return d

    @staticmethod
    def from_json(d) -> "RugDesign":
        # every key is optional, like the Mac decoder
        des = RugDesign()
        if not isinstance(d, dict):
            return des
        if d.get("style") in STYLES:
            des.style = d["style"]
        try:
            pal = d["palette"]
            des.palette = {k: (float(pal[k]["r"]), float(pal[k]["g"]), float(pal[k]["b"])) for k in SLOTS}
        except Exception:
            pass
        if isinstance(d.get("fringe"), bool):
            des.fringe = d["fringe"]
        if isinstance(d.get("wear"), (int, float)):
            des.wear = float(d["wear"])
        if isinstance(d.get("seed"), int):
            des.seed = d["seed"]
        if isinstance(d.get("imagePath"), str):
            des.image_path = d["imagePath"]
        if isinstance(d.get("pile"), (int, float)):
            des.pile = float(d["pile"])
        return des

    def copy(self) -> "RugDesign":
        return replace(self, palette=dict(self.palette))


@dataclass
class RugState:
    id: str
    cx: float
    cy: float
    w: float
    h: float
    angle: float = 0.0
    design: RugDesign = field(default_factory=RugDesign)
    positions: np.ndarray | None = None   # (N, 3) float32 cloth points (folds)
    below_icons: bool = False
    heading: float | None = None          # (Linux port) the angle double-click straightens to
    restore: tuple | None = None          # (Linux port) (cx, cy, w, h, angle) while it fills the screen

    def to_json(self):
        d = {"id": self.id, "cx": self.cx, "cy": self.cy, "w": self.w, "h": self.h, "angle": self.angle,
             "design": self.design.to_json()}
        if self.positions is not None:
            d["positions"] = base64.b64encode(np.ascontiguousarray(self.positions, "<f4").tobytes()).decode()
        d["belowIcons"] = self.below_icons
        if self.heading is not None:
            d["heading"] = self.heading
        if self.restore is not None:
            d["restore"] = dict(zip(("cx", "cy", "w", "h", "angle"), self.restore))
        return d

    @staticmethod
    def from_json(d) -> "RugState":
        pos = None
        if isinstance(d.get("positions"), str):
            try:
                pos = np.frombuffer(base64.b64decode(d["positions"]), "<f4").reshape(-1, 3).copy()
            except Exception:
                pos = None
        restore = None
        try:
            restore = tuple(float(d["restore"][k]) for k in ("cx", "cy", "w", "h", "angle"))
        except (KeyError, TypeError, ValueError):
            pass
        heading = d.get("heading")
        return RugState(id=str(d["id"]), cx=float(d["cx"]), cy=float(d["cy"]), w=float(d["w"]), h=float(d["h"]),
                        angle=float(d["angle"]), design=RugDesign.from_json(d["design"]), positions=pos,
                        below_icons=bool(d.get("belowIcons", False)),
                        heading=float(heading) if isinstance(heading, (int, float)) else None, restore=restore)


@dataclass
class Composition:
    id: str
    name: str
    rugs: list = field(default_factory=list)

    def to_json(self):
        return {"id": self.id, "name": self.name, "rugs": [r.to_json() for r in self.rugs]}

    @staticmethod
    def from_json(d) -> "Composition":
        return Composition(id=str(d["id"]), name=str(d["name"]), rugs=[RugState.from_json(r) for r in d["rugs"]])


def new_id() -> str:
    return str(uuid.uuid4()).upper()


def random_seed() -> int:
    return random.randrange(0, 100000)


class Prefs:
    """The key/value store (the Mac app's UserDefaults)."""

    def __init__(self):
        self.path = config_dir() / "settings.json"
        try:
            self.data = json.loads(self.path.read_text())
            if not isinstance(self.data, dict):
                self.data = {}
        except Exception:
            self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self._write()

    def _write(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)


def load_compositions(prefs: Prefs):
    comps = prefs.get("compositions.v1")
    try:
        comps = [Composition.from_json(c) for c in comps]
        if comps:
            return comps, prefs.get("activeComposition")
    except Exception:
        pass
    return [Composition(new_id(), "My Rugs", [])], None


def save_compositions(prefs: Prefs, comps, active_id):
    try:
        prefs.data["compositions.v1"] = [c.to_json() for c in comps]
    except Exception:
        pass
    prefs.data["activeComposition"] = active_id
    prefs._write()


def unique_name(comps, base: str) -> str:
    names = {c.name for c in comps}
    if base not in names:
        return base
    n = 2
    while f"{base} {n}" in names:
        n += 1
    return f"{base} {n}"
