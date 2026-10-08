"""Draws the rugs of one screen, pass for pass like the Mac renderer:
a half-resolution shadow mask (max-blended), two blur passes, the ground
shadow, then each rug (flat or fur shells) and its rim, in stacking order."""

import struct

import numpy as np
from OpenGL import GL

from . import cloth, gl
from .strands import make_strands

CAM_D = 2200.0
SHADOW_DIR = (0.43, 0.55)
SHADOW_STRENGTH = 0.26
RIM_THICKNESS = 5.5
UBO_STRIDE = 256


class Shared:
    """Programs, samplers and the strand texture (shared by all contexts)."""
    _inst = None

    def __init__(self):
        self.rug = gl.render_program("RUG_VS", "RUG_FS")
        self.fur = gl.render_program("FUR_VS", "FUR_FS")
        self.rim = gl.render_program("RIM_VS", "RIM_FS")
        self.shadow = gl.render_program("SHADOW_VS", "SHADOW_FS")
        self.blur = gl.render_program("FULLSCREEN_VS", "BLUR_FS")
        self.ground = gl.render_program("FULLSCREEN_VS", "GROUND_FS")
        self.blur_dir = GL.glGetUniformLocation(self.blur, "dir")
        self.sampler = gl.sampler(GL.GL_LINEAR_MIPMAP_LINEAR, GL.GL_LINEAR, GL.GL_CLAMP_TO_EDGE, 8.0)
        self.repeat = gl.sampler(GL.GL_NEAREST, GL.GL_NEAREST, GL.GL_REPEAT)
        self.smooth_repeat = gl.sampler(GL.GL_LINEAR, GL.GL_LINEAR, GL.GL_REPEAT)
        self.strands = gl.texture_rgba(256, 256, make_strands(), mipmaps=False)

    @classmethod
    def get(cls):
        if cls._inst is None:
            cls._inst = Shared()
        return cls._inst


def fur_params(rug):
    pile = float(rug.design.pile) * 18
    shells = float(min(int(pile * 1.5) + 6, 28))
    return pile, shells, rug.size[0], rug.size[1], pile * 4 + 150


