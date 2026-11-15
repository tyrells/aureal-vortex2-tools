"""Aureal Vortex 2 crosstalk canceller - the chip's arithmetic, with the driver's presets.

    out_L = EQ_L(in_L * g) + XT_R(delay(EQ_R(in_R * g)))
    out_R = EQ_R(in_R * g) + XT_L(delay(EQ_L(in_L * g)))

    block(x) = 2^shift * (k * x + sum of five sections in parallel)
    section:   y[n] = b0 x[n] + b1 x[n-1] + b2 x[n-2] + a1 y[n-1] + a2 y[n-2]

Coefficients and k are 2.14 words, the input gain g a 1.15 word, the delay in samples.
The presets below are the words Aureal's Windows driver (4.06.2048) loads for the control
panel's three speaker settings (read from traces of a real SQ2500; they are the Wide,
Narrow and Pipe tables of ALSA's au88x0_xtalk.c). The same table serves both sides.

Checked against a real card: see docs/appendix-b-crosstalk-model.md. Levels here are the
block's own; the 3D path as a whole carries a further measured factor (see the appendix).
"""
import numpy as np
from scipy.signal import lfilter

# name: (EQ sections, EQ k, EQ shift, XT sections, XT k, XT shift, delay, input gain)
# a section is (a1, a2, b0, b1, b2)
PRESETS = {
    'satellites': (
        [(0xec4c, 0xdce9, 0xfdc2, 0xfeec, 0), (0x5f60, 0xcbcb, 0xfc26, 0x0305, 0), (0x340b, 0xe8f5, 0x236c, 0xe40d, 0),
         (0x76d5, 0xc78d, 0x05ac, 0xfa5b, 0), (0x7f04, 0xc0fa, 0x0263, 0xfda2, 0)], 0x269c, 1,
        [(0x55c6, 0xc97b, 0x005b, 0x0047, 0), (0x6a60, 0xca20, 0xffc6, 0x0040, 0), (0x6411, 0xd711, 0xfca1, 0x0190, 0),
         (0x77dc, 0xc79e, 0xffb8, 0x000a, 0), (0, 0, 0, 0, 0)], 0xf25e, 0, 13, 0x3fff),
    'monitors': (
        [(0x50b5, 0xd07c, 0x026d, 0xfd21, 0), (0x460f, 0xe44f, 0xf75e, 0xefa6, 0), (0x556d, 0xdcab, 0x2098, 0xf0f2, 0),
         (0x7e03, 0xc1f0, 0x007d, 0xff89, 0), (0x383e, 0xfd9d, 0xb278, 0x4547, 0)], 0x468d, 3,
        [(0x3cb2, 0xdf49, 0xf6ea, 0x095b, 0), (0x6777, 0xc915, 0xfeaf, 0x00b1, 0), (0x7762, 0xc7d9, 0x025b, 0xfda6, 0),
         (0x6b7a, 0xd2aa, 0xf2fb, 0x0b64, 0), (0, 0, 0, 0, 0)], 0xf82e, 0, 7, 0x3fff),
    'headphones': (
        [(0, 0, 0x0fa0, 0, 0)] * 4 + [(0, 0, 0x1180, 0, 0)], 0, 1,
        [(0, 0, 0, 0, 0)] * 5, 0, 0, 0, 0x3fff),
}
PRESETS['wide'], PRESETS['narrow'] = PRESETS['satellites'], PRESETS['monitors']   # ALSA's names


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


def block(x, sections, k, shift):
    acc = _s16(k) / 16384 * x
    for a1, a2, b0, b1, b2 in sections:
        a1, a2, b0, b1, b2 = (_s16(v) / 16384 for v in (a1, a2, b0, b1, b2))
        acc = acc + lfilter([b0, b1, b2], [1, -a1, -a2], x)
    return acc * 2 ** shift


def process(in_l, in_r, mode='satellites'):
    """mode: 'satellites' (the default setting), 'monitors' or 'headphones'."""
    eq, eq_k, eq_sh, xt, xt_k, xt_sh, delay, gin = PRESETS[mode]
    g = gin / 32768
    e = [block(np.asarray(x, float) * g, eq, eq_k, eq_sh) for x in (in_l, in_r)]
    d = [np.concatenate([np.zeros(delay), x])[:len(x)] for x in e]
    return e[0] + block(d[1], xt, xt_k, xt_sh), e[1] + block(d[0], xt, xt_k, xt_sh)
