#!/usr/bin/env python3
"""Measure the crosstalk canceller's arithmetic on an idle card, from the Linux host.

Run on the X79 host with the card bound to vfio-pci and no VM running, like srcprobe.py.

  sudo ./xtprobe.py pipe [--log FILE]      stage 1: the direct path alone (every section 0, the
                                           left EQ block's k at 1.0, its input gain at 0x7fff), a
                                           constant into input 0, both outputs read. If nothing
                                           comes out, the log shows the block's registers
  sudo ./xtprobe.py sweep [--out FILE.npz] [--log FILE]
                                           stage 2 (about 20 s): input gain, k and shift; which
                                           input each gain word scales; the right side; rounding
                                           down to 1 LSB; overload; one section at a time (which
                                           word is which coefficient); the cross path and its
                                           delay; then the driver's three presets as traced
  sudo ./xtprobe.py clear

The chain: VDB FIFO 0, preloaded by the host -> canceller input I (VDB sink 0x96 + I; 0-4 left,
5-9 right, as the driver routes the A3D slices) -> canceller output (VDB source 0x66 left, 0x67
right) -> converter 0 at ratio 1 (passes samples unchanged; srcprobe) -> its output double buffer.

The point is the rest of the 3D path's measured 0.75 (reference: Open questions, bit accuracy):
a3dprobe found the slices' outputs halved, which leaves a factor of 1.5 that the model applies at
the canceller without knowing where it is. The headphone preset is five flat sections (sum 1.25),
shift 1 and input gains 0x3fff, which the model reads as 1.25 x the input; the card's reading of
the same registers says whether the 1.5 is here.

Set-up follows the driver (run 4): the canceller's words 0x24000-0x2455c cleared, the control
word written 0xa0000000, then 0xc0840088 (rate 17 in bits 7-3) and 0xd0840089 (enable), with the
two delays in bits 17-13 (left) and 12-8 (right). The presets are the words the driver wrote in
test-suite runs 4, 5 and 9 (data/xt-presets-traced.json). Every write is logged with its readback.
"""
import importlib.util
import json
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('srcprobe', os.path.join(here, 'srcprobe.py'))
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)

XT_IN, XT_OUT = 0x96, 0x66
BLOCK = {'eql': 0x24200, 'eqr': 0x242b4, 'xtl': 0x24368, 'xtr': 0x2441c}   # + section x 0x24 + word x 4
KSH = {'eql': 0x24538, 'eqr': 0x24540, 'xtl': 0x24548, 'xtr': 0x24550}     # k, then shift
GIN = 0x244d0                                                              # + input x 4
CTRL = 0x24660
PRESETS = os.path.join(here, '..', 'data', 'xt-presets-traced.json')


def regs(bar):
    return {'ctrl': hex(bar.rd(CTRL)), 'gin': [hex(bar.rd(GIN + 4 * i)) for i in range(10)],
            'ksh': [hex(bar.rd(0x24538 + 4 * i)) for i in range(8)],
            'eql_s0': [hex(bar.rd(BLOCK['eql'] + 4 * j)) for j in range(9)]}


def xt_setup(bar, log, k=None, sh=None, gin=None, sec=None, dl=0, dr=0, words=None, ctl=None):
    """k, sh: {block: value}; gin: {input: value}; sec: {(block, section, word): value};
    words: {addr: value} (a traced preset); ctl: its control word (delays taken from it)."""
    for a in range(0x24000, 0x24560, 4):
        bar.wr(a, 0)
    sp.w(bar, log, CTRL, 0xa0000000, 'xt control: off (driver)')
    for a, v in (words or {}).items():
        bar.wr(a, v)
    for b, v in (k or {}).items():
        bar.wr(KSH[b], v & 0xffff)
    for b, v in (sh or {}).items():
        bar.wr(KSH[b] + 4, v)
    for i, v in (gin or {}).items():
        bar.wr(GIN + 4 * i, v & 0xffff)
    for (b, s, j), v in (sec or {}).items():
        bar.wr(BLOCK[b] + 0x24 * s + 4 * j, v & 0xffff)
    d = (ctl & 0x3ff00) if ctl is not None else ((dl & 0x1f) << 13) | ((dr & 0x1f) << 8)
    sp.w(bar, log, CTRL, 0xc0840088 | d, 'xt control: rate 17 (driver)')
    sp.w(bar, log, CTRL, 0xd0840089 | d, 'xt control: enable (driver)')
    log(event='xt_regs', **regs(bar))


