"""Small OpenGL helpers: shader programs built from the ported Metal sources,
buffers and textures. Everything runs in the desktop window's GL context."""

from pathlib import Path

import numpy as np
import OpenGL

OpenGL.ERROR_CHECKING = False
OpenGL.ERROR_LOGGING = False
from OpenGL import GL  # noqa: E402

SHADERS = Path(__file__).parent / "shaders"
_sources: dict[str, str] = {}


def _source(name: str) -> str:
    if name not in _sources:
        _sources[name] = (SHADERS / name).read_text()
    return _sources[name]


def _compile(kind, text: str, label: str) -> int:
    s = GL.glCreateShader(kind)
    GL.glShaderSource(s, text)
    GL.glCompileShader(s)
    if not GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS):
        log = GL.glGetShaderInfoLog(s)
        raise RuntimeError(f"{label}: {log.decode() if isinstance(log, bytes) else log}")
    return s


def _link(shaders, label: str) -> int:
    p = GL.glCreateProgram()
    for s in shaders:
        GL.glAttachShader(p, s)
    GL.glLinkProgram(p)
    if not GL.glGetProgramiv(p, GL.GL_LINK_STATUS):
        log = GL.glGetProgramInfoLog(p)
        raise RuntimeError(f"{label}: {log.decode() if isinstance(log, bytes) else log}")
    for s in shaders:
        GL.glDeleteShader(s)
    return p


# The buffers each cloth kernel touches (drivers count every declared block).
KERNEL_BUFFERS = {
    "clearSupport": "Support",
    "rasterSupport": "Support LowerPos LowerIdx",
    "frameBegin": "Pos Prev Ground Contact FrameStart ContactAtStart Icons Support",
    "integrate": "Pos Prev Ground Contact Pinned Flatten",
    "clearBuckets": "BucketCount",
    "countTriangles": "Pos Index BucketCount",
    "scanBuckets": "BucketCount BucketStart",
    "fillTriangles": "Pos Index BucketCount BucketItems BucketStart",
    "solveBatch": "Pos Cons",
    "limitBatch": "Pos Cons",
    "applyTargets": "Pos Ground Pinned GrabOff Guide",
    "collide": "Pos Prev Contact Delta Index BucketItems BucketStart",
    "applyDeltas": "Pos Ground Contact Delta",
    "friction": "Pos Prev Ground Contact WasContact Anchor Pinned Stats",
    "frameEnd": "Pos Contact Pinned FrameStart ContactAtStart Stats",
    "refine": "Pos Ground VertexOut",
    "refineNormals": "VertexOut",
    "tether": "Pos Pinned UV",
    "edgeLift": "Pos Pinned",
}


def compute_program(kernel: str) -> int:
    uses = "".join(f"#define USE_{b}\n" for b in KERNEL_BUFFERS[kernel].split())
    text = f"#version 460\n#define K_{kernel}\n{uses}" + _source("cloth.glsl")
    return _link([_compile(GL.GL_COMPUTE_SHADER, text, kernel)], kernel)


def render_program(vs: str, fs: str) -> int:
    src = _source("render.glsl")
    v = _compile(GL.GL_VERTEX_SHADER, f"#version 460\n#define {vs}\n" + src, vs)
    f = _compile(GL.GL_FRAGMENT_SHADER, f"#version 460\n#define {fs}\n" + src, fs)
    return _link([v, f], f"{vs}+{fs}")


