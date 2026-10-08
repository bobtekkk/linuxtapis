"""The overlay drawn on top of the rugs: selection handles, the rotate knob,
the toolbar ("pill") under a selected rug and the "Press and hold" hint.
Icons are drawn as vector glyphs in the spirit of the Mac's SF Symbols."""

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen

from .tr import tr

SYSTEM_BLUE = QColor(10, 132, 255)
PILL_ICON = 20.0
PILL_SPACING = 18.0
PILL_INSET_X, PILL_INSET_Y = 20.0, 10.0
BUTTONS = ["design", "smooth", "rotate", "fill", "layer", "remove"]


def pill_size():
    n = len(BUTTONS)
    return (PILL_INSET_X * 2 + n * PILL_ICON + (n - 1) * PILL_SPACING, PILL_INSET_Y * 2 + PILL_ICON)


def pill_tooltips(below: bool, filled: bool = False):
    return {
        "design": tr("Design"),
        "smooth": tr("Smooth out (or double-click the rug)"),
        "rotate": tr("Rotate 90°"),
        "fill": tr("Restore size") if filled else tr("Fill screen"),
        "layer": tr("Place above icons") if below else tr("Place below icons"),
        "remove": tr("Remove rug"),
    }


def button_rects(pill: QRectF):
    out = {}
    x = pill.x() + PILL_INSET_X
    for b in BUTTONS:
        out[b] = QRectF(x - PILL_SPACING / 2, pill.y(), PILL_ICON + PILL_SPACING, pill.height())
        x += PILL_ICON + PILL_SPACING
    return out


def _pen(color, w=1.6):
    p = QPen(color, w)
    p.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return p


