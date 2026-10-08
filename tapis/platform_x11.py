"""X11 details for the desktop layer window: keep it above the desktop (and
its icons) but below every normal window, out of the taskbar and on every
virtual desktop, and let clicks fall through everywhere except on a rug."""

import ctypes
import ctypes.util

_xlib = _xext = None
_dpy = None


class _XRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short),
                ("w", ctypes.c_ushort), ("h", ctypes.c_ushort)]


class _XClientMessage(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong), ("send_event", ctypes.c_int),
                ("display", ctypes.c_void_p), ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                ("format", ctypes.c_int), ("data", ctypes.c_long * 5)]


class _XEvent(ctypes.Union):
    _fields_ = [("xclient", _XClientMessage), ("pad", ctypes.c_long * 24)]


def _init() -> bool:
    global _xlib, _xext, _dpy
    if _dpy:
        return True
    try:
        _xlib = ctypes.CDLL(ctypes.util.find_library("X11"))
        _xext = ctypes.CDLL(ctypes.util.find_library("Xext"))
    except OSError:
        return False
    _xlib.XOpenDisplay.restype = ctypes.c_void_p
    _xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    _xlib.XInternAtom.restype = ctypes.c_ulong
    _xlib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    _xlib.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    _xlib.XFlush.argtypes = [ctypes.c_void_p]
    _xlib.XDefaultRootWindow.restype = ctypes.c_ulong
    _xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    _xlib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long,
                                 ctypes.POINTER(_XEvent)]
    _xext.XShapeCombineRectangles.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                              ctypes.c_int, ctypes.POINTER(_XRect), ctypes.c_int,
                                              ctypes.c_int, ctypes.c_int]
    _dpy = _xlib.XOpenDisplay(None)
    return bool(_dpy)


def available() -> bool:
    return _init()


def _atom(name: str) -> int:
    return _xlib.XInternAtom(_dpy, name.encode(), 0)


def make_desktop_layer(wid: int):
    """Below normal windows, above the desktop; skip taskbar/pager; sticky."""
    if not _init():
        return
    XA_ATOM = 4
    names = ("_NET_WM_STATE_BELOW", "_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER",
             "_NET_WM_STATE_STICKY", "_KDE_NET_WM_STATE_SKIP_SWITCHER")
    states = (ctypes.c_ulong * len(names))(*(_atom(n) for n in names))
    _xlib.XChangeProperty(_dpy, wid, _atom("_NET_WM_STATE"), XA_ATOM, 32, 0, states, len(names))
    desktop = (ctypes.c_ulong * 1)(0xFFFFFFFF)
    _xlib.XChangeProperty(_dpy, wid, _atom("_NET_WM_DESKTOP"), 6, 32, 0, desktop, 1)  # XA_CARDINAL
    # Also ask the running window manager (the property alone only counts before mapping).
    root = _xlib.XDefaultRootWindow(_dpy)
    for first, second in (("_NET_WM_STATE_BELOW", "_NET_WM_STATE_STICKY"),
                          ("_NET_WM_STATE_SKIP_TASKBAR", "_NET_WM_STATE_SKIP_PAGER"),
                          ("_KDE_NET_WM_STATE_SKIP_SWITCHER", "_KDE_NET_WM_STATE_SKIP_SWITCHER")):
        ev = _XEvent()
        ev.xclient.type = 33  # ClientMessage
        ev.xclient.window = wid
        ev.xclient.message_type = _atom("_NET_WM_STATE")
        ev.xclient.format = 32
        ev.xclient.data[0] = 1  # _NET_WM_STATE_ADD
        ev.xclient.data[1] = _atom(first)
        ev.xclient.data[2] = _atom(second)
        ev.xclient.data[3] = 1
        _xlib.XSendEvent(_dpy, root, 0, (1 << 20) | (1 << 19), ctypes.byref(ev))
    _xlib.XFlush(_dpy)


def set_input_region(wid: int, rects):
    """Only these rectangles (window pixels) take the mouse; elsewhere clicks
    reach the desktop and its icons."""
    if not _init():
        return
    n = len(rects)
    arr = (_XRect * max(n, 1))(*[_XRect(int(x), int(y), max(int(w), 1), max(int(h), 1)) for x, y, w, h in rects])
    SHAPE_INPUT, SHAPE_SET, UNSORTED = 2, 0, 0
    _xext.XShapeCombineRectangles(_dpy, wid, SHAPE_INPUT, 0, 0, arr, n, SHAPE_SET, UNSORTED)
    _xlib.XFlush(_dpy)


def pointer_buttons() -> int:
    """Mouse buttons currently held anywhere on the screen (bitmask)."""
    if not _init():
        return 0
    if not hasattr(pointer_buttons, "_set"):
        _xlib.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + [ctypes.c_void_p] * 7
        _xlib.XQueryPointer.restype = ctypes.c_int
        pointer_buttons._set = True
    root = _xlib.XDefaultRootWindow(_dpy)
    r, c = ctypes.c_ulong(), ctypes.c_ulong()
    rx, ry, wx, wy = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
    mask = ctypes.c_uint()
    _xlib.XQueryPointer(_dpy, root, ctypes.byref(r), ctypes.byref(c), ctypes.byref(rx), ctypes.byref(ry),
                        ctypes.byref(wx), ctypes.byref(wy), ctypes.byref(mask))
    return (mask.value >> 8) & 0x1F   # Button1Mask..Button5Mask
