#!/usr/bin/env python3
"""Measure whole signal paths on an idle card with the driver's own settings, from the Linux host.

Run on the X79 host with the card bound to vfio-pci and no VM running, like srcprobe.py.

  sudo ./pathprobe.py eq [--log FILE]        the hardware EQ alone (VDB sink 0xa0 -> source 0x7e),
                                             the driver's coefficients with the bypass and band
                                             gains set in turn (current and target alike)
  sudo ./pathprobe.py paths [--out FILE.npz] [--log FILE]
                                             the two paths that the 3D level was measured between,
                                             each read at mixer 0's output (what goes to the
                                             codec's left channel), with every register the
                                             driver had set:
                                               native: input -> mixer 4 -> mixer 6 -> EQ -> mixer 0
                                               3D:     input -> A3D slot 14 -> canceller -> mixer 0
  sudo ./pathprobe.py clear

Why: in headphone mode a front 3D click came out +1.37 dB against a native click in the same
recording, and the model gave +3.88 dB, so a factor of 0.75 has been applied without knowing
where it is. Since then a3dprobe found the slices' outputs halved (leaving 1.5 to find) and
xtprobe found the canceller exactly as modelled. In the driver's routing at the time, the native
click does not go straight to the output: it goes through mixer 4, then mixer 6 at input gain
0x00 (0.5), then the hardware EQ, then mixer 0. The EQ is the one block not yet read digitally,
and the model plays it as its bypass gain alone (the driver's steady state: band targets 0, bypass
0x7fff; analog runs 9 and 16).

The settings are the driver's own registers from test-suite run 4 at 125 s, a 3D click at gain
1.0 in headphone mode (data/path-snapshot-run4.json): the mixer block, the canceller, the EQ and
A3D slice 3. Only the blocks on the path are loaded; the probe makes its own VDB route list
(FIFO 0 -> the path -> converter 0 -> FIFO 1, as srcprobe), and enables only the mixer inputs on
the path, so nothing held on another input can leak in. Each capture is a constant and an impulse.
"""
import importlib.util
import json
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('srcprobe', os.path.join(here, 'srcprobe.py'))
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)

SNAP = json.load(open(os.path.join(here, '..', 'data', 'path-snapshot-run4.json')))
EQ_IN, EQ_OUT = 0xa0, 0x7e
MIX_EN = 0x21a00        # + ((mix << 3) + (in >> 2)) * 4, bit in & 3
MIX_IN_T, MIX_IN_C = 0x21000, 0x20000
MIX_OUT_T, MIX_OUT_C = 0x21800, 0x20800
XT_CTRL = 0x24660
SLICE3 = 0x1fc00
SLOT = 14               # slice 3, source 2: the driver's first 3D slot
A_B = lambda s: 0x19000 + 3 * 0x2000 + s * 0x2c8
A_A = lambda s: 0x18000 + 3 * 0x2000 + s * 0x3a4


def words(name):
    return {int(a, 16): int(v, 16) for a, v in SNAP[name].items()}


def load(bar, ws, skip=lambda a: False):
    for a, v in ws.items():
        if not skip(a):
            bar.wr(a, v)


def routes(bar, log, pairs):
    """pairs: [(src, dst)], the last dst being the converter's input (srcprobe's chain continues
    from there to FIFO b)."""
    sp.w(bar, log, sp.VDB_WTDRAM_HEADERLINKS + 4 * sp.RATE, (pairs[0][0] << 8) | pairs[0][1], 'vdb hdr')
    for (s0, d0), (s1, d1) in zip(pairs, pairs[1:]):
        sp.w(bar, log, sp.VDB_WTDRAM_DESTLINKS + 4 * d0, (s1 << 8) | d1, f'vdb link {s1:#04x} -> {d1:#04x}')
    log(event='route', pairs=[f'{s:02x}->{d:02x}' for s, d in pairs])


