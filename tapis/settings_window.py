"""The Settings window (Linux port): how soft the cloth is, and whether
corners prefer to fold up."""

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget

from . import cloth
from .tr import tr


class SettingsWindow(QWidget):
    def __init__(self, prefs, controller):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.prefs = prefs
        self.ctrl = controller
        self.setWindowTitle(tr("Tapis Settings"))
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        head = QLabel(f"<b>{tr('Softness')}</b>")
        lay.addWidget(head)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(int(round(float(prefs.get("softness", 0.0)) * 100)))
        self.slider.setMinimumWidth(300)
        lay.addWidget(self.slider)
        ends = QHBoxLayout()
        left, right = QLabel(tr("Heavy rug")), QLabel(tr("Bed sheet"))
        for l in (left, right):
            l.setEnabled(False)       # the dimmer secondary text colour
        ends.addWidget(left)
        ends.addStretch(1)
        ends.addWidget(right)
        lay.addLayout(ends)

        lay.addSpacing(6)
        self.corners = QCheckBox(tr("Corners Fold Up"))
        self.corners.setChecked(bool(prefs.get("cornersFoldUp", False)))
        self.corners.setToolTip(tr("Edges and corners curl up and lie on top, instead of folding down and under."))
        lay.addWidget(self.corners)

        # applying rebuilds every rug's links, so wait until the slider rests a moment
        self._apply_timer = QTimer(self)
        self._apply_timer.setSingleShot(True)
        self._apply_timer.setInterval(120)
        self._apply_timer.timeout.connect(self._apply)
        self.slider.valueChanged.connect(lambda _: self._apply_timer.start())
        self.corners.toggled.connect(lambda _: self._apply())
        self.adjustSize()

    def _apply(self):
        softness = self.slider.value() / 100.0
        corners = self.corners.isChecked()
        self.prefs.set("softness", softness)
        self.prefs.set("cornersFoldUp", corners)
        cloth.set_softness(softness)
        cloth.FEEL["corners_up"] = corners
        self.ctrl.ctx.make_current()
        for r in self.ctrl.rugs:
            r.sim.refresh_constraints()
            r.sim.set_thickness(r.design.pile)
        self.ctrl.redraw = True

    def show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()
