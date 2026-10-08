// Cloth simulation kernels, ported 1:1 from Tapis' Metal compute source.
// The loader prepends "#version 460" and "#define K_<kernel>" to build one
// compute program per kernel. Buffer binding numbers match the Metal indices.

#define FLAG_GRAB 1u
#define FLAG_GUIDE 2u
#define FLAG_FLATTEN 4u
#define FLAG_PLACE 8u      // first frame of a new rug: lay it onto the surface
#define FIXED 1024.0       // fixed-point scale for atomic position deltas

layout(std140, binding = 0) uniform SimParamsBlock {
    uint n; uint cols; uint rows; uint triCount;
    float cellSize; float thickness; float g; float airDamp;
    float contactDamp; uint flags; float grabStiffness; float grabHeadroom;
    float grabX; float grabY; float grabZ; float guideStiffness;
    float stickLimit; float layerGrip; float maxStretch; uint hashSize;
    uint maxPerBucket; uint iconCount; float bumpHeight; float bumpHalfX;
    float bumpHalfY; float bumpRadius; float bumpSoft; float bumpOffX;
    float bumpOffY; float collisionRelax; int ring; float maxPush;
} P;

layout(std140, binding = 1) uniform SupportInfoBlock {
    float originX; float originY; float cell; uint dimX;
    uint dimY; uint triCount; float lowerThickness; uint pad1;
} S;

struct Constraint { uint a; uint b; float len; float k; };

uniform uvec2 batch;   // (first constraint, count)
uniform uvec4 R;       // (renderCols, renderRows, factor, renderCount)
uniform uint anchorIdx;   // tether: the grabbed particle
uniform vec2 restSize;    // tether: rug size (rest layout)
uniform float slack;      // tether: allowed stretch (fraction)
uniform float slide = 0.5; // friction: share of a hard tug that slides (Mac: 0.5)
uniform int edgeBand = 0;  // (Linux port) "corners fold up": edge rows that prefer to lie on top

#ifdef USE_Pos
layout(std430, binding = 0) buffer PosB { vec4 pos[]; };
#endif
#ifdef USE_Prev
layout(std430, binding = 1) buffer PrevB { vec4 prev[]; };
#endif
#ifdef USE_Ground
layout(std430, binding = 2) buffer GroundB { float ground[]; };
#endif
#ifdef USE_Contact
layout(std430, binding = 3) buffer ContactB { uint contact[]; };
#endif
#ifdef USE_WasContact
layout(std430, binding = 4) buffer WasContactB { uint wasContact[]; };
#endif
#ifdef USE_Anchor
layout(std430, binding = 5) buffer AnchorB { vec2 anchor[]; };
#endif
#ifdef USE_Pinned
layout(std430, binding = 6) buffer PinnedB { uint pinned[]; };
#endif
#ifdef USE_GrabOff
layout(std430, binding = 7) buffer GrabOffB { vec4 grabOff[]; };
#endif
#ifdef USE_Guide
layout(std430, binding = 8) buffer GuideB { vec2 guideTarget[]; };
#endif
#ifdef USE_Flatten
layout(std430, binding = 9) buffer FlattenB { vec2 flattenTarget[]; };
#endif
#ifdef USE_Delta
layout(std430, binding = 10) buffer DeltaB { int delta[]; };
#endif
#ifdef USE_Cons
layout(std430, binding = 11) buffer ConsB { Constraint cons[]; };
#endif
#ifdef USE_Index
layout(std430, binding = 12) buffer IndexB { uint indices[]; };
#endif
#ifdef USE_BucketCount
layout(std430, binding = 13) buffer BucketCountB { uint bucketCount[]; };
#endif
#ifdef USE_BucketItems
layout(std430, binding = 14) buffer BucketItemsB { int bucketItems[]; };
#endif
#ifdef USE_FrameStart
layout(std430, binding = 17) buffer FrameStartB { vec4 frameStart[]; };
#endif
#ifdef USE_ContactAtStart
layout(std430, binding = 18) buffer ContactAtStartB { uint contactAtStart[]; };
#endif
#ifdef USE_Stats
layout(std430, binding = 19) buffer StatsB { uint stats[]; };
#endif
#ifdef USE_Icons
layout(std430, binding = 20) buffer IconsB { vec2 icons[]; };
#endif
#ifdef USE_VertexOut
layout(std430, binding = 21) buffer VertexOutB { float vertexOut[]; };
#endif
#ifdef USE_BucketStart
layout(std430, binding = 23) buffer BucketStartB { uint bucketStart[]; };
#endif
#ifdef USE_Support
layout(std430, binding = 24) buffer SupportB { uint support[]; };
#endif
#ifdef USE_UV
layout(std430, binding = 22) buffer UVB { vec2 uv[]; };
#endif
// rasterSupport reads the lower rug's mesh
#ifdef USE_LowerPos
layout(std430, binding = 26) buffer LowerPosB { vec4 lowerPos[]; };
#endif
#ifdef USE_LowerIdx
layout(std430, binding = 27) buffer LowerIdxB { uint lowerIdx[]; };
#endif