class Renderer:
    """Per-window state: VAO, shadow targets, uniform buffers."""

    def __init__(self):
        self.S = Shared.get()
        self.vao = GL.glGenVertexArrays(1)
        self.shadow_size = (0, 0)
        self.shadow_tex = [0, 0]
        self.shadow_fbo = [0, 0]
        self.ubo = gl.Buffer(nbytes=UBO_STRIDE * 64)
        self.fur_ubo = gl.Buffer(nbytes=UBO_STRIDE * 64)
        self.rim_ubo = gl.Buffer(nbytes=UBO_STRIDE * 64)

    def _ensure_shadow(self, W, H):
        sw, sh = (W // 2 if W > 1 else 1), (H // 2 if H > 1 else 1)
        if self.shadow_size == (sw, sh):
            return
        if self.shadow_tex[0]:
            GL.glDeleteTextures(2, self.shadow_tex)
            GL.glDeleteFramebuffers(2, self.shadow_fbo)
        self.shadow_tex = list(GL.glGenTextures(2))
        self.shadow_fbo = list(GL.glGenFramebuffers(2))
        for t, f in zip(self.shadow_tex, self.shadow_fbo):
            GL.glBindTexture(GL.GL_TEXTURE_2D, t)
            GL.glTexStorage2D(GL.GL_TEXTURE_2D, 1, GL.GL_RG16F, sw, sh)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, f)
            GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, t, 0)
        self.shadow_size = (sw, sh)

    def draw_group(self, rugs, origin, view_size, draw_size, target_fbo, clear_color: bool):
        """Draws one layer of rugs (below or above icons) into target_fbo."""
        S = self.S
        W, H = draw_size
        # (QPainter may have run in between: reset what it can leave behind)
        GL.glDisable(GL.GL_SCISSOR_TEST)
        GL.glDisable(GL.GL_STENCIL_TEST)
        GL.glDisable(GL.GL_CULL_FACE)
        GL.glColorMask(True, True, True, True)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, target_fbo)
        GL.glViewport(0, 0, W, H)
        GL.glDepthMask(GL.GL_TRUE)
        if clear_color:
            GL.glClearColor(0, 0, 0, 0)
            GL.glClearDepth(1.0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        else:
            GL.glClearDepth(1.0)
            GL.glClear(GL.GL_DEPTH_BUFFER_BIT)
        if not rugs:
            return
        self._ensure_shadow(W, H)
        GL.glBindVertexArray(self.vao)
        GL.glClipControl(GL.GL_LOWER_LEFT, GL.GL_ZERO_TO_ONE)

        n = len(rugs)
        if self.ubo.size < n * UBO_STRIDE:
            for b in (self.ubo, self.fur_ubo, self.rim_ubo):
                b.alloc(n * UBO_STRIDE * 2)
        ubo = np.zeros(n * UBO_STRIDE, np.uint8)
        fur = np.zeros(n * UBO_STRIDE, np.uint8)
        rim = np.zeros(n * UBO_STRIDE, np.uint8)
        for i, r in enumerate(rugs):
            o = i * UBO_STRIDE
            ubo[o:o + 56] = np.frombuffer(struct.pack(
                "<2f2f2f2f2f4f", origin[0], origin[1], view_size[0], view_size[1], CAM_D, i * 0.002,
                SHADOW_DIR[0], SHADOW_DIR[1], float(W), float(H), SHADOW_STRENGTH, 0, 0, 0), np.uint8)
            fp = fur_params(r)
            fur[o:o + 32] = np.frombuffer(struct.pack("<8f", *fp, 0, 0, 0), np.uint8)
            rim[o:o + 16] = np.frombuffer(struct.pack("<4f", RIM_THICKNESS * cloth.thickness_scale(), 1.0 if fp[0] > 0.5 else 0.0, 0, 0),
                                          np.uint8)
        self.ubo.write(ubo)
        self.fur_ubo.write(fur)
        self.rim_ubo.write(rim)

        def bind_rug_ubos(i):
            GL.glBindBufferRange(GL.GL_UNIFORM_BUFFER, 0, self.ubo.id, i * UBO_STRIDE, 64)
            GL.glBindBufferRange(GL.GL_UNIFORM_BUFFER, 3, self.fur_ubo.id, i * UBO_STRIDE, 32)
            GL.glBindBufferRange(GL.GL_UNIFORM_BUFFER, 4, self.rim_ubo.id, i * UBO_STRIDE, 16)

        # ---- pass 1: shadow mask (max blend), depthBias 0 for every rug
        sw, sh = self.shadow_size
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.shadow_fbo[0])
        GL.glViewport(0, 0, sw, sh)
        GL.glClearColor(0, 0, 0, 0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendEquation(GL.GL_MAX)
        GL.glBlendFunc(GL.GL_ONE, GL.GL_ONE)
        GL.glUseProgram(S.shadow)
        GL.glBindSampler(0, S.sampler)
        GL.glBindBufferRange(GL.GL_UNIFORM_BUFFER, 0, self.ubo.id, 0, 64)   # rug 0's slot has bias 0
        for i, r in enumerate(rugs):
            GL.glBindBufferRange(GL.GL_UNIFORM_BUFFER, 3, self.fur_ubo.id, i * UBO_STRIDE, 32)
            r.sim.bufs["vertex"].ssbo(0)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, r.texture)
            GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, r.sim.render_index_buf.id)
            GL.glDrawElements(GL.GL_TRIANGLES, len(r.sim.render_indices), GL.GL_UNSIGNED_INT, None)
        GL.glBlendEquation(GL.GL_FUNC_ADD)
        GL.glDisable(GL.GL_BLEND)

        # ---- passes 2/3: blur A -> B (horizontal), B -> A (vertical)
        GL.glUseProgram(S.blur)
        GL.glBindSampler(1, S.sampler)
        for src, dst, d in ((0, 1, (2.4 / sw, 0.0)), (1, 0, (0.0, 2.4 / sh))):
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.shadow_fbo[dst])
            GL.glActiveTexture(GL.GL_TEXTURE1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.shadow_tex[src])
            GL.glUniform2f(S.blur_dir, *d)
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)

        # ---- pass 4: ground shadow, rugs, rims
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, target_fbo)
        GL.glViewport(0, 0, W, H)
        GL.glActiveTexture(GL.GL_TEXTURE1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.shadow_tex[0])
        GL.glBindSampler(1, S.sampler)
        GL.glActiveTexture(GL.GL_TEXTURE2)
        GL.glBindTexture(GL.GL_TEXTURE_2D, S.strands)
        GL.glBindSampler(2, S.repeat)
        GL.glActiveTexture(GL.GL_TEXTURE3)
        GL.glBindTexture(GL.GL_TEXTURE_2D, S.strands)
        GL.glBindSampler(3, S.smooth_repeat)

        GL.glUseProgram(S.ground)
        GL.glBindBufferRange(GL.GL_UNIFORM_BUFFER, 0, self.ubo.id, 0, 64)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFunc(GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, 3)
        GL.glDisable(GL.GL_BLEND)

        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDepthFunc(GL.GL_LEQUAL)
        GL.glDepthMask(GL.GL_TRUE)
        for i, r in enumerate(rugs):
            bind_rug_ubos(i)
            pile = float(r.design.pile) * 18
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, r.texture)
            GL.glBindSampler(0, S.sampler)
            r.sim.bufs["vertex"].ssbo(0)
            GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, r.sim.render_index_buf.id)
            count = len(r.sim.render_indices)
            if pile > 0.5:
                GL.glUseProgram(S.fur)
                GL.glDrawElementsInstanced(GL.GL_TRIANGLES, count, GL.GL_UNSIGNED_INT, None,
                                           int(min(int(pile * 1.5) + 6, 28)) + 1)
            else:
                GL.glUseProgram(S.rug)
                GL.glDrawElements(GL.GL_TRIANGLES, count, GL.GL_UNSIGNED_INT, None)
            if r.sim.rim_segments > 0:
                GL.glUseProgram(S.rim)
                r.sim.rim_buf.ssbo(4)
                GL.glDrawArrays(GL.GL_TRIANGLES, 0, r.sim.rim_segments * 6)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glUseProgram(0)
        GL.glBindVertexArray(0)
        for unit in range(4):
            GL.glBindSampler(unit, 0)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glClipControl(GL.GL_LOWER_LEFT, GL.GL_NEGATIVE_ONE_TO_ONE)
