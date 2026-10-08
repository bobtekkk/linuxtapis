// Rug rendering, ported 1:1 from Tapis' Metal render source.
// The loader prepends "#version 460" and one "#define <STAGE>" to pick a
// vertex or fragment entry point.

layout(std140, binding = 0) uniform UniformsBlock {
    vec2  origin;      // world-space top-left of the view (points)
    vec2  viewSize;    // view size (points)
    float camD;        // camera distance (points) for the mild perspective
    float depthBias;
    vec2  shadowDir;   // ground offset of a shadow per point of height
    vec2  drawSize;    // drawable size (pixels)
    float shadowStrength;
    float upad0, upad1, upad2;
} u;

// Per-rug fur settings (pile length 0 = flat-woven).
layout(std140, binding = 3) uniform FurBlock {
    float pile;      // pile length, points
    float shells;    // number of shells above the base
    float sizeX;     // rug size, points (for strand density and ragged edges)
    float sizeY;
    float tile;      // points per repeat of the strand texture
    float fpad0, fpad1, fpad2;
} f;

// Rim settings (the rim pass).
layout(std140, binding = 4) uniform RimBlock {
    float thickness; float fur; float rpad1, rpad2;
} rimU;

layout(std430, binding = 0) readonly buffer VertsB { float verts[]; };   // 9 floats per vertex
layout(std430, binding = 4) readonly buffer RimSegB { uvec4 rimSeg[]; };

vec3 vPos(uint i) { uint o = i * 9u; return vec3(verts[o], verts[o + 1u], verts[o + 2u]); }
vec3 vNrm(uint i) { uint o = i * 9u; return vec3(verts[o + 3u], verts[o + 4u], verts[o + 5u]); }
vec2 vUV(uint i)  { uint o = i * 9u; return vec2(verts[o + 6u], verts[o + 7u]); }
float vGround(uint i) { return verts[i * 9u + 8u]; }

vec4 projectWorld(vec3 p) {
    vec2 c = u.origin + u.viewSize * 0.5;
    float w = max((u.camD - p.z) / u.camD, 0.05);
    vec2 rel = (p.xy - c) / (u.viewSize * 0.5);
    float depth = clamp(0.5 - p.z / 4000.0 - u.depthBias, 0.0, 1.0);
    return vec4(rel.x, -rel.y, depth * w, w);
}

layout(binding = 0) uniform sampler2D tex;
layout(binding = 1) uniform sampler2D shadowTex;
layout(binding = 2) uniform sampler2D strandsR;   // strands, repeat sampler
layout(binding = 3) uniform sampler2D strandsL;   // strands, smooth repeat sampler

// ---------------------------------------------------------------- rug

#if defined(RUG_VS) || defined(FUR_VS) || defined(RIM_VS)
out vec3 vNrmO; out vec2 vUVO; out float vZ; out float vH;
#endif
#if defined(RUG_FS) || defined(FUR_FS) || defined(RIM_FS)
in vec3 vNrmO; in vec2 vUVO; in float vZ; in float vH;
out vec4 fragColor;
#endif

#ifdef RUG_VS
void main() {
    uint vid = uint(gl_VertexID);
    vec3 p = vPos(vid);
    gl_Position = projectWorld(p);
    vNrmO = vNrm(vid);
    vUVO = vUV(vid);
    vZ = p.z;
    vH = 0.0;
}
#endif

#if defined(RUG_FS) || defined(FUR_FS)
vec3 shadeRug(vec3 col, vec3 nIn, float z, vec4 fragPos, out bool back) {
    vec3 n = normalize(nIn);
    back = n.z < 0.0;
    if (back) {
        n = -n;
        float l = dot(col, vec3(0.3, 0.55, 0.15));
        col = mix(vec3(l), col, 0.7) * 0.82 + vec3(0.04, 0.03, 0.02);
    }
    vec3 L = normalize(vec3(-0.35, -0.45, 0.82));
    float ndl = max(dot(n, L), 0.0);
    float shade = (0.42 + 0.58 * ndl) / (0.42 + 0.58 * L.z);
    float sheen = pow(ndl, 12.0) * 0.06 * (1.0 - n.z);
    vec2 sh = texture(shadowTex, fragPos.xy / u.drawSize).rg;
    float casterH = sh.g / max(sh.r, 1e-3);
    // contact shadow: strongest just under a flap resting on the layer below
    float occ = sh.r * smoothstep(1.0, 14.0, casterH - z);
    shade *= 1.0 - 0.55 * occ;
    return col * shade + sheen;
}
#endif