def capture(bar, log, inp, right, pattern, **kw):
    out = XT_OUT + (1 if right else 0)
    sp.setup(bar, log, 0, 13, 0x4000, pattern=pattern)
    xt_setup(bar, log, **kw)
    sp.w(bar, log, sp.VDB_WTDRAM_HEADERLINKS + 4 * sp.RATE, (sp.FIFO_IN << 8) | (XT_IN + inp), f'vdb hdr: FIFO a -> xt in {inp}')
    sp.w(bar, log, sp.VDB_WTDRAM_DESTLINKS + 4 * (XT_IN + inp), (out << 8) | sp.SRC_DSTADDR, f'vdb link: xt out {out:#04x} -> SRC in')
    r, _, _ = sp.flush_and_read(bar, log, flush=False)
    after = regs(bar)
    sp.w(bar, log, sp.VDB_WTDRAM_DESTLINKS + 4 * (XT_IN + inp), sp.END, 'vdb link: none')
    sp.w(bar, log, CTRL, 0xa0000000, 'xt control: off')
    sp.clear(bar, log)
    return r, after


def plateau(r):
    nz = [i for i, x in enumerate(r) if x]
    seg = r[nz[0] + 6: nz[0] + 24] if nz else []
    return (sum(seg) / len(seg)) if seg else 0.0


def const(v):
    return [v & 0xffff] * 64


def impulse(v=0x4000, at=8):
    return [v & 0xffff if i == at else 0 for i in range(64)]


def cases():
    U = dict(k={'eql': 0x4000}, gin={0: 0x7fff})
    c = [('unit', 0, const(0x2000), U), ('unit impulse', 0, impulse(), U)]
    for g in (0x3fff, 0x4000, 0x2000, 0x8000, 0x0001):
        c.append((f'gin {g:#06x}', 0, const(0x2000), dict(k={'eql': 0x4000}, gin={0: g})))
    for k in (0x2000, 0x7fff, 0x8000, 0xc000, 0x0001):
        c.append((f'k {k:#06x}', 0, const(0x2000), dict(k={'eql': k}, gin={0: 0x7fff})))
    for s in (1, 2, 3, 4, 8, 15):
        c.append((f'k 0x0800 shift {s}', 0, const(0x2000), dict(k={'eql': 0x0800}, sh={'eql': s}, gin={0: 0x7fff})))
    for i in (1, 3, 4):
        c.append((f'input {i}, its own gin', i, const(0x2000), dict(k={'eql': 0x4000}, gin={i: 0x7fff})))
        c.append((f'input {i}, gin 0 only', i, const(0x2000), dict(k={'eql': 0x4000}, gin={0: 0x7fff})))
    c.append(('input 5 (right side)', 5, const(0x2000), dict(k={'eqr': 0x4000}, gin={5: 0x7fff})))
    c.append(('input 9 (right side)', 9, const(0x2000), dict(k={'eqr': 0x4000}, gin={9: 0x7fff})))
    for v in (1, 2, 3, 5, 7, -1, -2, -3, 0x7fff, -0x8000):
        c.append((f'value {v}', 0, const(v), U))
    for v in (1, 2, 3, 5, 7, -1, -3, 0x1001):
        c.append((f'value {v}, gin 0x3fff', 0, const(v), dict(k={'eql': 0x4000}, gin={0: 0x3fff})))
    c.append(('overload: k 0x7fff, input 0x7fff', 0, const(0x7fff), dict(k={'eql': 0x7fff}, gin={0: 0x7fff})))
    c.append(('overload: shift 2, input 0x4000', 0, const(0x4000), dict(k={'eql': 0x4000}, sh={'eql': 2}, gin={0: 0x7fff})))
    # one section, one word at a time (which is b0, b1, b2, a1, a2; words 5-8 presumably state)
    for j in range(9):
        c.append((f'section 0 word {j} = 0x4000', 0, impulse(), dict(sec={('eql', 0, j): 0x4000}, gin={0: 0x7fff})))
    for j in range(5):
        c.append((f'section 0 word {j} = 0x2000 with word 2 = 0x4000', 0, impulse(),
                  dict(sec={('eql', 0, 2): 0x4000, ('eql', 0, j): 0x2000} if j != 2 else {('eql', 0, 2): 0x2000}, gin={0: 0x7fff})))
    c.append(('sections 0 and 4, word 2 = 0x2000', 0, const(0x2000), dict(sec={('eql', 0, 2): 0x2000, ('eql', 4, 2): 0x2000}, gin={0: 0x7fff})))
    c.append(('section word 2 = 0x2000 and k 0x2000', 0, const(0x2000), dict(sec={('eql', 0, 2): 0x2000}, k={'eql': 0x2000}, gin={0: 0x7fff})))
    # cross path: left EQ unit, left-to-right XT block unit, delay
    for d in (0, 1, 5, 13, 31):
        X = dict(k={'eql': 0x4000, 'xtl': 0x4000}, gin={0: 0x7fff}, dl=d)
        c.append((f'cross, left delay {d}, right out', 0, impulse(), X))
    c.append(('cross, right delay 7, right out', 0, impulse(), dict(k={'eql': 0x4000, 'xtl': 0x4000}, gin={0: 0x7fff}, dr=7)))
    c.append(('cross, xtr block (should feed left)', 0, impulse(), dict(k={'eql': 0x4000, 'xtr': 0x4000}, gin={0: 0x7fff})))
    return c


