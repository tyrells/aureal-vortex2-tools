#!/usr/bin/env python3
"""Measure the A3D engine's arithmetic on an idle card, from the Linux host.

Run on the X79 host with the card bound to vfio-pci and no VM running, like srcprobe.py.

  sudo ./a3dprobe.py pipe [--slot S] [--log FILE]
                                stage 1: one source set up as a pass-through, as the driver's
                                and ALSA's ProgramPipe do (one tap of 0x7fff, gain 0x7fff, b0
                                0x4000, glides off); a constant and an impulse, both outputs
                                read through a converter, and the slice's registers logged.
                                If nothing comes out, the log shows where it stopped.
  sudo ./a3dprobe.py sweep [--slot S] [--out FILE.npz] [--log FILE]
                                stage 2: the arithmetic one field at a time (about 30 s):
                                input level (down to 1 LSB, for the rounding) and sign, gain,
                                tap value and position, which half of each word is which ear,
                                the atmospheric filter's b0, the ITD, and overload
  sudo ./a3dprobe.py atmos [--slot S] [--out FILE.npz] [--log FILE]
                                where the distance filter's a2 and b1/b2 sit (12 cases)
  sudo ./a3dprobe.py clear [--slot S]

The chain: VDB FIFO 0, preloaded by the host, -> A3D input of slot S (VDB sink 0x70 + S) ->
the source's filters -> its slice's left or right output (VDB source 0xa6 + 2 x slice, +1 for
right) -> converter 0 at ratio 1 (passes samples unchanged; srcprobe) -> its output double
buffer, read as it runs. All on the AC'97 rate (17), the rate the driver gives the slices.

The point is the factor of about 0.75 measured on the 3D path in recordings (reference: Open
questions, bit accuracy): with every coefficient at unity this reads the slice's output against
its input directly, and stepping each field shows where the scale sits and how each product is
rounded.

Source set-up follows the Windows driver's start of a source as traced (run 4, slot 15 at
117.94 s, the same values as ALSA's a3dsrc_ProgramPipe): bank B currents and targets (HRTF taps,
gain, atmospheric filter, ITD), bank A's four tracking rates, then the slice control word written
0xb8000088 (rate 17 in bits 7-3) and 0xf0000001 (enable). Tracking rates are left at 0 (off) so
the current values written here are the ones used. The slice's other three sources are zeroed
with gain 0, so only ours reaches the output. Every write is logged with its readback.
"""
import importlib.util
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('srcprobe', os.path.join(here, 'srcprobe.py'))
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)

A3D_IN = 0x70                   # VDB sink: + slot
A3D_OUT = 0xa6                  # VDB source: + 2 x slice (left), + 1 (right)
TAPS = 56
SLOT = int(sp.arg('--slot', 0))
SLICE, SOURCE = SLOT >> 2, SLOT & 3


def bank_b(src):
    return 0x19000 + SLICE * 0x2000 + src * 0x2c8


def bank_a(src):
    return 0x18000 + SLICE * 0x2000 + src * 0x3a4


SL = 0x19c00 + SLICE * 0x2000   # slice registers
SL_VDBDEST, SL_VDBSRC, SL_AB, SL_C, SL_CTRL, SL_DBG, SL_PTR = SL, SL + 0x20, SL + 0x30, SL + 0x34, SL + 0x38, SL + 0x3c, SL + 0x40


def pair(left, right):
    return ((right & 0xffff) << 16) | (left & 0xffff)


def slice_regs(bar):
    return {'vdbdest': [hex(bar.rd(SL_VDBDEST + 4 * i)) for i in range(8)],
            'vdbsrc': [hex(bar.rd(SL_VDBSRC + 4 * i)) for i in range(4)],
            'ab': hex(bar.rd(SL_AB)), 'c': hex(bar.rd(SL_C)), 'ctrl': hex(bar.rd(SL_CTRL)),
            'dbg': hex(bar.rd(SL_DBG)), 'ptr': hex(bar.rd(SL_PTR)),
            'hrtf_out': [hex(bar.rd(bank_a(SOURCE) + 0x39c)), hex(bar.rd(bank_a(SOURCE) + 0x3a0))],
            'xy': [hex(bar.rd(bank_a(SOURCE) + 0x38c + 4 * i)) for i in range(4)]}


def zero_source(bar, src):
    for off in range(0, 0x2c8, 4):
        bar.wr(bank_b(src) + off, 0)
    for off in range(0, 0x3a4, 4):
        bar.wr(bank_a(src) + off, 0)


