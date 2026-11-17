#!/usr/bin/env python3
"""Build the converter coefficient table from one or more 'srcprobe.py sweep' captures.

  ./make_srctab.py OUT.npz [--header HEADER.h] [--todo PHASES.txt] SWEEP.npz [SWEEP.npz ...]

1. Align each phase's capture to the Kaiser fit (beta 7.70, half-width 12.85) on the taps a
   capture gets right (the first 22; the last four can overlap the impulse's next pass through
   the converter's 32-entry FIFO). Captures that do not sum to the impulse (16384 +-8) or sit
   more than 5 LSB from the fit are dropped.
2. For each tap of each phase, every reading is a vote: the tap itself in every sweep, and its
   mirror (tap 25 - i of phase 16384 - p) in every sweep. The value with the most votes wins;
   a tie goes to the value nearer the fit. Where nothing was read the fit, rounded, fills in
   (recorded in 'model_filled'); 'votes' and 'agree' record the support for each value.
3. Keep phases 0-8192, replace isolated capture spikes (second difference along the phase of 3
   or more) by the mean of their neighbours ('smoothed'), and write the table, the 86Box header
   snd_vortex2_srctab.h (deltas along the phase, two per character) and, with --todo, the
   fractions still worth measuring (each filled or smoothed phase and its mirror), for
   'srcprobe.py sweep --phases'.
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


def align(path, K):
    d = np.load(path)
    R, F = d['response'].astype(np.int64), d['fracs'].astype(int)
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
            row = rp[best[1]:best[1] + 26][::-1]
            T[f] = np.where(core, row, INV)
    return T


def main():
    args = sys.argv[1:]
    opt = {}
    for k in ('--header', '--todo'):
        if k in args:
            i = args.index(k); opt[k] = args[i + 1]; del args[i:i + 2]
    out_path, sweeps = args[0], args[1:]
    K = kaiser(np.arange(N))
    H = np.zeros((N // 2 + 1, 26), dtype=np.int64)
    votes = np.zeros(H.shape, dtype=np.int16)
    agree = np.zeros(H.shape, dtype=np.int16)
    cands = [[{} for _ in range(26)] for _ in range(N // 2 + 1)]
    for path in sweeps:
        T = align(path, K)
        print(f'{path}: {int((T != INV).any(1).sum())} phases aligned')
        for p in range(N // 2 + 1):
            for i in range(26):
                for v in (T[p][i], T[(N - p) % N][25 - i] if p else INV):
                    if v != INV:
                        cands[p][i][int(v)] = cands[p][i].get(int(v), 0) + 1
    filled = np.zeros(H.shape, dtype=bool)
    for p in range(N // 2 + 1):
        for i in range(26):
            c = cands[p][i]
            if not c:
                H[p][i] = int(round(K[p][i])); filled[p][i] = True
                continue
            v = max(c, key=lambda x: (c[x], -abs(x - K[p][i])))
            H[p][i] = v; votes[p][i] = sum(c.values()); agree[p][i] = c[v]
    H[0] = 0; H[0][12] = 16384; filled[0] = False
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
    split = int(((votes > 0) & (agree < votes)).sum())
    print(f'read {(~filled).sum()} of {filled.size}, filled from the fit {filled.sum()}, '
          f'smoothed {smoothed.sum()}, read with disagreeing votes {split}')
    np.savez_compressed(out_path, table=H.astype(np.int16), model_filled=filled, smoothed=smoothed,
                        votes=votes, agree=agree)
    if '--todo' in opt:
        ph = sorted({q for p in np.where((filled | smoothed).any(1))[0] for q in (int(p), (N - int(p)) % N)})
        open(opt['--todo'], 'w').write('\n'.join(f'{q:#06x}' for q in ph) + '\n')
        print(f'{len(ph)} fractions written to {opt["--todo"]}')
    if '--header' in opt:
        D = (np.diff(H, axis=0) + 2).reshape(-1)
        assert D.min() >= 0 and D.max() <= 4, 'a step outside -2..2: the encoding needs widening'
        s = ''.join(chr(65 + 5 * int(a) + int(b)) for a, b in zip(D[0::2], D[1::2]))
        body = '\n'.join(f'    "{s[i:i + 100]}",' for i in range(0, len(s), 100))
        with open(opt['--header']) as f:
            old = f.read()
        head = old[:old.index('static const char *const v2_srctab_delta[] = {')]
        with open(opt['--header'], 'w') as f:
            f.write(head + 'static const char *const v2_srctab_delta[] = {\n' + body + '\n    0\n};\n\n#endif\n')


if __name__ == '__main__':
    main()