// ---------------------------------------------------------------- fur (shells)
// A shaggy rug is drawn as the base surface plus `shells` copies lifted along
// the normal. Each shell keeps only the texels where a strand is still present
// at that height, so together they read as a deep pile.

#ifdef FUR_VS
void main() {
    uint vid = uint(gl_VertexID);
    float h = float(gl_InstanceID) / f.shells;
    vec3 n = normalize(vNrm(vid));
    vec2 uv = vUV(vid);
    // shells sag a little toward the tips (a fluffy pile isn't rigidly upright)
    vec3 p = vPos(vid) + n * (f.pile * h * (1.0 - 0.25 * h));
    gl_Position = projectWorld(p);
    vNrmO = n;
    vUVO = uv;
    vZ = p.z;
    vH = h;
}
#endif

#ifdef FUR_FS
void main() {
    float h = vH;
    vec2 pt = vUVO * vec2(f.sizeX, f.sizeY);
    // Each tuft leans its own way: shift the strand lookup with height.
    float ang = texture(strandsL, pt / (f.tile * 3.0)).a * 6.2831853;
    vec2 lean = vec2(cos(ang), sin(ang)) * (f.pile * 0.7 * h * h);
    vec4 st = texture(strandsR, (pt - lean) / f.tile);
    // strand present at this height? (thinner toward the tip)
    if (h > 0.0 && (st.r < h || st.g > 1.0 - h * 0.7)) discard;
    // Ragged, fuzzy outline on the fur side. The backing stays solid to the edge.
    bool underside = vNrmO.z < 0.0;
    float edge = min(min(pt.x, f.sizeX - pt.x), min(pt.y, f.sizeY - pt.y));
    if (!(underside && h == 0.0) && edge < f.pile * (0.25 + 0.75 * st.b) * (0.4 + h)) discard;
    vec4 c = texture(tex, vUVO);
    if (c.a < 0.5) discard;
    vec3 col = c.rgb / max(c.a, 0.001);
    bool back;
    // the backing of a shag rug is plain: no pile on the underside
    vec3 lit = shadeRug(col, vNrmO, vZ, gl_FragCoord, back);
    if (back && h > 0.0) discard;
    // dark roots, bright tips, a little per-strand variation
    float ao = back ? 1.0 : mix(0.6, 1.1, h) * (0.92 + 0.16 * st.b);
    float tipSheen = back ? 0.0 : smoothstep(0.6, 1.0, h) * 0.06;
    fragColor = vec4(lit * ao + tipSheen, 1.0);
}
#endif

// ---------------------------------------------------------------- rim
// The rug's visible thickness: a strip along the outline from the surface down
// to `thickness` below it (along the normal), clamped at desk level.

#ifdef RIM_VS
void main() {
    uint vid = uint(gl_VertexID);
    uint seg = vid / 6u, corner = vid % 6u;
    const uint which[6] = uint[6](0u, 1u, 0u, 1u, 1u, 0u);
    const uint bottom[6] = uint[6](0u, 0u, 1u, 0u, 1u, 1u);
    uvec4 s = rimSeg[seg];
    uint k = which[corner] == 0u ? s.x : s.y;
    uint kin = which[corner] == 0u ? s.z : s.w;
    vec3 top = vPos(k);
    vec3 n = normalize(vNrm(k));
    // outward = away from the inward neighbour, flattened against the surface
    vec3 o = top - vPos(kin);
    o -= n * dot(o, n);
    float ol = length(o);
    vec3 outDir = ol > 1e-4 ? o / ol : vec3(0.0);
    vec3 p = top;
    if (bottom[corner] == 1u) {
        p -= n * rimU.thickness;
        p.z = max(p.z, 0.0);
        // A rounded edge bulges outward a little, only as much as the edge is
        // actually off the desk, so a flat rug's outline is unchanged.
        float lifted = clamp(length(top - p) / rimU.thickness, 0.0, 1.0);
        p += outDir * (rimU.thickness * 0.55 * lifted);
    }
    gl_Position = projectWorld(p);
    vNrmO = ol > 1e-4 ? normalize(outDir + n * 0.35) : n;
    // sample the outermost texel so the rim matches the edge binding
    vUVO = clamp(vUV(k), 0.002, 0.998);
    vZ = p.z;
    vH = n.z;   // which side faces up
}
#endif

