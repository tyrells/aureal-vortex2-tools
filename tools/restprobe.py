#!/usr/bin/env python3
"""The remaining digital questions about the Vortex 2, on an idle card, from the Linux host.

Run on the X79 host with the card bound to vfio-pci and no VM running, like srcprobe.py.

  sudo ./restprobe.py xtdelay [--out FILE.npz] [--log FILE]
        the canceller's cross-feed delay, which the analog runs confirmed but xtprobe could not
        read: two VDB FIFOs (0 and 2), both holding an impulse at the same place and started
        together, feed canceller inputs 0 (left) and 5 (right). The right output is then EQ
        right's direct path (input 5, at once, 16,384) plus XT left of EQ left's output (input
        0, half size, 8,192) after the left delay field, so the gap between the two impulses
        in one capture is the delay; likewise the left output with the right field.
  sudo ./restprobe.py eqgate [--log FILE]
        why the EQ's bands gave silence without the bypass when fed straight from a FIFO
        (pathprobe run 2) but their normal sum when fed through mixer 6: the same EQ settings
        fed both ways, with the bypass at 0 and at 1 (the smallest step), and the EQ's own
        registers (0x2b410-0x2b44c, ALSA's EQ_DEST, EQ_SOURCE and EQ_CTRL) logged each time.
  sudo ./restprobe.py mix [--out FILE.npz] [--log FILE]
        the mixer's arithmetic to the last bit: one input at input gains 0x00 (0.5) to 0x1f
        (7.5) with values down to 1 LSB and both signs, the output gain, saturation, and two
        inputs (FIFOs 0 and 2) summing past full scale.
  sudo ./restprobe.py clear

Everything follows srcprobe's chain (a VDB FIFO -> the block -> converter 0 at ratio 1 -> FIFO 1,
read from the converter's output double buffer as it runs), with the second FIFO set up the same
way as the first. Every write that matters is logged with its readback.
"""
import importlib.util
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, name + '.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sp = load('srcprobe')
xp = load('xtprobe')
xp.sp = sp                      # one srcprobe instance for everything

FIFO2 = 2
MIX_HDR, MIX_NEXT, MIX_ACTIVE = 0x21e40, 0x21e00, 0x21f00
MIX_IN_T, MIX_IN_C, MIX_OUT_T, MIX_OUT_C, MIX_EN = 0x21000, 0x20000, 0x21800, 0x20800, 0x21a00
EQ_IN, EQ_OUT = 0xa0, 0x7e
F, S = sp.FIFO_IN, sp.SRC_DSTADDR


def fifo2(bar, log, pattern, dmaptr=32):
    """A second play FIFO, set up as srcprobe sets up its first."""
    for i in range(64):
        bar.wr(sp.FIFO_VDB_CHANNEL_DATA + 0x100 * FIFO2 + 4 * i, (pattern[i] if i < len(pattern) else 0) & 0xffff)
    sp.w(bar, log, sp.FIFO_VDB_CHANNEL_PC + 4 * FIFO2, ((dmaptr & 0x3f) << 12) | sp.PC_VALID, 'fifo 2: valid, DMA pointer ahead')
    sp.w(bar, log, sp.DMA_VDBCH_MODE + 4 * FIFO2, (8 << 14) | (1 << 13), 'dma mode 2: 16-bit, play')


def fifo2_off(bar, log):
    sp.w(bar, log, sp.FIFO_VDB_CHANNEL_PC + 4 * FIFO2, 0, 'fifo 2: off')


def routes(bar, log, pairs):
    sp.w(bar, log, sp.VDB_WTDRAM_HEADERLINKS + 4 * sp.RATE, (pairs[0][0] << 8) | pairs[0][1], 'vdb hdr')
    for (s0, d0), (s1, d1) in zip(pairs, pairs[1:]):
        sp.w(bar, log, sp.VDB_WTDRAM_DESTLINKS + 4 * d0, (s1 << 8) | d1, f'vdb link {s1:#04x} -> {d1:#04x}')
    log(event='route', pairs=[f'{s:02x}->{d:02x}' for s, d in pairs])


def unroute(bar, pairs):
    for s, d in pairs[:-1]:
        bar.wr(sp.VDB_WTDRAM_DESTLINKS + 4 * d, sp.END)


def run(bar, log, pairs, pat, setup, pat2=None):
    sp.setup(bar, log, 0, 13, 0x4000, pattern=pat)
    if pat2 is not None:
        fifo2(bar, log, pat2)
    setup()
    routes(bar, log, pairs)
    r, _, _ = sp.flush_and_read(bar, log, flush=False)
    unroute(bar, pairs)
    if pat2 is not None:
        fifo2_off(bar, log)
    sp.clear(bar, log)
    return r