float min3(float a, float b, float c) { return min(a, min(b, c)); }
float max3(float a, float b, float c) { return max(a, max(b, c)); }

uint hashKey(int ix, int iy, uint size) {
    return ((uint(ix) * 73856093u) ^ (uint(iy) * 19349663u)) & (size - 1u);
}

#if defined(K_scanBuckets)
layout(local_size_x = 1024) in;
#else
layout(local_size_x = 64) in;
#endif

// ---- Support from rugs lying underneath: their top surface is rasterised
// into a height map (stored as float bits + 1 so 0 means "no rug here");
// this rug then rests one thickness above it, like on the desk.

#ifdef K_clearSupport
void main() {
    uint i = gl_GlobalInvocationID.x;
    if (i >= S.dimX * S.dimY) return;
    support[i] = 0u;
}
#endif

#ifdef K_rasterSupport
void main() {
    uint t = gl_GlobalInvocationID.x;
    if (t >= S.triCount) return;
    vec3 a = lowerPos[lowerIdx[3u * t]].xyz, b = lowerPos[lowerIdx[3u * t + 1u]].xyz, c = lowerPos[lowerIdx[3u * t + 2u]].xyz;
    vec2 o = vec2(S.originX, S.originY);
    int x0 = max(int(floor((min3(a.x, b.x, c.x) - o.x) / S.cell)), 0);
    int x1 = min(int(ceil((max3(a.x, b.x, c.x) - o.x) / S.cell)), int(S.dimX) - 1);
    int y0 = max(int(floor((min3(a.y, b.y, c.y) - o.y) / S.cell)), 0);
    int y1 = min(int(ceil((max3(a.y, b.y, c.y) - o.y) / S.cell)), int(S.dimY) - 1);
    if (x1 < x0 || y1 < y0 || (x1 - x0) * (y1 - y0) > 4096) return;
    vec2 e0 = b.xy - a.xy, e1 = c.xy - a.xy;
    float den = e0.x * e1.y - e0.y * e1.x;
    if (abs(den) < 1e-6) return;
    for (int y = y0; y <= y1; y++) {
        for (int x = x0; x <= x1; x++) {
            vec2 q = o + (vec2(x, y) + 0.5) * S.cell - a.xy;
            float v = (q.x * e1.y - q.y * e1.x) / den;
            float w = (e0.x * q.y - e0.y * q.x) / den;
            if (v < -0.05 || w < -0.05 || v + w > 1.05) continue;
            float z = max(a.z + (b.z - a.z) * v + (c.z - a.z) * w, 0.0);
            atomicMax(support[y * int(S.dimX) + x], floatBitsToUint(z + S.lowerThickness + 1.0));
        }
    }
}
#endif

