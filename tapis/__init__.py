"""Tapis for Linux — a cloth rug for your desktop.

A port of Tapis for macOS by Romain Lods (MIT License)."""

import OpenGL

# Must be set before anything imports OpenGL.GL: per-call error checks would
# slow the cloth solver's hundreds of calls per frame.
OpenGL.ERROR_CHECKING = False
OpenGL.ERROR_LOGGING = False

__version__ = "0.2.7"