#ifdef RIM_FS
void main() {
    // On a shag with the fur side up, the ragged pile already shows the thickness.
    if (rimU.fur > 0.5 && vH > 0.0) discard;
    vec4 c = texture(tex, vUVO);
    if (c.a < 0.5) discard;     // no slab under a fringe
    vec3 col = c.rgb / max(c.a, 0.001) * 0.72;   // the cut side reads darker
    vec3 n = normalize(vNrmO);
    vec3 L = normalize(vec3(-0.35, -0.45, 0.82));
    float shade = 0.5 + 0.5 * max(dot(n, L), 0.0);
    vec2 sh = texture(shadowTex, gl_FragCoord.xy / u.drawSize).rg;
    float casterH = sh.g / max(sh.r, 1e-3);
    shade *= 1.0 - 0.55 * sh.r * smoothstep(1.0, 14.0, casterH - vZ);
    fragColor = vec4(col * shade, 1.0);
}
#endif

#ifdef RUG_FS
void main() {
    vec4 c = texture(tex, vUVO);
    if (c.a < 0.5) discard;
    vec3 col = c.rgb / max(c.a, 0.001);
    bool back;
    fragColor = vec4(shadeRug(col, vNrmO, vZ, gl_FragCoord, back), 1.0);
}
#endif

// ---------------------------------------------------------------- shadow

#ifdef SHADOW_VS
out vec2 sUV; out float sZ;
void main() {
    uint vid = uint(gl_VertexID);
    vec3 p = vPos(vid) + vNrm(vid) * (f.pile * 0.3);
    // shadow length = height above what's directly underneath
    vec2 g = p.xy + u.shadowDir * (max(p.z - vGround(vid), 0.0) + 4.0);
    gl_Position = projectWorld(vec3(g, 0.0));
    sUV = vUV(vid);
    sZ = p.z;
}
#endif

#ifdef SHADOW_FS
in vec2 sUV; in float sZ;
out vec4 fragColor;
void main() {
    if (texture(tex, sUV).a < 0.5) discard;
    fragColor = vec4(1.0, sZ, 0.0, 0.0);
}
#endif

// ---------------------------------------------------------------- full-screen passes

#ifdef FULLSCREEN_VS
out vec2 fsUV;
void main() {
    uint vid = uint(gl_VertexID);
    vec2 p = vec2(float((vid << 1u) & 2u), float(vid & 2u));
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
    fsUV = p;   // GL textures are bottom-up, so no flip (Metal flips here)
}
#endif

#ifdef BLUR_FS
in vec2 fsUV;
out vec4 fragColor;
uniform vec2 dir;
void main() {
    const float w[5] = float[5](0.227027, 0.1945946, 0.1216216, 0.054054, 0.016216);
    vec4 acc = texture(shadowTex, fsUV) * w[0];
    for (int i = 1; i < 5; i++) {
        acc += texture(shadowTex, fsUV + dir * float(i)) * w[i];
        acc += texture(shadowTex, fsUV - dir * float(i)) * w[i];
    }
    fragColor = acc;
}
#endif

#ifdef GROUND_FS
in vec2 fsUV;
out vec4 fragColor;
void main() {
    float a = texture(shadowTex, fsUV).r * u.shadowStrength;
    fragColor = vec4(0.0, 0.0, 0.0, a);
}
#endif