// Desk height under a point: each icon is a soft rounded bump (piles add up),
// and any rug lying underneath.
#ifdef K_frameBegin
void main() {
    uint k = gl_GlobalInvocationID.x;
    if (k >= P.n) return;
    vec3 p = pos[k].xyz;
    float h = 0.0;
    vec2 half2 = vec2(P.bumpHalfX, P.bumpHalfY);
    vec2 off = vec2(P.bumpOffX, P.bumpOffY);
    for (uint i = 0u; i < P.iconCount; i++) {
        vec2 q = abs(p.xy - (icons[i] + off)) - half2 + P.bumpRadius;
        float d = length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - P.bumpRadius;
        if (d < P.bumpSoft) {
            float t = clamp((P.bumpSoft - d) / (P.bumpSoft * 1.8), 0.0, 1.0);
            h += P.bumpHeight * t * t * (3.0 - 2.0 * t);
        }
    }
    if (S.dimX > 0u) {
        int x = int(floor((p.x - S.originX) / S.cell)), y = int(floor((p.y - S.originY) / S.cell));
        if (x >= 0 && y >= 0 && x < int(S.dimX) && y < int(S.dimY)) {
            uint v = support[y * int(S.dimX) + x];
            // rests on the lower rug's top (its surface + its own thickness)
            if (v > 0u) h = max(h, uintBitsToFloat(v) - 1.0);
        }
    }
    ground[k] = h;
    if ((P.flags & FLAG_PLACE) != 0u) {
        // a new rug starts already draped over the desk, icons and rugs below
        p.z = max(p.z, h);
        pos[k] = vec4(p, 0.0);
        prev[k] = vec4(p, 0.0);
    }
    frameStart[k] = vec4(p, 0.0);
    contactAtStart[k] = contact[k];
}
#endif

// Verlet integration (+ the "smooth out" pull), and reset contact flags.
#ifdef K_integrate
void main() {
    uint k = gl_GlobalInvocationID.x;
    if (k >= P.n) return;
    vec3 p = pos[k].xyz;
    float damp = contact[k] != 0u ? P.contactDamp : P.airDamp;
    vec3 v = (p - prev[k].xyz) * damp;
    prev[k] = vec4(p, 0.0);
    p += v;
    p.z -= P.g;
    if ((P.flags & FLAG_FLATTEN) != 0u && pinned[k] == 0u) {
        vec2 d = flattenTarget[k] - p.xy;
        p.xy += d * 0.12;
        float lift = min(length(d) * 0.25, 40.0);   // folded parts unfold over the top
        if (p.z < ground[k] + lift) p.z += (ground[k] + lift - p.z) * 0.2;
    }
    pos[k] = vec4(p, 0.0);
    contact[k] = 0u;
}
#endif

#ifdef K_clearBuckets
void main() {
    uint i = gl_GlobalInvocationID.x;
    if (i >= P.hashSize) return;
    bucketCount[i] = 0u;
}
#endif

#if defined(K_countTriangles) || defined(K_fillTriangles)
bool triangleCells(uint t, out ivec4 r) {
    vec3 a = pos[indices[3u * t]].xyz, b = pos[indices[3u * t + 1u]].xyz, c = pos[indices[3u * t + 2u]].xyz;
    float hc = P.cellSize, th = P.thickness;
    r = ivec4(int(floor((min3(a.x, b.x, c.x) - th) / hc)), int(floor((min3(a.y, b.y, c.y) - th) / hc)),
              int(floor((max3(a.x, b.x, c.x) + th) / hc)), int(floor((max3(a.y, b.y, c.y) + th) / hc)));
    return r.z - r.x <= 6 && r.w - r.y <= 6;   // skip degenerate / exploded triangles
}
#endif

// Pass 1: count how many triangles touch each hash cell.
#ifdef K_countTriangles
void main() {
    uint t = gl_GlobalInvocationID.x;
    if (t >= P.triCount) return;
    ivec4 r;
    if (!triangleCells(t, r)) return;
    for (int cy = r.y; cy <= r.w; cy++)
        for (int cx = r.x; cx <= r.z; cx++)
            atomicAdd(bucketCount[hashKey(cx, cy, P.hashSize)], 1u);
}
#endif