def impulse(v=0x4000, at=8):
    return [v & 0xffff if i == at else 0 for i in range(64)]


def const(v):
    return [v & 0xffff] * 64


def nz(r, n=24):
    return [(i, x) for i, x in enumerate(r) if x][:n]


def plateau(r):
    z = [i for i, x in enumerate(r) if x]
    seg = r[z[0] + 4: z[0] + 20] if z else []
    return (sum(seg) / len(seg)) if seg else 0.0


# ---------------------------------------------------------------- mixer
def mixer(bar, log, mx, ins, out_gain=0x08):
    """Mixer mx alone on the rate, with inputs {input: gain} enabled (current = target)."""
    sp.w(bar, log, MIX_ACTIVE, bar.rd(MIX_ACTIVE) & ~(1 << sp.RATE), 'mixer rate off')
    for i in range(32):
        for a in (MIX_IN_T, MIX_IN_C):
            bar.wr(a + ((mx << 5) + i) * 4, ins.get(i, 0x80))
    for a in (MIX_OUT_T, MIX_OUT_C):
        bar.wr(a + mx * 4, out_gain)
    for i in range(8):
        bar.wr(MIX_EN + ((mx << 3) + i) * 4, 0)
    for i in ins:
        a = MIX_EN + ((mx << 3) + (i >> 2)) * 4
        bar.wr(a, bar.rd(a) | (1 << (i & 3)))
    sp.w(bar, log, MIX_HDR + 4 * sp.RATE, mx, f'mixer work-to-do: mixer {mx}, no next')
    sp.w(bar, log, MIX_ACTIVE, bar.rd(MIX_ACTIVE) | (1 << sp.RATE), 'mixer rate on')


