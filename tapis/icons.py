"""Where the desktop icons are, so rugs can drape over them.

The Mac app asks Finder for icon positions. On KDE Plasma the desktop's icons
are exposed through the accessibility bus (AT-SPI): each icon is a "canvas"
inside plasmashell's "Desktop @ QRect(...)" window, with its on-screen box.
Nothing but those boxes is read. The query runs in a short-lived helper
process so a slow or stuck accessibility bus can never freeze the rugs.
"""

import json
import subprocess
import sys
import threading

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

_QUERY = r"""
import json, gi
gi.require_version('Atspi', '2.0')
from gi.repository import Atspi
out = {"icons": [], "found": False}
desk = Atspi.get_desktop(0)
for i in range(desk.get_child_count()):
    app = desk.get_child_at_index(i)
    if app is None or app.get_name() != "plasmashell":
        continue
    for w in range(app.get_child_count()):
        win = app.get_child_at_index(w)
        if win is None or not (win.get_name() or "").startswith("Desktop @"):
            continue
        out["found"] = True
        for c in range(win.get_child_count()):
            item = win.get_child_at_index(c)
            if item is None or item.get_role_name() != "canvas":
                continue
            e = item.get_extents(Atspi.CoordType.SCREEN)
            if e.width <= 0 or e.height <= 0:
                continue
            label_top = None
            label = None
            for k in range(item.get_child_count()):
                ch = item.get_child_at_index(k)
                if ch is not None and ch.get_role_name() == "label":
                    le = ch.get_extents(Atspi.CoordType.SCREEN)
                    if le.height > 0:
                        label_top = le.y
                        label = [le.x, le.y, le.width, le.height]
            out["icons"].append([e.x, e.y, e.width, e.height, label_top, item.get_name() or "", label])
print(json.dumps(out))
"""


def query_icons(timeout: float = 8.0):
    """Returns (centres [(x, y)], icon_size, items) in screen points, or None
    if the desktop could not be read. items: [(name, (x, y, w, h), label box)]."""
    try:
        r = subprocess.run([sys.executable, "-I", "-c", _QUERY], capture_output=True,
                           text=True, timeout=timeout)
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return None
    if not data.get("found"):
        return None
    centres, sizes, items = [], [], []
    for x, y, w, h, label_top, name, label in data["icons"]:
        items.append((name, (x, y, w, h), tuple(label) if label else None))
        # The item box holds the icon image on top and its name below.
        image_h = (label_top - y) if label_top and label_top > y else min(w, h)
        size = max(16.0, image_h - 8.0)
        sizes.append(size)
        centres.append((x + w / 2.0, y + 4.0 + size / 2.0))
    size = sorted(sizes)[len(sizes) // 2] if sizes else 64.0
    return centres, size, items


class DesktopIcons(QObject):
    """Polls the icon layout in the background and reports changes."""

    changed = pyqtSignal()

    def __init__(self, interval_ms: int = 5000):
        super().__init__()
        self.positions: list[tuple[float, float]] = []
        self.icon_size = 64.0
        self.items = []
        self.available = True
        self._busy = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(interval_ms)
        self._result = None
        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._collect)

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        self._result = None

        def work():
            self._result = query_icons() or "failed"
        threading.Thread(target=work, daemon=True).start()
        self._poll.start()

    def _collect(self):
        if self._result is None:
            return
        self._poll.stop()
        self._busy = False
        res, self._result = self._result, None
        if res == "failed":
            self.available = False
            return
        self.available = True
        positions, size, items = res
        positions = [(round(x, 1), round(y, 1)) for x, y in positions]
        if positions != self.positions or size != self.icon_size or items != self.items:
            self.positions, self.icon_size, self.items = positions, size, items
            self.changed.emit()