// Pass 2: exclusive prefix sum of the counts (one workgroup of 1024 threads);
// resets the counts so pass 3 can use them as cursors.
#ifdef K_scanBuckets
shared uint sums[1024];
void main() {
    uint tid = gl_LocalInvocationID.x;
    uint per = P.hashSize / 1024u;
    uint base = tid * per;
    uint local_ = 0u;
    for (uint i = 0u; i < per; i++) local_ += bucketCount[base + i];
    sums[tid] = local_;
    barrier();
    for (uint off = 1u; off < 1024u; off <<= 1u) {
        uint v = tid >= off ? sums[tid - off] : 0u;
        barrier();
        sums[tid] += v;
        barrier();
    }
    uint running = sums[tid] - local_;   // exclusive
    for (uint i = 0u; i < per; i++) {
        bucketStart[base + i] = running;
        running += bucketCount[base + i];
        bucketCount[base + i] = 0u;
    }
    if (tid == 1023u) bucketStart[P.hashSize] = running;
}
#endif

// Pass 3: write each triangle into its cells' slots.
#ifdef K_fillTriangles
void main() {
    uint t = gl_GlobalInvocationID.x;
    if (t >= P.triCount) return;
    ivec4 r;
    if (!triangleCells(t, r)) return;
    for (int cy = r.y; cy <= r.w; cy++) {
        for (int cx = r.x; cx <= r.z; cx++) {
            uint key = hashKey(cx, cy, P.hashSize);
            uint slot = bucketStart[key] + atomicAdd(bucketCount[key], 1u);
            if (slot < P.maxPerBucket) bucketItems[slot] = int(t);   // maxPerBucket = item capacity
        }
    }
}
#endif

// One colour of distance constraints: no two in a batch share a particle,
// so they can all be solved at once (Gauss-Seidel by colour).
#ifdef K_solveBatch
void main() {
    uint i = gl_GlobalInvocationID.x;
    if (i >= batch.y) return;
    Constraint c = cons[batch.x + i];
    vec3 d = pos[c.b].xyz - pos[c.a].xyz;
    float len = length(d);
    if (len < 1e-6) return;
    float s = (len - c.len) / (len * 2.0) * c.k;
    pos[c.a].xyz += d * s;
    pos[c.b].xyz -= d * s;
}
#endif

// Strain limiting on one colour of structural links.
#ifdef K_limitBatch
void main() {
    uint i = gl_GlobalInvocationID.x;
    if (i >= batch.y) return;
    Constraint c = cons[batch.x + i];
    vec3 d = pos[c.b].xyz - pos[c.a].xyz;
    float len = length(d);
    float maxLen = c.len * P.maxStretch;
    if (len <= maxLen) return;
    vec3 corr = d * ((len - maxLen) / len * 0.5);
    pos[c.a].xyz += corr;
    pos[c.b].xyz -= corr;
}
#endif

// The cursor's spring on the grabbed patch, and guided move/rotate/resize.
#ifdef K_applyTargets
void main() {
    uint k = gl_GlobalInvocationID.x;
    if (k >= P.n) return;
    vec3 p = pos[k].xyz;
    if ((P.flags & FLAG_GRAB) != 0u && pinned[k] != 0u) {
        float gs = P.grabStiffness;
        vec3 t = vec3(P.grabX, P.grabY, P.grabZ) + grabOff[k].xyz;
        p.xy += (t.xy - p.xy) * gs;
        float minZ = ground[k] + P.grabZ;
        float maxZ = minZ + P.grabHeadroom;
        if (p.z < minZ) p.z += (minZ - p.z) * gs;
        if (p.z > maxZ) p.z += (maxZ - p.z) * gs;
    }
    if ((P.flags & FLAG_GUIDE) != 0u) {
        p.xy += (guideTarget[k] - p.xy) * P.guideStiffness;
    }
    pos[k] = vec4(p, 0.0);
}
#endif

#ifdef K_collide
void addDelta(uint i, vec3 v) {
    atomicAdd(delta[4u * i + 0u], int(round(v.x * FIXED)));
    atomicAdd(delta[4u * i + 1u], int(round(v.y * FIXED)));
    atomicAdd(delta[4u * i + 2u], int(round(v.z * FIXED)));
    atomicAdd(delta[4u * i + 3u], 1);
}