def draw_glyph(p: QPainter, name: str, r: QRectF, color: QColor, below: bool = False, filled: bool = False):
    """A 20×20-ish glyph centred in r."""
    s = min(r.width(), r.height()) / 20.0
    p.save()
    p.translate(r.center())
    p.scale(s, s)
    p.setPen(_pen(color))
    p.setBrush(Qt.BrushStyle.NoBrush)
    if name == "design":                      # paintpalette
        path = QPainterPath()
        path.moveTo(0, -8)
        path.cubicTo(6, -8, 9, -4, 9, 0)
        path.cubicTo(9, 3.5, 6.5, 4.5, 4.5, 4)
        path.cubicTo(2.5, 3.5, 1.5, 5, 2.5, 6.5)
        path.cubicTo(3.5, 8.5, 1, 9, 0, 9)
        path.cubicTo(-6, 9, -9, 4.5, -9, 0)
        path.cubicTo(-9, -5, -5, -8, 0, -8)
        p.drawPath(path)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        for cx, cy in ((-4.8, 1.8), (-4.2, -3.4), (0.4, -5.2), (4.8, -2.6)):
            p.drawEllipse(QPointF(cx, cy), 1.5, 1.5)
    elif name == "smooth":                    # arrow.up.left.and.arrow.down.right
        p.drawLine(QPointF(-7, -7), QPointF(7, 7))
        for sx in (-1, 1):
            tip = QPointF(7 * sx, 7 * sx)
            p.drawLine(tip, QPointF(7 * sx - 5 * sx, 7 * sx))
            p.drawLine(tip, QPointF(7 * sx, 7 * sx - 5 * sx))
    elif name == "rotate":                    # rotate.right
        p.drawRoundedRect(QRectF(-8, -1, 10, 9), 1.5, 1.5)
        arc = QPainterPath()
        arc.moveTo(-5, -4)
        arc.cubicTo(-5, -8.5, 3, -9.5, 6, -4.5)
        arc.lineTo(6.5, 2)
        p.drawPath(arc)
        p.drawLine(QPointF(6.5, 2.5), QPointF(3.8, -0.5))
        p.drawLine(QPointF(6.5, 2.5), QPointF(9, -0.6))
    elif name == "fill":                      # fill screen / restore (corner brackets)
        a, b = 8.0, 3.6
        for sx in (-1, 1):
            for sy in (-1, 1):
                if filled:                    # brackets pointing inward: back to normal size
                    cx, cy = sx * b, sy * b
                    p.drawLine(QPointF(cx, cy), QPointF(cx + sx * (a - b), cy))
                    p.drawLine(QPointF(cx, cy), QPointF(cx, cy + sy * (a - b)))
                else:
                    cx, cy = sx * a, sy * a
                    p.drawLine(QPointF(cx, cy), QPointF(cx - sx * (a - b), cy))
                    p.drawLine(QPointF(cx, cy), QPointF(cx, cy - sy * (a - b)))
    elif name == "layer":                     # square.2.layers.3d.{bottom,top}.filled
        def rhombus(dy):
            path = QPainterPath()
            path.moveTo(0, -5 + dy)
            path.lineTo(9, 0 + dy)
            path.lineTo(0, 5 + dy)
            path.lineTo(-9, 0 + dy)
            path.closeSubpath()
            return path
        top, bottom = rhombus(-3.2), rhombus(3.2)
        if below:       # currently below icons -> "Place above icons": top layer filled
            p.setBrush(color)
            p.drawPath(top)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(bottom.subtracted(top))
        else:
            p.setBrush(color)
            p.drawPath(bottom)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(top)
    elif name == "remove":                    # trash
        p.drawLine(QPointF(-8, -5.5), QPointF(8, -5.5))
        p.drawRoundedRect(QRectF(-3, -8.5, 6, 3), 1, 1)
        body = QPainterPath()
        body.moveTo(-6.3, -5.5)
        body.lineTo(-5.3, 8)
        body.lineTo(5.3, 8)
        body.lineTo(6.3, -5.5)
        p.drawPath(body)
        for x in (-2.4, 0, 2.4):
            p.drawLine(QPointF(x, -2.5), QPointF(x * 0.9, 5))
    elif name == "clockwise":                 # arrow.clockwise (the rotate knob)
        p.setPen(_pen(color, 2.2))
        path = QPainterPath()
        path.arcMoveTo(QRectF(-6, -6, 12, 12), 70)
        path.arcTo(QRectF(-6, -6, 12, 12), 70, -300)
        p.drawPath(path)
        tip = QPointF(6 * math.cos(math.radians(70)), -6 * math.sin(math.radians(70)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        head = QPainterPath()
        head.moveTo(tip + QPointF(3.6, 0.2))
        head.lineTo(tip + QPointF(-1.6, -3.4))
        head.lineTo(tip + QPointF(-1.2, 3.4))
        head.closeSubpath()
        p.drawPath(head)
    p.restore()


def draw_capsule(p: QPainter, r: QRectF, alpha: float = 1.0):
    p.save()
    p.setOpacity(alpha)
    p.setPen(QPen(QColor(255, 255, 255, 34), 1))
    p.setBrush(QColor(30, 30, 33, 214))
    rad = r.height() / 2
    p.drawRoundedRect(r, rad, rad)
    p.restore()


def draw_selection(p: QPainter, rug, origin, hover):
    """Stem, corner dots and rotate knob (there is no outline)."""
    to = lambda w: QPointF(w[0] - origin[0], w[1] - origin[1])
    knob = to(rug.local_to_world(rug.knob_local()))
    stem = to(rug.local_to_world(rug.stem_base_local()))
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    # soft shadow: a couple of offset translucent passes
    for dy, a in ((1.0, 60), (1.5, 30)):
        p.setPen(QPen(QColor(0, 0, 0, a), 3.0))
        p.drawLine(stem + QPointF(0, dy), knob + QPointF(0, dy))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, a))
        for c in rug.corners():
            p.drawEllipse(to(c) + QPointF(0, dy), 8.5, 8.5)
        p.drawEllipse(knob + QPointF(0, dy), 12.5, 12.5)
    p.setPen(QPen(QColor(255, 255, 255, 230), 1.5))
    p.drawLine(stem, knob)
    p.setPen(Qt.PenStyle.NoPen)
    for i, c in enumerate(rug.corners()):
        col, rad = (SYSTEM_BLUE, 8.0) if hover == i else (QColor(255, 255, 255), 7.0)
        p.setBrush(col)
        p.drawEllipse(to(c), rad, rad)
    p.setBrush(SYSTEM_BLUE if hover == 4 else QColor(255, 255, 255))
    p.drawEllipse(knob, 11, 11)
    glyph = QColor(255, 255, 255) if hover == 4 else QColor(64, 64, 64)
    draw_glyph(p, "clockwise", QRectF(knob.x() - 11, knob.y() - 11, 22, 22), glyph)
    p.restore()


def draw_pill(p: QPainter, rect: QRectF, below: bool, hover: str | None, pressed: str | None,
              filled: bool = False):
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    draw_capsule(p, rect)
    for name, br in button_rects(rect).items():
        icon = QRectF(br.center().x() - PILL_ICON / 2, br.center().y() - PILL_ICON / 2, PILL_ICON, PILL_ICON)
        col = QColor(255, 255, 255, 150 if pressed == name else (255 if hover == name else 235))
        if name == "fill" and filled:         # the Fill screen toggle is on
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(SYSTEM_BLUE)
            p.drawRoundedRect(icon.adjusted(-5, -5, 5, 5), 7, 7)
        draw_glyph(p, name, icon, col, below, filled)
    p.restore()


def hint_font():
    f = QFont()
    f.setPointSizeF(10.0)
    f.setWeight(QFont.Weight.Medium)
    return f


def hint_size():
    fm = QFontMetricsF(hint_font())
    return fm.horizontalAdvance(tr("Press and hold to edit")) + 28, fm.height() + 14


def draw_hint(p: QPainter, rect: QRectF, alpha: float):
    if alpha <= 0:
        return
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    draw_capsule(p, rect, alpha)
    p.setOpacity(alpha)
    p.setFont(hint_font())
    p.setPen(QColor(255, 255, 255))
    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, tr("Press and hold to edit"))
    p.restore()