def unroute(bar, log, pairs):
    for s, d in pairs[:-1]:
        bar.wr(sp.VDB_WTDRAM_DESTLINKS + 4 * d, sp.END)


def mixer_setup(bar, log, enable):
    """The driver's mixer block, current gains set to the targets, and only the (mix, input)
    pairs in enable switched on."""
    m = words('mixer')
    load(bar, m)
    for i in range(0x800 // 4):
        bar.wr(MIX_IN_C + 4 * i, m.get(MIX_IN_T + 4 * i, 0))
    for i in range(16):
        bar.wr(MIX_OUT_C + 4 * i, m.get(MIX_OUT_T + 4 * i, 0))
    for i in range(0x80 // 4):
        bar.wr(MIX_EN + 4 * i, 0)
    for mix, inp in enable:
        a = MIX_EN + ((mix << 3) + (inp >> 2)) * 4
        bar.wr(a, bar.rd(a) | (1 << (inp & 3)))
    log(event='mixer', enable=enable, gains={f'{mx}/{i}': hex(bar.rd(MIX_IN_C + ((mx << 5) + i) * 4)) for mx, i in enable},
        out=[hex(bar.rd(MIX_OUT_C + 4 * mx)) for mx in range(8)])


EQ_SIDES = (0x2b000, 0x2b1e0)
EQ_BYP_T, EQ_BYP_C = (0x2b3d4, 0x2b3ec), (0x2b3d0, 0x2b3e8)   # bypass target, current (ALSA au88x0_eq.c)


def eq_setup(bar, log, variant='steady'):
    """The driver's coefficients and time constants, with gains set as the variant says, current
    and target alike (run 1 of this probe copied the trace, where the band gains' current words
    held 0x3e96 gliding to targets of 0 and the bypass current was cleared, so it read a moment
    of a glide: the band sum, plus a bypass rising from 0)."""
    e = words('eq')
    for a in range(0x2b000, 0x2b450, 4):
        bar.wr(a, 0)
    load(bar, e)
    byp, bands = {'steady': (0x7fff, {}), 'bypass 0x3fff': (0x3fff, {}), 'bypass 0x8001': (0x8001, {}),
                  'all bands': (0, {b: 0x3e96 for b in range(10)}), 'one band 5': (0, {5: 0x3e96}),
                  'one band 0': (0, {0: 0x3e96}), 'one band 9': (0, {9: 0x3e96}),
                  'band 5 and bypass': (0x7fff, {5: 0x3e96}), 'bands off': (0, {})}[variant]
    for side in EQ_SIDES:
        for b in range(10):
            g = bands.get(b, 0)
            bar.wr(side + b * 0x30 + 0x28, g)
            bar.wr(side + b * 0x30 + 0x2c, g)
    for a in EQ_BYP_T + EQ_BYP_C:
        bar.wr(a, byp)
    log(event='eq', variant=variant, bypass=[hex(bar.rd(a)) for a in EQ_BYP_T + EQ_BYP_C],
        band5=[hex(bar.rd(0x2b000 + 5 * 0x30 + o)) for o in (0x28, 0x2c)], ctl=hex(bar.rd(0x2b440)))


def xt_setup(bar, log):
    for a in range(0x24000, 0x24560, 4):
        bar.wr(a, 0)
    sp.w(bar, log, XT_CTRL, 0xa0000000, 'xt control: off (driver)')
    load(bar, words('xt'), skip=lambda a: a == XT_CTRL)
    ctl = int(SNAP['xt_ctl'], 16) & 0x3ffff
    # only the input on the path (slice 3 left is canceller input 3), so nothing held leaks in
    for i in range(10):
        if i != 3:
            bar.wr(0x244d0 + 4 * i, 0)
    sp.w(bar, log, XT_CTRL, 0xc0840088 | (ctl & 0x3ff00), 'xt control: rate 17')
    sp.w(bar, log, XT_CTRL, 0xd0840089 | (ctl & 0x3ff00), 'xt control: enable')


def a3d_setup(bar, log):
    for s in range(4):
        for off in range(0, 0x2c8, 4):
            bar.wr(A_B(s) + off, 0)
        for off in range(0, 0x3a4, 4):
            bar.wr(A_A(s) + off, 0)
    src = SLOT & 3
    lo, hi = A_B(src), A_B(src) + 0x2c8
    loa, hia = A_A(src), A_A(src) + 0x3a4
    w = words('a3d_slice3')
    load(bar, {a: v for a, v in w.items() if lo <= a < hi or loa <= a < hia})
    b = A_B(src)
    # start from the targets (the driver's currents may be mid-glide), halves as the chip keeps them
    swap = lambda v: ((v & 0xffff) << 16) | (v >> 16)
    for k in range(56):
        bar.wr(b + 4 * k, swap(bar.rd(b + 0x100 + 4 * k)))
    for cur, tgt in ((0xe0, 0xe4), (0xe8, 0xec), (0xf0, 0xf4), (0xf8, 0xfc), (0x1e0, 0x1e4)):
        bar.wr(b + cur, swap(bar.rd(b + tgt)))
    for off in range(0x1e8, 0x2c8, 4):            # filter state
        bar.wr(b + off, 0)
    # Tracking off, so the currents written here are what plays. Run 1 kept the driver's rates
    # and read the targets' high halves back as 0 once the slice ran, which left the distance
    # filter with a1 alone (1.75, unstable).
    for i in range(4):
        bar.wr(A_A(src) + 0x37c + 4 * i, 0)
    log(event='a3d_before_enable', gain=[hex(bar.rd(b + 0xe0)), hex(bar.rd(b + 0xe4))], a12=[hex(bar.rd(b + 0xe8)), hex(bar.rd(b + 0xec))],
        b01=[hex(bar.rd(b + 0xf0)), hex(bar.rd(b + 0xf4))], taps=[hex(bar.rd(b + 4 * k)) for k in range(3)])
    sp.w(bar, log, SLICE3 + 0x40, 0, 'slice pointers 0')
    sp.w(bar, log, SLICE3 + 0x38, 0xb8000088, 'slice control: rate 17 (driver value)')
    sp.w(bar, log, SLICE3 + 0x38, 0xf0000001, 'slice control: enable (driver value)')
    log(event='a3d', gain=[hex(bar.rd(b + 0xe0)), hex(bar.rd(b + 0xe4))], b01=hex(bar.rd(b + 0xf4)),
        a12=hex(bar.rd(b + 0xec)), itd=hex(bar.rd(b + 0x1e4)), taps=[hex(bar.rd(b + 0x100 + 4 * k)) for k in range(4)])


def a3d_off(bar, log):
    sp.w(bar, log, SLICE3 + 0x38, 0xf0000000, 'slice control: disable')


def capture(bar, log, pairs, pattern, setup):
    sp.setup(bar, log, 0, 13, 0x4000, pattern=pattern)
    setup()
    routes(bar, log, pairs)
    r, _, _ = sp.flush_and_read(bar, log, flush=False)
    unroute(bar, log, pairs)
    sp.clear(bar, log)
    return r


def const(v):
    return [v & 0xffff] * 64


def impulse(v=0x4000, at=8):
    return [v & 0xffff if i == at else 0 for i in range(64)]


def plateau(r):
    nz = [i for i, x in enumerate(r) if x]
    seg = r[nz[0] + 8: nz[0] + 24] if nz else []
    return (sum(seg) / len(seg)) if seg else 0.0


F, S = sp.FIFO_IN, sp.SRC_DSTADDR
NATIVE = [(F, 0x66), (0x34, 0x51), (0x36, EQ_IN), (EQ_OUT, 0x57), (0x30, S)]     # mixin 22 -> mix 4 -> mixin 1 -> mix 6 -> EQ -> mixin 7 -> mix 0
NATIVE_EN = [(4, 22), (6, 1), (0, 7)]
THREED = [(F, 0x70 + SLOT), (0xa6 + 2 * (SLOT >> 2), 0x99), (0x66, 0x5b), (0x30, S)]  # A3D 14 -> slice 3 L -> canceller in 3 -> mixin 11 -> mix 0
THREED_EN = [(0, 11)]


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd not in ('eq', 'paths', 'clear'):
        sys.exit(__doc__)
    if sp.vm_running():
        sys.exit('a QEMU process is running; stop the VM first (this needs the card idle)')
    sp.wake()
    bar = sp.Bar()
    if bar.rd(0x2a00c) == 0xffffffff:
        sys.exit('the card reads 0xffffffff; see srcprobe.py')
    log = sp.Log(sp.arg('--log', 'pathprobe-log.jsonl', str))
    log(event='start', cmd=cmd, argv=sys.argv[1:], chip_ctrl=hex(bar.rd(0x2a00c)), snapshot=SNAP['source'])
    if bar.rd(0x2a00c) & (1 << 21):
        sp.release(bar, log)
    quiet = sp.Log(os.devnull)
    if cmd == 'eq':
        pairs = [(F, EQ_IN), (EQ_OUT, S)]
        for variant in ('steady', 'bypass 0x3fff', 'bypass 0x8001', 'all bands', 'one band 5', 'one band 0',
                        'one band 9', 'band 5 and bypass', 'bands off'):
            for name, pat in (('const 0x2000', const(0x2000)), ('impulse 0x4000', impulse())):
                r = capture(bar, quiet, pairs, pat, lambda: eq_setup(bar, log, variant))
                log(event='eqcase', variant=variant, case=name, level=round(plateau(r), 1),
                    first=[(i, x) for i, x in enumerate(r) if x][:40])
    elif cmd == 'paths':
        import numpy as np
        rows, names = [], []
        def run(name, pairs, pat, setup):
            r = capture(bar, quiet, pairs, pat, setup)
            log(event='path', case=name, level=round(plateau(r), 1), first=[(i, x) for i, x in enumerate(r) if x][:40])
            rows.append((len(names), *((r + [0] * 160)[:160])))
            names.append(name)
        for pname, pat in (('const 0x2000', const(0x2000)), ('impulse 0x4000', impulse())):
            run(f'native, {pname}', NATIVE, pat, lambda: (mixer_setup(bar, log, NATIVE_EN), eq_setup(bar, log)))
            run(f'native, EQ all bands (no bypass), {pname}', NATIVE, pat, lambda: (mixer_setup(bar, log, NATIVE_EN), eq_setup(bar, log, 'all bands')))
            run(f'3D, {pname}', THREED, pat, lambda: (mixer_setup(bar, log, THREED_EN), xt_setup(bar, log), a3d_setup(bar, log)))
            a3d_off(bar, log)
            # the 3D path's pieces: slice output alone, and canceller output alone
            run(f'3D to slice output, {pname}', [(F, 0x70 + SLOT), (0xa6 + 2 * (SLOT >> 2), S)], pat, lambda: a3d_setup(bar, log))
            a3d_off(bar, log)
            run(f'3D to canceller output, {pname}', [(F, 0x70 + SLOT), (0xa6 + 2 * (SLOT >> 2), 0x99), (0x66, S)], pat,
                lambda: (xt_setup(bar, log), a3d_setup(bar, log)))
            a3d_off(bar, log)
        path = sp.arg('--out', 'pathprobe.npz', str)
        np.savez(path, rows=np.array(rows), names=np.array(names))
        log(event='saved', path=path)
    elif cmd == 'clear':
        a3d_off(bar, log)
        sp.w(bar, log, XT_CTRL, 0xa0000000, 'xt control: off')
        for i in range(0x80 // 4):
            bar.wr(MIX_EN + 4 * i, 0)
        sp.clear(bar, log)


if __name__ == '__main__':
    main()