// Point-vs-triangle self-collision: whichever side of a nearby triangle a
// particle was on at the start of the substep, keep it `thickness` on that side.
void main() {
    uint v = gl_GlobalInvocationID.x;
    if (v >= P.n) return;
    vec3 p = pos[v].xyz;
    float th = P.thickness, hc = P.cellSize;
    int nc = int(P.cols), cellsPerRow = nc - 1;
    int vi = int(v) % nc, vj = int(v) / nc;
    uint key = hashKey(int(floor(p.x / hc)), int(floor(p.y / hc)), P.hashSize);
    uint end_ = min(bucketStart[key + 1u], P.maxPerBucket);
    for (uint e = bucketStart[key]; e < end_; e++) {
        int tri = bucketItems[e];
        int cell = tri / 2;
        int ci = cell % cellsPerRow, cj = cell / cellsPerRow;
        // Skip the particle's own triangles and the ring of weave neighbours a
        // thickness away: the same sheet bending isn't a collision.
        int Rr = P.ring;
        if (vi >= ci - Rr && vi <= ci + 1 + Rr && vj >= cj - Rr && vj <= cj + 1 + Rr) continue;
        uint ia = indices[3 * tri], ib = indices[3 * tri + 1], ic = indices[3 * tri + 2];
        vec3 a = pos[ia].xyz, b = pos[ib].xyz, c = pos[ic].xyz;
        vec3 e0 = b - a, e1 = c - a;
        vec3 cr = cross(e0, e1);
        float crl = length(cr);
        if (crl < 1e-6) continue;
        vec3 nn = cr / crl;
        float d = dot(p - a, nn);
        if (abs(d) > th * 2.5) continue;
        vec3 v2 = (p - nn * d) - a;
        float d00 = dot(e0, e0), d01 = dot(e0, e1), d11 = dot(e1, e1);
        float d20 = dot(v2, e0), d21 = dot(v2, e1);
        float den = d00 * d11 - d01 * d01;
        if (abs(den) < 1e-9) continue;
        float bv = (d11 * d20 - d01 * d21) / den;
        float bw = (d00 * d21 - d01 * d20) / den;
        float bu = 1.0 - bv - bw;
        if (bu < -0.02 || bv < -0.02 || bw < -0.02) continue;
        vec3 qa = prev[ia].xyz;
        vec3 qn = cross(prev[ib].xyz - qa, prev[ic].xyz - qa);
        float qnl = length(qn);
        float dPrev = qnl > 1e-6 ? dot(prev[v].xyz - qa, qn / qnl) : d;
        if (abs(dPrev) < 0.05) dPrev = d;
        float side = dPrev >= 0.0 ? 1.0 : -1.0;
        // "Corners fold up": where an edge meets the rest of the rug and it isn't
        // already clearly under, the edge goes on top instead of tucking under.
        if (edgeBand > 0 && abs(dPrev) < th * 1.5) {
            int ev = min(min(vi, nc - 1 - vi), min(vj, int(P.rows) - 1 - vj));
            int et = min(min(ci, nc - 2 - ci), min(cj, int(P.rows) - 2 - cj));
            float up = nn.z >= 0.0 ? 1.0 : -1.0;
            if (ev < edgeBand && ev < et) side = up;          // edge point over interior cloth
            else if (et < edgeBand && et < ev) side = -up;    // interior point under an edge
        }
        float sd = d * side;
        if (sd >= th) continue;
        // capped, so one deep overlap can't fling the cloth apart
        vec3 push = nn * (side * min((th - sd) * 0.5, P.maxPush));
        addDelta(v, push);
        float kk = 1.0 / (bu * bu + bv * bv + bw * bw);
        addDelta(ia, -push * (bu * kk));
        addDelta(ib, -push * (bv * kk));
        addDelta(ic, -push * (bw * kk));
        float up = side * nn.z;
        if (up > 0.3) contact[v] = 1u;
        if (up < -0.3) { contact[ia] = 1u; contact[ib] = 1u; contact[ic] = 1u; }
    }
}
#endif