def preset_cases():
    c = []
    try:
        pre = json.load(open(PRESETS))
    except OSError:
        return c
    for name, p in pre.items():
        words = {int(a, 16): int(v, 16) for a, v in p['words'].items()}
        kw = dict(words=words, ctl=int(p['ctl'], 16))
        for inp in (0, 5):
            c.append((f'{name}, const 0x2000, input {inp}', inp, const(0x2000), kw))
            c.append((f'{name}, impulse 0x4000, input {inp}', inp, impulse(), kw))
    return c


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd not in ('pipe', 'sweep', 'clear'):
        sys.exit(__doc__)
    if sp.vm_running():
        sys.exit('a QEMU process is running; stop the VM first (this needs the card idle)')
    sp.wake()
    bar = sp.Bar()
    if bar.rd(0x2a00c) == 0xffffffff:
        sys.exit('the card reads 0xffffffff; see srcprobe.py')
    log = sp.Log(sp.arg('--log', 'xtprobe-log.jsonl', str))
    log(event='start', cmd=cmd, argv=sys.argv[1:], chip_ctrl=hex(bar.rd(0x2a00c)))
    if bar.rd(0x2a00c) & (1 << 21):
        sp.release(bar, log)
    quiet = sp.Log(os.devnull)
    if cmd == 'pipe':
        log(event='xt_before', **regs(bar))
        for name, inp, pat, kw in cases()[:2]:
            for right in (False, True):
                r, after = capture(bar, log, inp, right, pat, **kw)
                log(event='pipe', case=name, out='right' if right else 'left', level=plateau(r),
                    nonzero=[(i, x) for i, x in enumerate(r) if x][:40], xt=after)
    elif cmd == 'sweep':
        import numpy as np
        rows, names = [], []
        for name, inp, pat, kw in cases() + preset_cases():
            res = []
            for right in (False, True):
                r, _ = capture(bar, quiet, inp, right, pat, **kw)
                rows.append((len(names), int(right), *((r + [0] * 160)[:160])))
                res.append((plateau(r), [(i, x) for i, x in enumerate(r) if x][:16]))
            log(event='case', n=len(names), case=name, left=round(res[0][0], 2), right=round(res[1][0], 2),
                first_left=res[0][1], first_right=res[1][1])
            names.append(name)
        path = sp.arg('--out', 'xtprobe-sweep.npz', str)
        np.savez(path, rows=np.array(rows), names=np.array(names))
        log(event='saved', path=path, cases=len(names))
    elif cmd == 'clear':
        sp.w(bar, log, CTRL, 0xa0000000, 'xt control: off')
        for a in range(0x24000, 0x24560, 4):
            bar.wr(a, 0)
        sp.clear(bar, log)


if __name__ == '__main__':
    main()
