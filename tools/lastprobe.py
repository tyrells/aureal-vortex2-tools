#!/usr/bin/env python3
"""The last three open details of the chip, on an idle card, from the Linux host.

Run on the X79 host with the card bound to vfio-pci and no VM running, like srcprobe.py.

  sudo ./lastprobe.py interp [--out FILE.npz] [--log FILE]
        the wavetable voice's interpolator, read directly: a single-sample impulse in the voice's
        FIFO, played at a step of exactly 1 with the pitch phase (PARM2 bits 15-0) set to each
        of 64 fractions, read on the voice's own output. Each capture gives the kernel's weights
        at that fraction; together they are the whole kernel, which the 44.1 kHz image line
        (6 dB under the cubic model) depends on. Then the same impulse at steps of 1/64 and 1/16
        and at the driver's 44.1 kHz step (0x9acc), watched for 0.1 s, as a cross-check, and
        the impulse in the high half of a FIFO word instead of the low half.
  sudo ./lastprobe.py own [--out FILE.npz] [--log FILE]
        where the voice's 0.7998 comes from: 192 different sample values (1 LSB to full scale,
        both signs) through the voice's own output at step 1, phase 0, gain and input gain
        0xcf, so the factor and its rounding can be fitted value by value; and the same at
        gains 0xc0 and 0xbf and filter bytes 0xcf and 0xff, to see which stage it rides on.
  sudo ./lastprobe.py eq [--out FILE.npz] [--log FILE]
        the EQ's gate: the bypass and the band gains each set as current/target pairs
        (0/0, gliding up 0 -> 0x7fff, gliding down 0x7fff -> 0, held), with the EQ's two time
        constants as the driver writes them (0x2b3c4 = 0xc, 0x2b3c8 = 0x7fe0) and at 0, fed
        through mixer 6 (impulse and constant), the EQ's registers logged before and after.
        pathprobe run 1 heard the bands with the bypass at 0 while the band gains were gliding
        down; restprobe heard silence with the bypass at 0 and the bands held.
  sudo ./lastprobe.py clear
"""
import importlib.util
import json
import os
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(here, name + '.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sp = load('srcprobe')
wp = load('wtprobe')
rp = load('restprobe')
wp.sp = sp
rp.sp = sp
rp.xp.sp = sp

OWN = wp.ALL['own']
P0, P1, P2, P3 = wp.WT_PARM, wp.WT_PARM + 4, wp.WT_PARM + 8, wp.WT_PARM + 12


def voice(bar, log, words, rate=0xa000, phase=0, gain=0xcf, filt=0xcf):
    """The voice on its own output, FIFO words as given (low half = first sample)."""
    wp.voice_setup(bar, log, 0, 0, 0, ds=True, gain=gain, level=0, filt=filt)
    for i in range(64):
        bar.wr(wp.WT_FIFO_DATA + 4 * i, words[i] if i < len(words) else 0)
    bar.wr(P0, (filt << 24) | (gain << 16) | (rate & 0xfffe))
    bar.wr(P1, (filt << 24) | (gain << 16) | (rate & 0xfffe) | 1)
    bar.wr(P2, phase & 0xffff)


def capture(bar, log, words, long=0.0, **kw):
    sp.SRC_FEED = OWN
    sp.setup(bar, log, 0, 13, 0x4000, pattern=[0] * 64)
    voice(bar, log, words, **kw)
    wp.voice_go(bar)
    if long:
        out, _, _ = wp.long_capture(bar, long, 0.0, lambda: None)
        # rebuild sample indices from the converter's read pointer (8 bits, one step a sample)
        r, last, idx = [], None, 0
        for t, ptr, v in out:
            if last is not None:
                idx += (ptr - last) & 0xff
            last = ptr
            r.append((idx, v))
        res = [0] * (idx + 1)
        for i, v in r:
            res[i] = v
    else:
        res, _, _ = sp.flush_and_read(bar, log, flush=False)
    wp.voice_stop(bar, log)
    sp.clear(bar, log)
    return res


def nz(r, n=24):
    return [(i, x) for i, x in enumerate(r) if x][:n]


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd not in ('interp', 'own', 'eq', 'clear'):
        sys.exit(__doc__)
    if sp.vm_running():
        sys.exit('a QEMU process is running; stop the VM first (this needs the card idle)')
    sp.wake()
    bar = sp.Bar()
    if bar.rd(0x2a00c) == 0xffffffff:
        sys.exit('the card reads 0xffffffff; see srcprobe.py')
    log = sp.Log(sp.arg('--log', 'lastprobe-log.jsonl', str))
    log(event='start', cmd=cmd, argv=sys.argv[1:], chip_ctrl=hex(bar.rd(0x2a00c)))
    if bar.rd(0x2a00c) & (1 << 21):
        sp.release(bar, log)
    quiet = sp.Log(os.devnull)
    import numpy as np
    rows, names = [], []

    def keep(name, r, n=200, **kw):
        log(event='case', n=len(names), case=name, first=nz(r), **kw)
        rows.append((len(names), *((list(r) + [0] * n)[:n])))
        names.append(name)

    if cmd == 'interp':
        wp.mute_others(bar, log)
        imp = [0] * 64
        imp[8] = 0x4000                                  # low half of word 8: one sample of 16,384
        for ph in range(0, 0x10000, 0x400):
            r = capture(bar, quiet, imp, phase=ph)
            keep(f'impulse low half, step 1, phase {ph:#06x}', r, phase=ph)
        imp_hi = [0] * 64
        imp_hi[8] = 0x4000 << 16
        keep('impulse high half, step 1, phase 0', capture(bar, quiet, imp_hi))
        for rate, name in ((0x0800, '1/64'), (0x2000, '1/16'), (0x9acc, '44.1 kHz (0x9acc)')):
            r = capture(bar, quiet, imp, long=0.1, rate=rate)
            keep(f'impulse low half, step {name}, 0.1 s', r, n=4800, rate=hex(rate))
    elif cmd == 'own':
        wp.mute_others(bar, log)
        vals = []
        for k in range(3):
            vs = []
            for i in range(64):
                j = k * 64 + i
                mag = int(round(1.0 * (32767 ** (j / 191.0))))      # 1 to 32767, log-spaced
                vs.append(mag if (j % 2 == 0) else -mag)
            vals.append(vs)
        for g, f in ((0xcf, 0xcf), (0xc0, 0xcf), (0xbf, 0xcf), (0xcf, 0xff)):
            for k, vs in enumerate(vals):
                words = [((v & 0xffff) << 16) | (v & 0xffff) for v in vs]   # each value twice
                r = capture(bar, quiet, words, gain=g, filt=f)
                keep(f'values set {k}, gain {g:#04x}, filter {f:#04x}', r, values=vs, gain=g, filt=f)
    elif cmd == 'eq':
        pairs = [(rp.F, 0x51), (0x36, rp.EQ_IN), (rp.EQ_OUT, rp.S)]
        snap = {int(a, 16): int(v, 16) for a, v in json.load(open(os.path.join(here, '..', 'data', 'path-snapshot-run4.json')))['eq'].items()}

        def eq(byp, band, tc):
            def f():
                rp.mixer(bar, quiet, 6, {1: 0x08})
                for a in range(0x2b000, 0x2b440, 4):
                    bar.wr(a, 0)
                for a, v in snap.items():
                    if a < 0x2b440:
                        bar.wr(a, v)
                for side in (0x2b000, 0x2b1e0):
                    for b in range(10):
                        bar.wr(side + b * 0x30 + 0x28, band[0])     # current
                        bar.wr(side + b * 0x30 + 0x2c, band[1])     # target
                bar.wr(0x2b3d0, byp[0]); bar.wr(0x2b3e8, byp[0])    # bypass current
                bar.wr(0x2b3d4, byp[1]); bar.wr(0x2b3ec, byp[1])    # bypass target
                if tc is not None:
                    bar.wr(0x2b3c4, tc[0]); bar.wr(0x2b3c8, tc[1])
                log(event='eq_regs_before', byp=[hex(x) for x in byp], band=[hex(x) for x in band], tc=tc,
                    regs={hex(a): hex(bar.rd(a)) for a in (0x2b028, 0x2b02c, 0x2b0e8, 0x2b0ec, 0x2b3c4, 0x2b3c8, 0x2b3d0, 0x2b3d4, 0x2b3dc, 0x2b3e0, 0x2b440)})
            return f
        BYP = {'0/0': (0, 0), 'up 0->7fff': (0, 0x7fff), 'down 7fff->0': (0x7fff, 0), 'held 7fff': (0x7fff, 0x7fff)}
        BAND = {'0/0': (0, 0), 'down 3e96->0': (0x3e96, 0), 'up 0->3e96': (0, 0x3e96), 'held 3e96': (0x3e96, 0x3e96)}
        for tcn, tc in (('driver tc', None), ('tc 0', (0, 0))):
            for bn, byp in BYP.items():
                for gn, band in BAND.items():
                    if bn == '0/0' and gn == '0/0':
                        continue
                    for pn, pat in (('impulse', rp.impulse()), ('const', rp.const(0x2000))):
                        r = rp.run(bar, quiet, pairs, pat, eq(byp, band, tc))
                        rp.mixer_off(bar, quiet)
                        keep(f'bypass {bn}, bands {gn}, {tcn}, {pn}', r, n=160)
                        log(event='eq_regs_after', regs={hex(a): hex(bar.rd(a)) for a in (0x2b028, 0x2b0e8, 0x2b3d0, 0x2b3dc)})
    elif cmd == 'clear':
        wp.voice_stop(bar, log)
        rp.mixer_off(bar, log)
        sp.clear(bar, log)
    if rows:
        path = sp.arg('--out', f'lastprobe-{cmd}.npz', str)
        L = max(len(x) for x in rows)
        np.savez(path, rows=np.array([list(x) + [0] * (L - len(x)) for x in rows]), names=np.array(names))
        log(event='saved', path=path)


if __name__ == '__main__':
    main()