// (Linux port) Long-range tethers: a rug barely stretches, so no point may end up
// farther from the grabbed point than it is on the flat rug. Pulling a corner
// drags the whole rug along instead of stretching the cloth.
#ifdef K_tether
void main() {
    uint k = gl_GlobalInvocationID.x;
    if (k >= P.n || pinned[k] != 0u) return;
    vec3 a = pos[anchorIdx].xyz;
    vec3 p = pos[k].xyz;
    float rest = length((uv[k] - uv[anchorIdx]) * restSize) * (1.0 + slack);
    vec3 d = p - a;
    float len = length(d);
    if (len > rest && len > 1e-6) pos[k] = vec4(a + d * (rest / len), 0.0);
}
#endif

// (Linux port) "Corners fold up": the outermost rows may curl up but not roll
// down: an edge point stays no lower than its neighbour two rows in, minus a
// gentle slope. Lifted edges then turn up like a bound rug, never tuck under.
#ifdef K_edgeLift
void main() {
    uint k = gl_GlobalInvocationID.x;
    if (k >= P.n || pinned[k] != 0u) return;
    int nc = int(P.cols), nr = int(P.rows);
    int i = int(k) % nc, j = int(k) / nc;
    int sx = i < edgeBand ? 1 : (i > nc - 1 - edgeBand ? -1 : 0);
    int sy = j < edgeBand ? 1 : (j > nr - 1 - edgeBand ? -1 : 0);
    if (sx == 0 && sy == 0) return;
    int ii = clamp(i + 2 * sx, 0, nc - 1), jj = clamp(j + 2 * sy, 0, nr - 1);
    vec3 p = pos[k].xyz;
    vec3 q = pos[jj * nc + ii].xyz;
    float allowed = 0.3 * length(q.xy - p.xy);
    float floorZ = q.z - allowed;
    if (p.z < floorZ) p.z += (floorZ - p.z) * 0.5;
    pos[k] = vec4(p, 0.0);
}
#endif

// Applies the (over-relaxed) average of the collision pushes, then the desk.
#ifdef K_applyDeltas
void main() {
    uint k = gl_GlobalInvocationID.x;
    if (k >= P.n) return;
    int cnt = atomicExchange(delta[4u * k + 3u], 0);
    vec3 p = pos[k].xyz;
    if (cnt > 0) {
        int dx = atomicExchange(delta[4u * k + 0u], 0);
        int dy = atomicExchange(delta[4u * k + 1u], 0);
        int dz = atomicExchange(delta[4u * k + 2u], 0);
        p += vec3(dx, dy, dz) / FIXED * min(1.0, P.collisionRelax / float(cnt));
    }
    if (p.z <= ground[k]) { p.z = ground[k]; contact[k] = 1u; }
    pos[k] = vec4(p, 0.0);
}
#endif

// End of substep: desk, contact, stick-slip friction, and stillness tracking.
#ifdef K_friction
void main() {
    uint k = gl_GlobalInvocationID.x;
    if (k >= P.n) return;
    vec3 p = pos[k].xyz;
    float gnd = ground[k];
    if (p.z <= gnd) p.z = gnd;
    if (p.z <= gnd + 0.3) contact[k] = 1u;
    bool guided = (P.flags & FLAG_GUIDE) != 0u;
    if (contact[k] == 0u || pinned[k] != 0u || guided) {
        wasContact[k] = 0u;
    } else if (p.z > gnd + 0.3) {
        // wool on wool: slides easily, but tiny creeping motions stop
        wasContact[k] = 0u;
        vec2 d = p.xy - prev[k].xy;
        if (dot(d, d) < P.layerGrip * P.layerGrip) p.xy = prev[k].xy;
        else p.xy -= d * 0.25;
    } else if (wasContact[k] == 0u) {
        anchor[k] = p.xy;
        wasContact[k] = 1u;
    } else {
        // on the desk: stays where it touched down unless tugged hard
        vec2 d = p.xy - anchor[k];
        float len = length(d);
        if (len <= P.stickLimit) {
            p.xy = anchor[k];
        } else {
            vec2 slid = anchor[k] + d * ((len - P.stickLimit) * slide / len);
            p.xy = slid;
            anchor[k] = slid;
        }
    }
    // Resting on something is inelastic: when the surface underneath pushed
    // this point up, it doesn't keep that as upward speed and fly off.
    if (contact[k] != 0u && pinned[k] == 0u && p.z <= gnd + 0.3) prev[k].z = max(prev[k].z, p.z);
    pos[k] = vec4(p, 0.0);
    vec3 m = p - prev[k].xyz;
    atomicMax(stats[0], floatBitsToUint(dot(m, m)));
}
#endif