def mixer_off(bar, log):
    sp.w(bar, log, MIX_ACTIVE, bar.rd(MIX_ACTIVE) & ~(1 << sp.RATE), 'mixer rate off')
    sp.w(bar, log, MIX_HDR + 4 * sp.RATE, 0, 'mixer work-to-do: none')


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd not in ('xtdelay', 'eqgate', 'mix', 'clear'):
        sys.exit(__doc__)
    if sp.vm_running():
        sys.exit('a QEMU process is running; stop the VM first (this needs the card idle)')
    sp.wake()
    bar = sp.Bar()
    if bar.rd(0x2a00c) == 0xffffffff:
        sys.exit('the card reads 0xffffffff; see srcprobe.py')
    log = sp.Log(sp.arg('--log', 'restprobe-log.jsonl', str))
    log(event='start', cmd=cmd, argv=sys.argv[1:], chip_ctrl=hex(bar.rd(0x2a00c)))
    if bar.rd(0x2a00c) & (1 << 21):
        sp.release(bar, log)
    quiet = sp.Log(os.devnull)
    import numpy as np
    rows, names = [], []

    def keep(name, r, **kw):
        log(event='case', n=len(names), case=name, level=round(plateau(r), 2), first=nz(r), **kw)
        rows.append((len(names), *((r + [0] * 160)[:160])))
        names.append(name)

    if cmd == 'xtdelay':
        # Run 1 wrote the control word as the driver does in headphone mode (0xc0840088, then
        # 0xd0840089, with the delays OR'd in) and the cross copy came out with no delay at any
        # setting. In satellite mode the driver writes 0xa001ad00, 0xd001ad88, 0xc001ad89: no
        # bits 23 and 18. Both forms here.
        def ctl_sat(d):
            def f():
                sp.w(bar, log, xp.CTRL, 0xa0000000 | d, 'xt control: off, delays (satellite form)')
                sp.w(bar, log, xp.CTRL, 0xd0000088 | d, 'xt control: rate 17 (satellite form)')
                sp.w(bar, log, xp.CTRL, 0xc0000089 | d, 'xt control: enable (satellite form)')
            return f
        for form in ('satellite', 'headphone'):
            for dl, dr in ((0, 0), (1, 0), (5, 0), (13, 0), (31, 0), (0, 7), (0, 13)):
                K = dict(k={'eql': 0x4000, 'eqr': 0x4000, 'xtl': 0x2000, 'xtr': 0x2000}, gin={0: 0x7fff, 5: 0x7fff}, dl=dl, dr=dr)
                d = ((dl & 0x1f) << 13) | ((dr & 0x1f) << 8)
                setup = (lambda K=K, d=d: (xp.xt_setup(bar, quiet, **K), ctl_sat(d)())) if form == 'satellite' else (lambda K=K: xp.xt_setup(bar, quiet, **K))
                for side, out in (('right', 0x67), ('left', 0x66)):
                    xp.zero_inputs(bar, quiet)
                    pairs = [(F, 0x96), (FIFO2, 0x9b), (out, S)]
                    r = run(bar, quiet, pairs, impulse(), setup, pat2=impulse())
                    keep(f'{form} form, delays L {dl} R {dr}, {side} output', r, dl=dl, dr=dr, side=side, form=form,
                         ctl=hex(bar.rd(xp.CTRL)))
        # the two FIFOs alone into the same output, to check they start together
        xp.zero_inputs(bar, quiet)
        r = run(bar, quiet, [(F, 0x96), (FIFO2, 0x97), (0x66, S)], impulse(0x4000, 8),
                lambda: xp.xt_setup(bar, quiet, k={'eql': 0x4000}, gin={0: 0x7fff, 1: 0x7fff}), pat2=impulse(0x2000, 12))
        keep('alignment: FIFO 0 impulse at 8, FIFO 2 half-size at 12, same side', r)
    elif cmd == 'eqgate':
        bands = lambda byp: lambda: (eq_set(bar, log, byp, 0x3e96), log(event='eq_regs', regs={hex(a): hex(bar.rd(a)) for a in range(0x2b3c0, 0x2b450, 4)}))
        for byp in (0, 1, 0x7fff):
            for how in ('direct', 'via mixer 6'):
                for pname, pat in (('impulse', impulse()), ('const', const(0x2000))):
                    if how == 'direct':
                        pairs = [(F, EQ_IN), (EQ_OUT, S)]
                        setup = bands(byp)
                    else:
                        pairs = [(F, 0x51), (0x36, EQ_IN), (EQ_OUT, S)]
                        setup = (lambda b: lambda: (mixer(bar, quiet, 6, {1: 0x08}), bands(b)()))(byp)
                    r = run(bar, quiet, pairs, pat, setup)
                    if how != 'direct':
                        mixer_off(bar, quiet)
                    keep(f'EQ bands on, bypass {byp:#06x}, {how}, {pname}', r)
    elif cmd == 'mix':
        pairs = [(F, 0x50), (0x30, S)]
        for g in (0x00, 0x04, 0x07, 0x08, 0x0c, 0x10, 0x1f):
            for v in (0x1000, 1, 2, 3, 5, 7, -1, -2, -3, 0x7fff, -0x8000):
                r = run(bar, quiet, pairs, const(v), lambda: mixer(bar, quiet, 0, {0: g}))
                mixer_off(bar, quiet)
                keep(f'in gain {g:#04x}, value {v}', r, gain=g, value=v)
        for og in (0x00, 0x04, 0x0c, 0x10):
            r = run(bar, quiet, pairs, const(0x1000), lambda: mixer(bar, quiet, 0, {0: 0x08}, out_gain=og))
            mixer_off(bar, quiet)
            keep(f'out gain {og:#04x}, value 4096', r, out_gain=og)
        for a, b in ((0x6000, 0x6000), (-0x6000, -0x6000), (0x4000, -0x2000), (3, 3)):
            r = run(bar, quiet, [(F, 0x50), (FIFO2, 0x51), (0x30, S)], const(a), lambda: mixer(bar, quiet, 0, {0: 0x08, 1: 0x08}), pat2=const(b))
            mixer_off(bar, quiet)
            keep(f'two inputs {a} + {b}', r, a=a, b=b)
    elif cmd == 'clear':
        mixer_off(bar, log)
        fifo2_off(bar, log)
        sp.clear(bar, log)
    if rows:
        path = sp.arg('--out', f'restprobe-{cmd}.npz', str)
        np.savez(path, rows=np.array(rows), names=np.array(names))
        log(event='saved', path=path)


def eq_set(bar, log, bypass, band):
    for a in range(0x2b000, 0x2b450, 4):
        if a not in (0x2b440,):
            bar.wr(a, 0)
    snap = {int(a, 16): int(v, 16) for a, v in __import__('json').load(open(os.path.join(here, '..', 'data', 'path-snapshot-run4.json')))['eq'].items()}
    for a, v in snap.items():
        bar.wr(a, v)
    for side in (0x2b000, 0x2b1e0):
        for b in range(10):
            bar.wr(side + b * 0x30 + 0x28, band)
            bar.wr(side + b * 0x30 + 0x2c, band)
    for a in (0x2b3d4, 0x2b3ec, 0x2b3d0, 0x2b3e8):
        bar.wr(a, bypass)


if __name__ == '__main__':
    main()
