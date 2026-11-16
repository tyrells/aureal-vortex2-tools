#!/usr/bin/env python3
"""Summarise and compare BAR0 dumps from 'vortex-poke.py dump'.

The converter's coefficient table is not exposed in any register (the register map's author),
so the filter-shape search below will not find it; it is kept for other tables.

  ./scan-dump.py A.bin [B.bin ...]

For each 4 KB page of each dump: how many words are zero, 0xDEADDEAD/0xDEADBABE fill, or the
same value repeated (an unmapped page returns the last value on the bus), and how many distinct
values it holds. Then, for every 256-word window, the window read as 32-bit words and as the
low and high 16-bit halves (signed), it reports windows that look like a windowed-sinc table:
a single dominant peak, values near symmetric about some centre, falling off away from the
peak with sign changes. Such a table may be stored as one long symmetric run or polyphase
(each phase a short run, phases one after another), so both a whole-window and a 27-tap check
are made. With two or more dumps, words that differ between them are counted per page, which
separates fixed tables from state and sample data.
"""
import struct
import sys

import numpy as np

PAGE = 0x1000
FILL = {0xDEADDEAD, 0xDEADBABE}


def load(path):
    d = open(path, 'rb').read()
    return np.array(struct.unpack(f'<{len(d) // 4}I', d), dtype=np.uint32)


def as_signed(w, view):
    if view == 'w32':
        return w.astype(np.int64) - ((w.astype(np.int64) >> 31) << 32)
    if view == 'lo16':
        v = (w & 0xffff).astype(np.int64)
    else:
        v = (w >> 16).astype(np.int64)
    return v - ((v >> 15) << 16)


def sinc_score(x):
    """0..1: how much x looks like a symmetric, peaked, sign-alternating filter."""
    x = x.astype(float)
    if len(x) < 9 or not np.any(x):
        return 0.0
    a = np.abs(x)
    k = int(np.argmax(a))
    peak = a[k]
    if peak == 0 or np.count_nonzero(x) < len(x) // 2:
        return 0.0
    if len(set(x.tolist())) < len(x) // 3:              # too repetitive for a filter
        return 0.0
    half = min(k, len(x) - 1 - k)
    if half < 4:
        return 0.0
    left, right = x[k - half:k][::-1], x[k + 1:k + 1 + half]
    sym = np.dot(left, right) / (np.linalg.norm(left) * np.linalg.norm(right) + 1e-9)
    dom = peak / (np.sort(a)[-2] + 1e-9)                 # main lobe should lead
    signs = np.count_nonzero(np.diff(np.sign(right[np.abs(right) > peak * 0.005])))
    decay = np.mean(np.abs(right[half // 2:])) / (np.mean(np.abs(right[:max(1, half // 2)])) + 1e-9)
    s = max(0.0, sym) * min(1.0, dom / 1.05) * (1.0 if signs >= 2 else 0.3) * (1.0 if decay < 0.7 else 0.4)
    return float(s)


def main():
    paths = sys.argv[1:]
    if not paths:
        sys.exit(__doc__)
    dumps = [load(p) for p in paths]
    for p, w in zip(paths, dumps):
        print(f'== {p}: {len(w) * 4:#x} bytes')
        for pg in range(0, len(w) * 4, PAGE):
            x = w[pg // 4:(pg + PAGE) // 4]
            z = int(np.count_nonzero(x == 0))
            fill = int(sum(np.count_nonzero(x == f) for f in FILL))
            vals, counts = np.unique(x, return_counts=True)
            top = int(counts.max())
            print(f'  {pg:#07x}  zero {z:4d}  fill {fill:4d}  most-common {int(vals[counts.argmax()]):#010x} x{top:4d}  distinct {len(vals):4d}')
        hits = []
        for view in ('w32', 'lo16', 'hi16'):
            v = as_signed(w, view)
            for start in range(0, len(v) - 256, 64):
                win = v[start:start + 256]
                sc = sinc_score(win)
                if sc > 0.6:
                    hits.append((sc, start * 4, view, 'whole'))
                for off in range(0, 256 - 27, 27):           # 27-tap phases
                    sc27 = sinc_score(win[off:off + 27])
                    if sc27 > 0.85:
                        hits.append((sc27, (start + off) * 4, view, '27-tap'))
        seen = set()
        print('  candidate filter tables (score, address, view, kind):')
        for sc, addr, view, kind in sorted(hits, reverse=True):
            key = (addr // 0x100, view, kind)
            if key in seen:
                continue
            seen.add(key)
            print(f'    {sc:.2f}  {addr:#07x}  {view:5s} {kind}')
            if len(seen) >= 30:
                break
        if not seen:
            print('    none')
    if len(dumps) > 1:
        print('== words that differ between dumps, per 4 KB page')
        a = dumps[0]
        for pg in range(0, len(a) * 4, PAGE):
            sl = slice(pg // 4, (pg + PAGE) // 4)
            diff = sum(int(np.count_nonzero(a[sl] != b[sl])) for b in dumps[1:])
            if diff:
                print(f'  {pg:#07x}  {diff}')


if __name__ == '__main__':
    main()