// End of frame: sound cues.
#ifdef K_frameEnd
void main() {
    uint k = gl_GlobalInvocationID.x;
    if (k >= P.n) return;
    vec3 p = pos[k].xyz;
    atomicAdd(stats[1], uint(length(p - frameStart[k].xyz) * 1000.0));
    if (contact[k] != 0u && contactAtStart[k] == 0u && pinned[k] == 0u) {
        float fall = frameStart[k].z - p.z;
        if (fall > 0.5) atomicAdd(stats[2], uint(fall * 1000.0));
    }
}
#endif

// ---- Render mesh: a finer, smooth surface interpolated from the simulated
// grid (Catmull-Rom in both directions), so creases and folded edges draw as
// curves instead of showing the simulation's triangles.
#ifdef K_refine
vec3 catmull(vec3 p0, vec3 p1, vec3 p2, vec3 p3, float t) {
    float t2 = t * t, t3 = t2 * t;
    return 0.5 * (2.0 * p1 + (p2 - p0) * t + (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2
                  + (3.0 * p1 - p0 - 3.0 * p2 + p3) * t3);
}

void main() {
    uint r = gl_GlobalInvocationID.x;
    if (r >= R.w) return;
    int nc = int(P.cols), nr = int(P.rows);
    float gx = float(r % R.x) / float(R.z), gy = float(r / R.x) / float(R.z);
    int i1 = min(int(gx), nc - 2), j1 = min(int(gy), nr - 2);
    float tx = gx - float(i1), ty = gy - float(j1);
    vec3 col[4];
    for (int m = 0; m < 4; m++) {
        int j = clamp(j1 - 1 + m, 0, nr - 1) * nc;
        col[m] = catmull(pos[j + max(i1 - 1, 0)].xyz, pos[j + i1].xyz, pos[j + i1 + 1].xyz, pos[j + min(i1 + 2, nc - 1)].xyz, tx);
    }
    vec3 p = catmull(col[0], col[1], col[2], col[3], ty);
    float g0 = mix(ground[j1 * nc + i1], ground[j1 * nc + i1 + 1], tx);
    float g1 = mix(ground[(j1 + 1) * nc + i1], ground[(j1 + 1) * nc + i1 + 1], tx);
    uint o = r * 9u;
    vertexOut[o + 0u] = p.x; vertexOut[o + 1u] = p.y; vertexOut[o + 2u] = p.z;
    vertexOut[o + 6u] = gx / float(nc - 1); vertexOut[o + 7u] = gy / float(nr - 1);
    vertexOut[o + 8u] = mix(g0, g1, ty);   // what this point rests on (shadow length)
}
#endif

#ifdef K_refineNormals
vec3 P3(int ii, int jj, int rc) {
    uint o = uint(jj * rc + ii) * 9u;
    return vec3(vertexOut[o], vertexOut[o + 1u], vertexOut[o + 2u]);
}

void main() {
    uint r = gl_GlobalInvocationID.x;
    if (r >= R.w) return;
    int rc = int(R.x), rr = int(R.y);
    int i = int(r) % rc, j = int(r) / rc;
    vec3 du = P3(min(i + 1, rc - 1), j, rc) - P3(max(i - 1, 0), j, rc);
    vec3 dv = P3(i, min(j + 1, rr - 1), rc) - P3(i, max(j - 1, 0), rc);
    vec3 n = cross(du, dv);
    float l = length(n);
    n = l > 1e-6 ? n / l : vec3(0.0, 0.0, 1.0);
    uint o = r * 9u;
    vertexOut[o + 3u] = n.x; vertexOut[o + 4u] = n.y; vertexOut[o + 5u] = n.z;
}
#endif
