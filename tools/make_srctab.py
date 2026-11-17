#!/usr/bin/env python3
"""Build the converter coefficient table from a full 'srcprobe.py sweep --step 1' capture.

  ./make_srctab.py SWEEP.npz OUT.npz [HEADER.h]

1. Align each phase's capture to the Kaiser fit (beta 7.70, half-width 12.85) on the taps a
   capture gets right (the first 22; the last four can overlap the impulse's next pass through
   the FIFO, and a capture can start late and miss the first ones). Captures that do not sum to
   the impulse (16384 +-8) or sit more than 5 LSB from the fit are dropped.
2. Merge with the mirror image: tap i of phase p equals tap 25 - i of phase 16384 - p. Where a
   value and its mirror disagree the one nearer the fit wins; where neither was read the fit,
   rounded, fills in (recorded in 'model_filled').
3. Keep phases 0-8192, replace isolated capture spikes (second difference along the phase of 3
   or more) by the mean of their neighbours ('smoothed'), and write the table and, if asked,
   the 86Box header snd_vortex2_srctab.h (deltas along the phase, two per character).
"""
import sys

import numpy as np
from numpy import i0

N, M = 16384, np.arange(-12, 14)
INV = np.iinfo(np.int64).min


def kaiser(f):
    x = M[None, :] - np.asarray(f)[:, None] / N
    a = np.abs(x)
    return 16384 * np.sinc(x) * i0(7.70 * np.sqrt(np.clip(1 - (a / 12.85) ** 2, 0, 1))) / i0(7.70) * (a < 12.85)


def main():
    d = np.load(sys.argv[1])
    R, F = d['response'].astype(np.int64), d['fracs'].astype(int)
    K = kaiser(np.arange(N))
    core = np.arange(26) < 22
    T = np.full((N, 26), INV, dtype=np.int64)
    for r, f in zip(R, F):
        if abs(r.sum() - 16384) > 8:
            continue
        rp = np.r_[np.full(30, INV), r]
        best = None
        for s in range(len(rp) - 25):
            w = rp[s:s + 26][::-1]              # outputs come out with the tap index falling
            known = (w != INV) & core
            if known.sum() >= 12:
                e = np.abs(w[known] - K[f][known]).max()
                if best is None or e < best[0]:
                    best = (e, s)
        if best and best[0] <= 5:
            T[f] = rp[best[1]:best[1] + 26][::-1]
    own = np.where(core[None, :], T, INV)
    par = np.full_like(own, INV)
    for f in range(1, N):
        par[f] = T[N - f][::-1]
    par = np.where(np.arange(26)[None, :] >= 4, par, INV)
    A, B = own != INV, par != INV
    out = np.full((N, 26), INV, dtype=np.int64)
    out = np.where(A & ~B, own, out)
    out = np.where(B & ~A, par, out)
    out = np.where(A & B, np.where(np.abs(own - K) <= np.abs(par - K), own, par), out)
    out[0] = 0; out[0][12] = 16384
    H = out[:N // 2 + 1]
    filled = H == INV
    H = np.where(filled, np.round(K[:N // 2 + 1]).astype(np.int64), H)
    smoothed = np.zeros_like(filled)
    for _ in range(10):
        n = 0
        for k in range(26):
            v = H[:, k]
            for f in range(1, len(v) - 1):
                if abs(2 * v[f] - v[f - 1] - v[f + 1]) >= 3:
                    v[f] = int(round((v[f - 1] + v[f + 1]) / 2)); smoothed[f, k] = True; n += 1
        if not n:
            break
    print(f'read {(~filled).sum()} of {filled.size}, filled from the fit {filled.sum()}, smoothed {smoothed.sum()}')
    np.savez_compressed(sys.argv[2], table=H.astype(np.int16), model_filled=filled, smoothed=smoothed)
    if len(sys.argv) > 3:
        D = (np.diff(H, axis=0) + 2).reshape(-1)
        assert D.min() >= 0 and D.max() <= 4, 'a step outside -2..2: the encoding needs widening'
        s = ''.join(chr(65 + 5 * int(a) + int(b)) for a, b in zip(D[0::2], D[1::2]))
        body = '\n'.join(f'    "{s[i:i + 100]}",' for i in range(0, len(s), 100))
        with open(sys.argv[3]) as f:
            old = f.read()
        head = old[:old.index('static const char *const v2_srctab_delta[] = {')]
        with open(sys.argv[3], 'w') as f:
            f.write(head + 'static const char *const v2_srctab_delta[] = {\n' + body + '\n    0\n};\n\n#endif\n')


if __name__ == '__main__':
    main()
