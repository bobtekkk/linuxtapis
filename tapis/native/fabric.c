/* Fabric sounds: a muffled wool rustle while a rug moves and a soft thump
 * when a fold lands. Same synthesis as the Mac app (all float32). */
#include <math.h>
#include <stdint.h>

#define TWO_PI 6.28319f

typedef struct {
    /* written by the app every frame */
    float rustle, thump, softness;
    /* synthesis state */
    float sr;
    uint32_t rng;
    float level, brown, hpY, hpX, lp1, lp2, lp3;
    int gustCounter;
    float gustTarget, gust, hiss, air;
    float thPhase, thNoise, thEnv;
    float aLevel, aGust, thDecay, hpP, aThN, aHiss, thInc;
    int gustPeriod;
} Fabric;

void fabric_init(Fabric *f, float sr) {
    if (sr <= 0) sr = 48000;
    *f = (Fabric){0};
    f->sr = sr;
    f->rng = 0x9E3779B9u;
    f->aLevel = 1 - expf(-1 / (0.05f * sr));
    f->aGust = 1 - expf(-1 / (0.03f * sr));
    f->gustPeriod = (int)(sr / 14);
    f->thDecay = expf(-1 / (0.14f * sr));
    f->hpP = expf(-1759.29f / sr);
    f->aThN = 1 - expf(-1130.97f / sr);
    f->aHiss = 1 - expf(-13823.0f / sr);
    f->thInc = 58 * TWO_PI / sr;
}

static float white(Fabric *f) {
    uint32_t r = f->rng;
    r ^= r << 13; r ^= r >> 17; r ^= r << 5;
    f->rng = r;
    return ((float)r / 4294967296.0f) * 2 - 1;
}

/* Fills n interleaved stereo frames. */
void fabric_render(Fabric *f, float *out, int n) {
    float target = f->rustle;
    if (f->thump > 0) {
        if (f->thump > f->thEnv) f->thEnv = f->thump;
        f->thump = 0;
    }
    float soft = f->softness;
    float fc = (f->level * 2200 + 1100) * (1 - 0.55f * soft);
    float aLP = 1 - expf(-TWO_PI * fc / f->sr);
    float thGain = 1 - 0.5f * soft;
    for (int i = 0; i < n; i++) {
        f->level += f->aLevel * (target - f->level);
        float w = white(f);
        f->brown = f->brown * 0.985f + w * 0.15f;
        f->hpY = f->hpP * (f->hpY + f->brown - f->hpX);
        f->hpX = f->brown;
        f->lp1 += aLP * (f->hpY - f->lp1);
        f->lp2 += aLP * (f->lp1 - f->lp2);
        f->lp3 += aLP * (f->lp2 - f->lp3);
        if (--f->gustCounter <= 0) {
            f->gustTarget = fabsf(white(f)) * 0.45f + 0.55f;
            f->gustCounter = f->gustPeriod;
        }
        f->gust += f->aGust * (f->gustTarget - f->gust);
        f->hiss += f->aHiss * (w - f->hiss);
        f->air = f->hiss * 0.5f + (w - f->hiss) * 0.5f;
        float th = 0;
        if (f->thEnv > 0.0005f) {
            f->thPhase += f->thInc;
            if (f->thPhase > TWO_PI) f->thPhase -= TWO_PI;
            f->thNoise += f->aThN * (w - f->thNoise);
            th = f->thEnv * (sinf(f->thPhase) * 0.45f + f->thNoise * 2.2f) * 0.32f * thGain;
            f->thEnv *= f->thDecay;
        }
        float rustle = f->lp3 * 0.5f * f->level * f->gust + f->air * 0.01f * f->level * f->level;
        float gL = 0.9f + 0.1f * f->gust, gR = 1 - 0.1f * f->gust;
        out[2 * i] = (th + rustle * gL) * 0.8f;      /* mixer volume 0.8 */
        out[2 * i + 1] = (th + rustle * gR) * 0.8f;
    }
}

int fabric_size(void) { return (int)sizeof(Fabric); }