def source_setup(bar, log, taps=None, gain=(0x7fff, 0x7fff), atmos=(0, 0, 0x4000, 0, 0), itd=(0, 0),
                 swap_cur=False, raw=None, tc=(0, 0, 0, 0)):
    """taps: {index: (left, right)}, default tap 0 = 0x7fff both ears. atmos: a1, a2, b0, b1, b2.
    The targets take each pair as low half = left; the currents as the driver writes them at a
    start (run 4: B01Current 0x40000000 against B10Target 0x00004000, so b0 in the high half);
    swap_cur writes the taps', gain's and ITD's currents the other way round, to test which half
    the chip reads (the atmospheric words stay as the driver writes them)."""
    w = sp.w
    for s in range(4):
        zero_source(bar, s)
    taps = {0: (0x7fff, 0x7fff)} if taps is None else taps
    b = bank_b(SOURCE)
    a1, a2, b0, b1, b2 = atmos
    cur = lambda l, r: pair(r, l)       # atmospheric currents as the driver writes them: first value in the high half
    for k in range(TAPS):
        l, r = taps.get(k, (0, 0))
        bar.wr(b + 0x000 + 4 * k, pair(r, l) if not swap_cur else pair(l, r))
        bar.wr(b + 0x100 + 4 * k, pair(l, r))
    w(bar, log, b + 0xe0, pair(gain[1], gain[0]) if not swap_cur else pair(gain[0], gain[1]), 'GainCurrent')
    w(bar, log, b + 0xe4, pair(gain[0], gain[1]), 'GainTarget')
    w(bar, log, b + 0xe8, cur(a1, a2), 'A12Current')
    w(bar, log, b + 0xec, pair(a1, a2), 'A21Target')
    w(bar, log, b + 0xf0, cur(b0, b1), 'B01Current')
    w(bar, log, b + 0xf4, pair(b0, b1), 'B10Target')
    w(bar, log, b + 0xf8, cur(b2, 0), 'B2Current')
    w(bar, log, b + 0xfc, pair(b2, 0), 'B2Target')
    w(bar, log, b + 0x1e0, pair(itd[1], itd[0]) if not swap_cur else pair(itd[0], itd[1]), 'ITDCurrent')
    w(bar, log, b + 0x1e4, pair(itd[0], itd[1]), 'ITDTarget')
    log(event='taps', current=[hex(bar.rd(b + 4 * k)) for k in range(4)], target=[hex(bar.rd(b + 0x100 + 4 * k)) for k in range(4)])
    for off, v in (raw or {}).items():
        w(bar, log, b + off, v, f'raw bank B +{off:#x}')
    for i in range(4):
        w(bar, log, bank_a(SOURCE) + 0x37c + 4 * i, tc[i], 'tracking rate')
    w(bar, log, SL_PTR, 0, 'slice pointers 0 (driver init)')
    w(bar, log, SL_CTRL, 0xb8000088, 'slice control: rate 17 (driver value)')
    w(bar, log, SL_CTRL, 0xf0000001, 'slice control: enable (driver value)')


def route(bar, log, right):
    """Called after sp.setup (FIFO a -> SRC -> FIFO b): put the A3D slot between them."""
    out = A3D_OUT + 2 * SLICE + (1 if right else 0)
    sp.w(bar, log, sp.VDB_WTDRAM_HEADERLINKS + 4 * sp.RATE, (sp.FIFO_IN << 8) | (A3D_IN + SLOT), f'vdb hdr: FIFO a -> A3D in {SLOT}')
    sp.w(bar, log, sp.VDB_WTDRAM_DESTLINKS + 4 * (A3D_IN + SLOT), (out << 8) | sp.SRC_DSTADDR, f'vdb link: A3D out {out:#04x} -> SRC in')


def unroute(bar, log):
    sp.w(bar, log, sp.VDB_WTDRAM_DESTLINKS + 4 * (A3D_IN + SLOT), sp.END, 'vdb link: none')
    sp.w(bar, log, SL_CTRL, 0xf0000000, 'slice control: disable')


def capture(bar, log, right, pattern, **kw):
    sp.setup(bar, log, 0, 13, 0x4000, pattern=pattern)
    source_setup(bar, log, **kw)
    route(bar, log, right)
    r, _, _ = sp.flush_and_read(bar, log, flush=False)
    regs = slice_regs(bar)
    unroute(bar, log)
    sp.clear(bar, log)
    return r, regs


