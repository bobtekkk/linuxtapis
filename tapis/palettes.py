"""The 7 built-in rug palettes, in menu order (same values as the Mac app).

Each palette has five colours: field, border, accent, light ("Ivory") and
dark ("Ink"), as (r, g, b) floats in 0..1 (sRGB)."""


def _c(r, g, b):
    return (r / 255.0, g / 255.0, b / 255.0)


def _pal(field, border, accent, light, dark):
    return {"field": _c(*field), "border": _c(*border), "accent": _c(*accent),
            "light": _c(*light), "dark": _c(*dark)}


PALETTES: list[tuple[str, dict]] = [
    ("Crimson", _pal((156, 46, 31), (28, 36, 67), (111, 134, 166), (234, 220, 194), (20, 22, 38))),
    ("Indigo", _pal((35, 48, 94), (142, 42, 38), (197, 154, 76), (233, 222, 198), (16, 20, 42))),
    ("Sage", _pal((125, 140, 106), (61, 74, 58), (196, 138, 90), (239, 230, 210), (35, 41, 31))),
    ("Saffron", _pal((201, 138, 46), (106, 44, 30), (47, 92, 99), (242, 228, 196), (42, 24, 16))),
    ("Charcoal", _pal((58, 58, 61), (31, 31, 33), (163, 139, 109), (217, 211, 199), (17, 17, 18))),
    ("Blush", _pal((216, 167, 160), (140, 90, 99), (110, 143, 138), (245, 236, 228), (74, 46, 51))),
    ("Lavender", _pal((142, 107, 196), (110, 79, 166), (184, 155, 224), (217, 201, 242), (62, 42, 99))),
]
