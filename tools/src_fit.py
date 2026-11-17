#!/usr/bin/env python3
"""Fit the converter's interpolation kernel from a 'srcprobe.py sweep' file.

  ./src_fit.py data/srcprobe-sweep-20261117.npz

Each row of 'response' is the converter's output for a single-sample impulse (16384) at one
time fraction (fracs, 14-bit) and a conversion ratio of 1: the kernel sampled at
t = t_peak - (j - peak), with t_peak = -phase below a half and 1 - phase above. Rows whose 26
taps do not sum to the impulse (a capture that lost or doubled a sample) are dropped. Prints the
best Kaiser window (beta, half-width) and the residual, robustly (points more than 4 LSB off
the first fit are left out of the second).
"""
import sys

import numpy as np
from numpy import i0

d = np.load(sys.argv[1])
R, F = d['response'].astype(float), d['fracs']
ts, hs = [], []
for r, f in zip(R, F):
    phi = f / 16384
    p = int(np.argmax(np.abs(r)))
    tp = 0.5 if f == 0x2000 else (-phi if phi < 0.5 else 1 - phi)
    t = tp - (np.arange(len(r)) - p)
    m = np.abs(t) < 13
    if m.sum() == 26 and abs(r[m].sum() - 16384) < 8:
        ts.append(t[m]); hs.append(r[m])
t, h = np.concatenate(ts), np.concatenate(hs)
a, s = np.abs(t), 16384 * np.sinc(t)
print(f'{len(ts)} of {len(F)} phases usable, {len(t)} points')


def model(b, L):
    return s * i0(b * np.sqrt(np.clip(1 - (a / L) ** 2, 0, 1))) / i0(b) * (a < L)


keep = np.ones(len(t), bool)
for _ in range(3):
    best = min((np.sqrt(np.mean((h - model(b, L))[keep] ** 2)), b, L)
               for L in np.arange(12.0, 14.01, 0.05) for b in np.arange(6, 9.5, 0.02))
    keep = np.abs(h - model(best[1], best[2])) < 4
print(f'Kaiser beta {best[1]:.2f}, half-width {best[2]:.2f}: rms {best[0]:.2f} LSB of 16384 '
      f'({keep.sum()} of {len(t)} points kept)')