def plateau(r):
    nz = [i for i, x in enumerate(r) if x]
    seg = r[nz[0] + 4: nz[0] + 24] if nz else []
    return (sum(seg) / len(seg)) if seg else 0.0


def const(v):
    return [v & 0xffff] * 64


def impulse(v=0x4000, at=8):
    return [v & 0xffff if i == at else 0 for i in range(64)]


def cases():
    c = []
    for v in (0x1000, 0x2000, 0x4000, 0x7fff, -0x1000, -0x8000, 0x0800, 0x0100, 100, 16, 15, 8, 7, 5, 4, 3, 2, 1, -1, -2, -3):
        c.append((f'input {v}', const(v), {}))
    c.append(('impulse', impulse(), {}))
    for g in (0x4000, 0x2000, 0x1000, 0x0800, 0x0001, 0x8000, 0xc000):
        c.append((f'gain {g:#06x}', const(0x2000), dict(gain=(g, g))))
    c.append(('gain left only', const(0x2000), dict(gain=(0x7fff, 0))))
    c.append(('gain right only', const(0x2000), dict(gain=(0, 0x7fff))))
    for t in (0x4000, 0x2000, 0x1000, 0x0001, -0x8000):
        c.append((f'tap {t & 0xffff:#06x}', const(0x2000), dict(taps={0: (t, t)})))
    c.append(('tap left only', const(0x2000), dict(taps={0: (0x7fff, 0)})))
    c.append(('tap right only', const(0x2000), dict(taps={0: (0, 0x7fff)})))
    c.append(('currents swapped, tap left only', const(0x2000), dict(taps={0: (0x7fff, 0)}, swap_cur=True)))
    c.append(('currents swapped, gain left only', const(0x2000), dict(gain=(0x7fff, 0), swap_cur=True)))
    for k in (1, 5, 20, 55):
        c.append((f'tap at {k}', impulse(), dict(taps={k: (0x7fff, 0x7fff)})))
    c.append(('two taps 0x4000 at 0 and 3', impulse(), dict(taps={0: (0x4000, 0x4000), 3: (0x4000, 0x4000)})))
    c.append(('taps 0x7fff at 0 and 1 (sum past 1)', const(0x4000), dict(taps={0: (0x7fff, 0x7fff), 1: (0x7fff, 0x7fff)})))
    for b0 in (0x2000, 0x7fff, 0x1000, 0x0001, -0x4000):
        c.append((f'b0 {b0 & 0xffff:#06x}', const(0x2000), dict(atmos=(0, 0, b0, 0, 0))))
    c.append(('b0 0x4000 b1 0x4000', const(0x2000), dict(atmos=(0, 0, 0x4000, 0x4000, 0))))
    c.append(('b0 0x4000 a1 0x2000 (pole 0.5)', impulse(), dict(atmos=(0x2000, 0, 0x4000, 0, 0))))
    c.append(('b0 0x4000 a1 0x2000 (pole 0.5), step', const(0x1000), dict(atmos=(0x2000, 0, 0x4000, 0, 0))))
    c.append(('b0 0x7fff, input 0x7fff (overload)', const(0x7fff), dict(atmos=(0, 0, 0x7fff, 0, 0))))
    c.append(('b0 0x7fff, input -0x8000 (overload)', const(-0x8000), dict(atmos=(0, 0, 0x7fff, 0, 0))))
    for d in ((0x600, 0), (0, 0x600), (0x100, 0x100), (0x80, 0x80), (0x1000, 0x1000)):
        c.append((f'itd {d[0]:#x}/{d[1]:#x}', impulse(), dict(itd=d)))
    return c


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd not in ('pipe', 'sweep', 'atmos', 'clear'):
        sys.exit(__doc__)
    if sp.vm_running():
        sys.exit('a QEMU process is running; stop the VM first (this needs the card idle)')
    sp.wake()
    bar = sp.Bar()
    if bar.rd(0x2a00c) == 0xffffffff:
        sys.exit('the card reads 0xffffffff; see srcprobe.py')
    log = sp.Log(sp.arg('--log', 'a3dprobe-log.jsonl', str))
    log(event='start', cmd=cmd, argv=sys.argv[1:], slot=SLOT, chip_ctrl=hex(bar.rd(0x2a00c)))
    if bar.rd(0x2a00c) & (1 << 21):
        sp.release(bar, log)
    quiet = sp.Log(os.devnull)
    if cmd == 'pipe':
        log(event='slice_before', **slice_regs(bar))
        for name, pat in (('const 0x2000', const(0x2000)), ('impulse 0x4000', impulse())):
            for right in (False, True):
                r, regs = capture(bar, log, right, pat)
                log(event='pipe', case=name, out='right' if right else 'left', level=plateau(r),
                    nonzero=[(i, x) for i, x in enumerate(r) if x][:40], slice=regs)
    elif cmd == 'sweep':
        import numpy as np
        rows, names = [], []
        for name, pat, kw in cases():
            res = []
            for right in (False, True):
                r, regs = capture(bar, quiet, right, pat, **kw)
                rows.append((len(names), int(right), *((r + [0] * 160)[:160])))
                res.append((plateau(r), [(i, x) for i, x in enumerate(r) if x][:12]))
            log(event='case', n=len(names), case=name, left=round(res[0][0], 2), right=round(res[1][0], 2),
                first_left=res[0][1], first_right=res[1][1])
            names.append(name)
        path = sp.arg('--out', 'a3dprobe-sweep.npz', str)
        np.savez(path, rows=np.array(rows), names=np.array(names))
        log(event='saved', path=path, cases=len(names))
    elif cmd == 'atmos':
        # pathprobe run 2: the driver's distance filter (b0 0x00fe, a1 0x700a, a2 0xcef7, a stable
        # low-pass) grew on the card by 1.75 a sample, as if a2 were not applied. Where is a2?
        import numpy as np
        D = dict(a1=0x700a, a2=0xcef7, b0=0x00fe)
        cs = [
            ('a1 0x2000 (control: pole 0.5)', impulse(), dict(atmos=(0x2000, 0, 0x4000, 0, 0))),
            ('a2 0x2000 via A12Current low half', impulse(), dict(atmos=(0, 0x2000, 0x4000, 0, 0))),
            ('a2 -0x2000 via A12Current low half', impulse(), dict(atmos=(0, -0x2000 & 0xffff, 0x4000, 0, 0))),
            ('raw A12Current 0x00002000', impulse(), dict(raw={0xe8: 0x00002000})),
            ('raw A12Current 0x20000000', impulse(), dict(raw={0xe8: 0x20000000})),
            ('raw B2Current 0x00002000 (low half)', impulse(), dict(raw={0xf8: 0x00002000})),
            ('raw B2Current 0x20000000 (high half: b2?)', impulse(), dict(raw={0xf8: 0x20000000})),
            ('raw B01Current 0x40002000 (b1 in low half?)', impulse(), dict(raw={0xf0: 0x40002000})),
            ('a1 0x4000 a2 -0x2000 (poles 0.5 +- 0.5j)', impulse(), dict(atmos=(0x4000, -0x2000 & 0xffff, 0x4000, 0, 0))),
            ('driver set as currents, step', const(0x2000), dict(atmos=(D['a1'], D['a2'], D['b0'], 0, 0))),
            ('driver set as currents, impulse', impulse(), dict(atmos=(D['a1'], D['a2'], D['b0'], 0, 0))),
            ('driver set as targets only, driver tracking rates, step', const(0x2000),
             dict(atmos=(0, 0, 0x4000, 0, 0), tc=(0x147, 0x300, 0x300, 0xc8),
                  raw={0xec: (D['a2'] << 16) | D['a1'], 0xf4: D['b0'], 0xe8: 0, 0xf0: 0x40000000})),
        ]
        rows, names = [], []
        for name, pat, kw in cs:
            res = []
            for right in (False, True):
                r, regs = capture(bar, quiet, right, pat, **kw)
                rows.append((len(names), int(right), *((r + [0] * 160)[:160])))
                res.append([(i, x) for i, x in enumerate(r) if x][:20])
            log(event='atmos', n=len(names), case=name, first_left=res[0], first_right=res[1])
            names.append(name)
        path = sp.arg('--out', 'a3dprobe-atmos.npz', str)
        np.savez(path, rows=np.array(rows), names=np.array(names))
        log(event='saved', path=path)
    elif cmd == 'clear':
        unroute(bar, log)
        sp.w(bar, log, SL_CTRL, 0, 'slice control 0')
        for s in range(4):
            zero_source(bar, s)
        sp.clear(bar, log)


if __name__ == '__main__':
    main()
