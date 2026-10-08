"""Fabric sounds: the synth lives in native/fabric.c (built once into the
cache folder); its stereo float samples are streamed to PipeWire/PulseAudio.
The stream only runs while something moves and stops ~1.5 s after."""

import ctypes
import fcntl
import hashlib
import os
import shutil
import subprocess
import threading
from pathlib import Path

RATE = 48000
BLOCK = 256

_SRC = Path(__file__).parent / "native" / "fabric.c"


class _Fabric(ctypes.Structure):
    _fields_ = [("rustle", ctypes.c_float), ("thump", ctypes.c_float), ("softness", ctypes.c_float)]


def _build_library():
    src = _SRC.read_bytes()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "tapis"
    cache.mkdir(parents=True, exist_ok=True)
    lib = cache / f"fabric-{hashlib.sha1(src).hexdigest()[:12]}.so"
    if not lib.exists():
        cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
        if not cc:
            return None
        tmp = lib.with_suffix(".tmp")
        r = subprocess.run([cc, "-O2", "-shared", "-fPIC", "-o", str(tmp), str(_SRC), "-lm"],
                           capture_output=True)
        if r.returncode != 0:
            return None
        tmp.replace(lib)
    try:
        dll = ctypes.CDLL(str(lib))
    except OSError:
        return None
    dll.fabric_init.argtypes = [ctypes.c_void_p, ctypes.c_float]
    dll.fabric_render.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    dll.fabric_size.restype = ctypes.c_int
    return dll


def _player_command():
    if shutil.which("pw-cat"):
        return ["pw-cat", "--playback", "--raw", "--format", "f32", "--rate", str(RATE), "--channels", "2",
                "--latency", "20ms", "--media-role", "Game", "-"]
    if shutil.which("pacat"):
        return ["pacat", "--playback", "--raw", "--format=float32le", f"--rate={RATE}", "--channels=2",
                "--latency-msec=20", "--client-name=Tapis"]
    return None


class FabricSound:
    def __init__(self, prefs):
        self.prefs = prefs
        v = prefs.get("fabricSounds")
        self.enabled = v if isinstance(v, bool) else True
        self.silent_frames = 0
        self._dll = None
        self._state = None
        self._proc = None
        self._thread = None
        self._running = False

    # ---- menu
    def toggle(self):
        self.enabled = not self.enabled
        self.prefs.set("fabricSounds", self.enabled)
        if not self.enabled:
            self._stop()

    # ---- per frame, from the rug loop
    def update(self, motion: float, impact: float, softness: float):
        if not self.enabled:
            return
        rustle = min(max((motion - 0.06) / 2.5, 0.0), 1.0) ** 0.7
        thump = min(impact * 18.0, 1.0) if impact > 0.006 else 0.0
        st = self._state
        if st is not None:
            st.softness = softness
            st.rustle = rustle
            if thump > 0:
                st.thump = max(st.thump, thump)
        if thump > 0 or rustle > 0:
            self.silent_frames = 0
            if not self._running:
                self._start(rustle, thump, softness)
        elif self._running:
            self.silent_frames += 1
            if self.silent_frames >= 91:
                self._stop()

    # ---- engine
    def _start(self, rustle, thump, softness):
        if self._dll is None:
            self._dll = _build_library() or False
        cmd = _player_command()
        if not self._dll or not cmd:
            return
        buf = ctypes.create_string_buffer(self._dll.fabric_size())
        self._dll.fabric_init(buf, RATE)
        state = _Fabric.from_buffer(buf)
        state.rustle, state.thump, state.softness = rustle, thump, softness
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError:
            return
        try:
            fcntl.fcntl(proc.stdin.fileno(), 1031, 4096)   # F_SETPIPE_SZ: keep latency low
        except OSError:
            pass
        self._buf, self._state, self._proc, self._running = buf, state, proc, True
        self._thread = threading.Thread(target=self._pump, args=(proc, buf), daemon=True)
        self._thread.start()

    def _pump(self, proc, buf):
        out = (ctypes.c_float * (BLOCK * 2))()
        raw = memoryview(out).cast("B")
        while self._running and self._proc is proc:
            self._dll.fabric_render(buf, out, BLOCK)
            try:
                proc.stdin.write(raw)
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                break

    def _stop(self):
        self._running = False
        proc, self._proc = self._proc, None
        self._state = None
        if proc:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
            except Exception:
                pass

    def shutdown(self):
        self._stop()