class Buffer:
    """A GL buffer object used as SSBO / UBO / vertex or index storage."""

    def __init__(self, data=None, nbytes: int = 0, usage=GL.GL_DYNAMIC_DRAW):
        self.id = GL.glGenBuffers(1)
        self.size = 0
        if data is not None:
            self.upload(data, usage)
        elif nbytes:
            self.alloc(nbytes, usage)

    def alloc(self, nbytes: int, usage=GL.GL_DYNAMIC_DRAW):
        GL.glBindBuffer(GL.GL_COPY_WRITE_BUFFER, self.id)
        GL.glBufferData(GL.GL_COPY_WRITE_BUFFER, max(nbytes, 16), None, usage)
        self.size = max(nbytes, 16)
        self.clear()

    def upload(self, data: np.ndarray, usage=GL.GL_DYNAMIC_DRAW):
        data = np.ascontiguousarray(data)
        GL.glBindBuffer(GL.GL_COPY_WRITE_BUFFER, self.id)
        if data.nbytes > self.size or self.size == 0:
            GL.glBufferData(GL.GL_COPY_WRITE_BUFFER, max(data.nbytes, 16), data if data.nbytes else None, usage)
            self.size = max(data.nbytes, 16)
        elif data.nbytes:
            GL.glBufferSubData(GL.GL_COPY_WRITE_BUFFER, 0, data.nbytes, data)

    def write(self, data: np.ndarray, offset: int = 0):
        data = np.ascontiguousarray(data)
        GL.glBindBuffer(GL.GL_COPY_WRITE_BUFFER, self.id)
        GL.glBufferSubData(GL.GL_COPY_WRITE_BUFFER, offset, data.nbytes, data)

    def clear(self):
        GL.glClearNamedBufferData(self.id, GL.GL_R32UI, GL.GL_RED_INTEGER, GL.GL_UNSIGNED_INT, None)

    def read(self, dtype, count: int = -1, offset: int = 0) -> np.ndarray:
        itemsize = np.dtype(dtype).itemsize
        nbytes = (self.size - offset) if count < 0 else count * itemsize
        out = np.empty(nbytes // itemsize, dtype)
        GL.glGetNamedBufferSubData(self.id, offset, out.nbytes, out)
        return out

    def ssbo(self, binding: int):
        GL.glBindBufferBase(GL.GL_SHADER_STORAGE_BUFFER, binding, self.id)

    def ubo(self, binding: int):
        GL.glBindBufferBase(GL.GL_UNIFORM_BUFFER, binding, self.id)

    def delete(self):
        if self.id:
            GL.glDeleteBuffers(1, [self.id])
            self.id = 0


def texture_rgba(width: int, height: int, data: bytes | None, mipmaps: bool = True) -> int:
    """An RGBA8 texture from premultiplied pixels (top row first, like the
    Mac CGContext bitmap; uv (0,0) is the rug's top-left)."""
    t = GL.glGenTextures(1)
    GL.glBindTexture(GL.GL_TEXTURE_2D, t)
    GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, width, height, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, data)
    if mipmaps:
        GL.glGenerateMipmap(GL.GL_TEXTURE_2D)
    return t


def update_texture(t: int, width: int, height: int, data: bytes, mipmaps: bool = True):
    GL.glBindTexture(GL.GL_TEXTURE_2D, t)
    GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
    GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA8, width, height, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, data)
    if mipmaps:
        GL.glGenerateMipmap(GL.GL_TEXTURE_2D)


def sampler(min_filter, mag_filter, wrap, anisotropy: float = 1.0) -> int:
    s = GL.glGenSamplers(1)
    GL.glSamplerParameteri(s, GL.GL_TEXTURE_MIN_FILTER, min_filter)
    GL.glSamplerParameteri(s, GL.GL_TEXTURE_MAG_FILTER, mag_filter)
    GL.glSamplerParameteri(s, GL.GL_TEXTURE_WRAP_S, wrap)
    GL.glSamplerParameteri(s, GL.GL_TEXTURE_WRAP_T, wrap)
    if anisotropy > 1.0:
        GL.glSamplerParameterf(s, 0x84FE, anisotropy)  # GL_TEXTURE_MAX_ANISOTROPY
    return s


def groups(n: int, size: int = 64) -> int:
    return max(1, (n + size - 1) // size)


class _Fast:
    """Direct ctypes entry points for the calls issued hundreds of times per
    frame by the cloth solver (PyOpenGL's wrappers are too slow for that)."""

    def __init__(self):
        import ctypes
        import ctypes.util
        lib = ctypes.CDLL(ctypes.util.find_library("GL") or "libGL.so.1")
        get = lib.glXGetProcAddressARB
        get.restype = ctypes.c_void_p
        get.argtypes = [ctypes.c_char_p]
        u, i, v = ctypes.c_uint, ctypes.c_int, None

        def fn(name, *args):
            return ctypes.CFUNCTYPE(v, *args)(get(name.encode()))
        self.useProgram = fn("glUseProgram", u)
        self.uniform2ui = fn("glUniform2ui", i, u, u)
        self.uniform4ui = fn("glUniform4ui", i, u, u, u, u)
        self.dispatch = fn("glDispatchCompute", u, u, u)
        self.barrier = fn("glMemoryBarrier", u)
        self.bindBase = fn("glBindBufferBase", u, u, u)
        self.waitSync = fn("glWaitSync", ctypes.c_void_p, u, ctypes.c_uint64)

    def wait_sync(self, fence):
        import ctypes
        ptr = fence if isinstance(fence, int) else ctypes.cast(fence, ctypes.c_void_p).value
        self.waitSync(ptr, 0, 0xFFFFFFFFFFFFFFFF)   # GL_TIMEOUT_IGNORED


_fast = None


def fast() -> _Fast:
    global _fast
    if _fast is None:
        _fast = _Fast()
    return _fast
