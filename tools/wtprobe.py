#!/usr/bin/env python3
"""Measure a wavetable voice's pan and send law on an idle card, from the Linux host.

Run on the X79 host with the card bound to vfio-pci and no VM running, like srcprobe.py.

  sudo ./wtprobe.py start [--log FILE]       stage 1: start one voice on a hand-filled FIFO and
                                             watch whether it consumes it (FIFO pointers, status)
  sudo ./wtprobe.py bus  [--pan P] [--rev R] [--cho C] [--log FILE]
                                             stage 2: one setting, the four buses (left, right,
                                             reverb, chorus) read through a converter
  sudo ./wtprobe.py sweep [--out FILE.npz] [--log FILE]
                                             stage 3: pan 0..0x7f and both sends 0..0x7f, every
                                             bus each time (about a minute)
  sudo ./wtprobe.py levels [--log FILE]      the direct-stream pair (DSREG set) against the main
                                             pair, at two input levels, and the outputs with the
                                             voice silent or stopped (about 20 s)
  sudo ./wtprobe.py gains [--out FILE.npz]  the gain byte and the input gain byte on the direct-
                                             stream pair, the pair's pan, and the main pair with the
                                             other voices muted (about 15 s)
  sudo ./wtprobe.py state                    the gain-state field, the filter byte and the input level
                                             on the direct-stream pair (about 15 s)
  sudo ./wtprobe.py mix                      the same constant from an ADB FIFO and from the voice,
                                             straight to the converter and through mixer 0 at unity
                                             (about 10 s)
  sudo ./wtprobe.py own                      the voice's own ADB source (the A3D-input route) against
                                             its input, with DSREG, pan and gain varied (about 10 s)
  sudo ./wtprobe.py clear

The chain: a second-half voice (global 40, local 8) with DSREG clear, so it plays onto its half's
main pair (outputs 34/35, ADB sources 0xa2/0xa3) and its reverb and chorus sends (36/37, 0xa4/
0xa5), the path Half-Life pans with. Its 64-word FIFO is filled by hand with a constant (0x4000),
so the voice plays DC at gain 0xcf (unity) with the filter open and the rate at 48 kHz (0xa000).
One bus at a time is routed into converter 0 (ratio 1, time fraction 0, which passes samples
through unchanged; srcprobe), whose output double buffer is read as the voice plays. The level on
each bus against PARM3's pan (bits 20-14), chorus send (13-7) and reverb send (6-0) gives the law
and which output is which.

Voice set-up follows the Windows driver's start of a voice as traced (reference: Wavetable
engine): the half's control word 0x1aa3, DSREG bit clear, the pair-sync bit clear, PARM0-3, the
running flag at 0x8180 + voice x 4 written 0, the DMA control word 0x22000 (signed 16-bit), and
the FIFO control word valid with its DMA pointer ahead of its read pointer, as for the VDB FIFOs
in srcprobe. Every write is logged with its readback.
"""
import importlib.util
import os
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('srcprobe', os.path.join(here, 'srcprobe.py'))
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)

VOICE = 40                                   # global voice: second half, local voice 8
LV = VOICE - 32
BANK = 0x8000
WT_CTRL, WT_SRAMP, WT_DSREG = BANK + 0x0, BANK + 0x4, BANK + 0x8
WT_SYNC = BANK + 0x80 + (LV // 2) * 4
WT_RUN = BANK + 0x180 + LV * 4
WT_PARM = BANK + 0x200 + LV * 0x10
WT_FIFO_DATA = 0x10000 + VOICE * 0x100
WT_FIFO_PC = 0x16000 + VOICE * 4
WTDMA_CTRL = 0x27900 + VOICE * 4
WTDMA_STAT = 0x27d00 + VOICE * 4
BUSES = {'left': 0xa2, 'right': 0xa3, 'reverb': 0xa4, 'chorus': 0xa5}
ALL = {'ds_left': 0xa0, 'ds_right': 0xa1, **BUSES, 'own': 0x80 + LV}   # own: the voice's own ADB source, which the driver routes to A3D inputs
LEVEL = 0x4000


def parm3(pan, cho, rev):
    return (0xcf << 24) | (7 << 21) | ((pan & 0x7f) << 14) | ((cho & 0x7f) << 7) | (rev & 0x7f)


def mute_others(bar, log):
    """Hard-mute the half's other 31 voices (PARM3 gain state 0). On a cold card they hold
    whatever they powered up with; levels run 1 showed a component on the main pair and sends
    that stayed with our voice silent."""
    for lv in range(32):
        if lv != LV:
            bar.wr(BANK + 0x200 + lv * 0x10 + 12, 0)
    log(event='muted_others')


def voice_setup(bar, log, pan, cho, rev, ds=False, gain=0xcf, level=None, ingain=0xcf, state=7, filt=0xcf):
    w = sp.w
    w(bar, log, WT_FIFO_PC, 0, 'wt fifo: off')
    for i in range(64):
        lv = LEVEL if level is None else level
        bar.wr(WT_FIFO_DATA + 4 * i, ((lv & 0xffff) << 16) | (lv & 0xffff))   # both halves: steady whether a word holds one sample or two
    log(event='wt_preload', readback=[hex(bar.rd(WT_FIFO_DATA + 4 * i) & 0xffff) for i in range(4)])
    w(bar, log, WT_CTRL, 0x1aa3, 'half control (driver value)')
    w(bar, log, WT_SRAMP, 0x00830000, 'SRAMP (driver value)')
    if ds:
        w(bar, log, WT_DSREG, bar.rd(WT_DSREG) | (1 << LV), 'DSREG: voice set (direct-stream pair 32/33)')
    else:
        w(bar, log, WT_DSREG, bar.rd(WT_DSREG) & ~(1 << LV), 'DSREG: voice clear (main pair and sends)')
    w(bar, log, WT_SYNC, 0, 'pair sync off')
    w(bar, log, WTDMA_CTRL, 0x22000, 'wt dma: signed 16-bit')
    w(bar, log, WT_PARM + 0, (filt << 24) | 0xa000 | (gain << 16), 'PARM0: filter open, gain, 48 kHz (direct pair: left gain)')
    w(bar, log, WT_PARM + 4, (filt << 24) | 0xa001 | (gain << 16), 'PARM1: same, state bit differing (direct pair: right gain)')
    w(bar, log, WT_PARM + 8, 0, 'PARM2: no feedback, phase 0')
    w(bar, log, WT_PARM + 12, (parm3(pan, cho, rev) & 0x001fffff) | (state << 21) | (ingain << 24), f'PARM3: input gain {ingain:#04x} pan {pan:#04x} chorus {cho:#04x} reverb {rev:#04x}')
    w(bar, log, WT_RUN, 0, 'running flag (driver writes 0)')


def voice_go(bar):
    bar.wr(WT_FIFO_PC, (63 << 12) | sp.PC_VALID)      # valid, DMA pointer 63 ahead: the FIFO is full


def voice_stop(bar, log):
    sp.w(bar, log, WT_FIFO_PC, 0, 'wt fifo: off')
    sp.w(bar, log, WT_PARM + 12, 0, 'PARM3: hard mute')


def capture_bus(bar, log, bus, pan, cho, rev, ds=False, gain=0xcf, level=None, go=True, ingain=0xcf, state=7, filt=0xcf):
    sp.SRC_FEED = ALL[bus]
    sp.setup(bar, log, 0, 13, 0x4000, pattern=[0] * 64)
    voice_setup(bar, log, pan, cho, rev, ds=ds, gain=gain, level=level, ingain=ingain, state=state, filt=filt)
    if go:
        voice_go(bar)
    r, _, _ = sp.flush_and_read(bar, log, flush=False)
    voice_stop(bar, log)
    sp.clear(bar, log)
    # the level: mean over the settled part (run 1 alternated sample by sample with the FIFO's
    # words holding the level in one half only, so take even and odd samples apart as well)
    nz = [i for i, x in enumerate(r) if x]
    seg = r[nz[0] + 8: nz[0] + 72] if nz else []
    if len(seg) < 16:
        return (0.0, 0.0, 0.0), r
    ev, od = sum(seg[0::2]) / len(seg[0::2]), sum(seg[1::2]) / len(seg[1::2])
    return ((ev + od) / 2, ev, od), r


# ---------------------------------------------------------------- mixer comparison
MIX_HDR = 0x21e40                            # + rate * 4: first mixer on the rate's work-to-do list
MIX_NEXT = 0x21e00                           # + mixer * 4: next mixer (bit 4 valid)
MIX_ACTIVE = 0x21f00                         # bit per rate (CAsp4Mixer::EnableSampleRate)
MIX_INVOL_A, MIX_INVOL_B = 0x21000, 0x20000  # target, current: + ((mix << 5) + in) * 4
MIX_VOL_A, MIX_VOL_B = 0x21800, 0x20800      # target, current: + mix * 4
MIX_ENIN = 0x21a00                           # + ((mix << 3) + (in >> 2)) * 4, bit in & 3
MIXER, MIXIN = 0, 0


def mix_route(bar, log, feed, in_gain=0x08, out_gain=0x08):
    """feed -> mixer 0 input 0 -> mixer 0 out -> converter 0, all on the converter's rate.
    Called after sp.setup, which has set up the converter and its link to the output."""
    w, R_ = sp.w, sp.RATE
    w(bar, log, MIX_ACTIVE, bar.rd(MIX_ACTIVE) & ~(1 << R_), 'mixer rate off')
    w(bar, log, sp.VDB_WTDRAM_HEADERLINKS + 4 * R_, (feed << 8) | (0x50 + MIXIN), f'vdb hdr: {feed:#04x} -> mixer input')
    w(bar, log, sp.VDB_WTDRAM_DESTLINKS + 4 * (0x50 + MIXIN), ((0x30 + MIXER) << 8) | sp.SRC_DSTADDR, 'vdb link: mixer out -> SRC in')
    for a in (MIX_INVOL_A, MIX_INVOL_B):
        for i in range(32):
            bar.wr(a + ((MIXER << 5) + i) * 4, 0x80)          # every input muted
        w(bar, log, a + ((MIXER << 5) + MIXIN) * 4, in_gain, f'mixer input gain {in_gain:#04x}')
    for a in (MIX_VOL_A, MIX_VOL_B):
        w(bar, log, a + MIXER * 4, out_gain, f'mixer output gain {out_gain:#04x}')
    for i in range(8):
        bar.wr(MIX_ENIN + ((MIXER << 3) + i) * 4, 0)
    w(bar, log, MIX_ENIN + ((MIXER << 3) + (MIXIN >> 2)) * 4, 1 << (MIXIN & 3), 'mixer input enabled')
    w(bar, log, MIX_HDR + 4 * R_, MIXER, 'mixer work-to-do: mixer 0, no next (as CAsp4Mixer::AddWTD)')
    w(bar, log, MIX_ACTIVE, bar.rd(MIX_ACTIVE) | (1 << R_), 'mixer rate on')


def mix_clear(bar, log):
    sp.w(bar, log, MIX_ACTIVE, bar.rd(MIX_ACTIVE) & ~(1 << sp.RATE), 'mixer rate off')
    sp.w(bar, log, MIX_HDR + 4 * sp.RATE, 0, 'mixer work-to-do: none')
    sp.w(bar, log, sp.VDB_WTDRAM_DESTLINKS + 4 * (0x50 + MIXIN), sp.END, 'vdb link: none')


def capture_mix(bar, log, source, via_mixer, level=0x1000, in_gain=0x08):
    """source 'adb': VDB FIFO 0 preloaded with a constant; 'wt': the wavetable voice on the
    direct-stream pair (DSREG set, pan 0, gain 0xcf) playing the same constant."""
    feed = sp.FIFO_IN if source == 'adb' else ALL['ds_left']
    sp.SRC_FEED = feed
    sp.setup(bar, log, 0, 13, 0x4000, pattern=[level] * 64)
    if via_mixer:
        mix_route(bar, log, feed, in_gain=in_gain)
    voice_setup(bar, log, 0, 0x7f, 0x7f, ds=True, level=level, gain=0xcf if source == 'wt' else 0)
    voice_go(bar)
    r, _, _ = sp.flush_and_read(bar, log, flush=False)
    voice_stop(bar, log)
    if via_mixer:
        mix_clear(bar, log)
    sp.clear(bar, log)
    nz = [i for i, x in enumerate(r) if x]
    seg = r[nz[0] + 4: nz[0] + 24] if nz else []
    return (sum(seg) / len(seg) if seg else 0.0), r


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd not in ('start', 'bus', 'sweep', 'levels', 'gains', 'state', 'mix', 'own', 'clear'):
        sys.exit(__doc__)
    if sp.vm_running():
        sys.exit('a QEMU process is running; stop the VM first (this needs the card idle)')
    sp.wake()
    bar = sp.Bar()
    if bar.rd(0x2a00c) == 0xffffffff:
        sys.exit('the card reads 0xffffffff; see srcprobe.py')
    log = sp.Log(sp.arg('--log', 'wtprobe-log.jsonl', str))
    log(event='start', cmd=cmd, argv=sys.argv[1:], chip_ctrl=hex(bar.rd(0x2a00c)))
    if bar.rd(0x2a00c) & (1 << 21):
        sp.release(bar, log)
    quiet = sp.Log(os.devnull)
    if cmd == 'start':
        voice_setup(bar, log, 0x3f, 0x7f, 0x7f)
        voice_go(bar)
        t0, seen, last = time.perf_counter(), [], None
        while time.perf_counter() - t0 < 0.01:
            cur = (bar.rd(WT_FIFO_PC), bar.rd(WTDMA_STAT), bar.rd(WT_RUN), bar.rd(WT_PARM + 8))
            if cur != last:
                seen.append({'t_us': round((time.perf_counter() - t0) * 1e6), 'fifo_pc': hex(cur[0]),
                             'dma_stat': hex(cur[1]), 'run': hex(cur[2]), 'parm2': hex(cur[3])})
                last = cur
        log(event='watch', changes=seen[:200])
        voice_stop(bar, log)
    elif cmd == 'bus':
        pan, rev, cho = sp.arg('--pan', 0x3f), sp.arg('--rev', 0x7f), sp.arg('--cho', 0x7f)
        for bus in BUSES:
            plateau, r = capture_bus(bar, quiet, bus, pan, cho, rev)
            log(event='bus', bus=bus, pan=pan, rev=rev, cho=cho, level=plateau,
                nonzero=[(i, x) for i, x in enumerate(r) if x][:40])
    elif cmd == 'sweep':
        import numpy as np
        settings = []
        for p in range(0, 0x80, 4):
            settings.append((p, 0x7f, 0x7f))
        settings.append((0x7f, 0x7f, 0x7f))
        for v in list(range(0, 0x80, 4)) + [0x7f]:
            settings.append((0x3f, 0x7f, v))          # chorus send held, reverb stepped
            settings.append((0x3f, v, 0x7f))          # reverb held, chorus stepped
        rows = []
        for pan, cho, rev in settings:
            row = []
            for bus in BUSES:
                plateau, _ = capture_bus(bar, quiet, bus, pan, cho, rev)
                row.append(plateau)
            rows.append((pan, cho, rev, *[x[0] for x in row], *[x[1] for x in row], *[x[2] for x in row]))
            log(event='setting', pan=pan, cho=cho, rev=rev, **{b: [round(v, 1) for v in x] for b, x in zip(BUSES, row)})
        path = sp.arg('--out', 'wtprobe-sweep.npz', str)
        np.savez(path, rows=np.array(rows), cols=np.array(['pan', 'cho', 'rev', *BUSES, *[b + '_even' for b in BUSES], *[b + '_odd' for b in BUSES]]), level=LEVEL)
        log(event='saved', path=path, settings=len(rows))
    elif cmd == 'levels':
        # the direct-stream pair against the main pair, and the alternating component on its own:
        # each case on all six outputs, three takes
        cases = [('main, pan 0, sends full, 0x4000', dict(ds=False)),
                 ('main, pan 0, sends full, 0x2000', dict(ds=False, level=0x2000)),
                 ('direct (DSREG set), 0x4000', dict(ds=True)),
                 ('direct (DSREG set), 0x2000', dict(ds=True, level=0x2000)),
                 ('main, voice gain 0', dict(ds=False, gain=0)),
                 ('direct, voice gain 0', dict(ds=True, gain=0)),
                 ('voice not started', dict(ds=False, go=False))]
        for name, kw in cases:
            for bus in ALL:
                takes = [capture_bus(bar, quiet, bus, 0, 0x7f, 0x7f, **kw)[0] for _ in range(3)]
                log(event='level', case=name, bus=bus, takes=[[round(v, 1) for v in t] for t in takes])
    elif cmd == 'gains':
        # levels run 1: on the direct-stream pair (clean, no other component) a voice at gain 0xcf
        # and pan 0 gave 26003 for a 16384 input, 0.79 of the x2 the headroom arithmetic gives.
        # The gain byte and the input gain byte, each stepped with the other at 0xcf; pan 0 and
        # pan 0x3f; the other voices muted first, then the main pair checked for the component.
        import numpy as np
        mute_others(bar, log)
        rows = []
        codes = [0xcf, 0xce, 0xcc, 0xc8, 0xc0, 0xbf, 0xb8, 0xb0, 0xaf, 0xa0, 0x9f, 0x90, 0x80, 0x70, 0x60, 0x40, 0x20, 0x10, 0x08, 0x01, 0x00,
                 0xd0, 0xdf, 0xe0, 0xef, 0xff]
        for which in ('gain', 'ingain'):
            for code in codes:
                kw = {which: code}
                lv, r = capture_bus(bar, quiet, 'ds_left', 0, 0x7f, 0x7f, ds=True, level=0x1000, **kw)
                rows.append((0 if which == 'gain' else 1, code, 0, *lv))
                log(event='gain', which=which, code=hex(code), level=[round(v, 1) for v in lv])
        for pan in (0, 0x3f, 0x40, 0x7f):
            for bus in ('ds_left', 'ds_right'):
                lv, r = capture_bus(bar, quiet, bus, pan, 0x7f, 0x7f, ds=True, level=0x1000)
                rows.append((2, pan, 0 if bus == 'ds_left' else 1, *lv))
                log(event='ds_pan', pan=hex(pan), bus=bus, level=[round(v, 1) for v in lv])
        for bus in BUSES:
            lv, r = capture_bus(bar, quiet, bus, 0x3f, 0x7f, 0x7f, gain=0)
            log(event='others_muted', bus=bus, level=[round(v, 1) for v in lv])
        path = sp.arg('--out', 'wtprobe-gains.npz', str)
        np.savez(path, rows=np.array(rows), input=0x1000)
        log(event='saved', path=path)
    elif cmd == 'state':
        # gains run: the gain bytes decode exactly as modelled, and the pan is (127 - p) / 127 and
        # p / 127, but a full voice comes out at 1.587 x its input, not the x2 the headroom gives.
        # Candidates: the gain-state field (PARM3 bits 23-21, 7 = normal) and the filter byte.
        mute_others(bar, log)
        for st in range(8):
            lv, _ = capture_bus(bar, quiet, 'ds_left', 0, 0x7f, 0x7f, ds=True, level=0x1000, state=st)
            log(event='state', state=st, level=[round(v, 1) for v in lv])
            # a ramping state would not settle: keep the raw run too
            _, r = capture_bus(bar, quiet, 'ds_left', 0, 0x7f, 0x7f, ds=True, level=0x1000, state=st)
            log(event='state_raw', state=st, samples=r[:150])
        for f in (0xcf, 0xd0, 0xdf, 0xe0, 0xef, 0xff, 0xce, 0xc0, 0xb0):
            lv, _ = capture_bus(bar, quiet, 'ds_left', 0, 0x7f, 0x7f, ds=True, level=0x1000, filt=f)
            log(event='filter', code=hex(f), level=[round(v, 1) for v in lv])
        for lvl in (0x0001, 0x0010, 0x0100, 0x1000, 0x2000, 0x4000, 0x7fff, -0x1000, -0x8000):
            lv, _ = capture_bus(bar, quiet, 'ds_left', 0, 0x7f, 0x7f, ds=True, level=lvl)
            log(event='input', level_in=lvl, level=[round(v, 1) for v in lv])
    elif cmd == 'mix':
        # run 16 found a direct-stream voice at 0xcf and centre pan as loud as the plain ADB path,
        # through the mixer to the codec; wtprobe finds the voice at 0.794 of x2 per side. The
        # same constant from an ADB FIFO and from the voice, each straight into the converter and
        # each through mixer 0 at unity in and out; the ADB FIFO holds only 32 samples, so the
        # level is taken over the first 20 after the response starts.
        mute_others(bar, log)
        for lvl in (0x1000, 0x2000):
            for source in ('adb', 'wt'):
                for via in (False, True):
                    takes = [capture_mix(bar, quiet, source, via, level=lvl) for _ in range(3)]
                    log(event='mix', source=source, via_mixer=via, level_in=lvl,
                        level=[round(t[0], 1) for t in takes], first=[x for x in takes[0][1] if x][:12])
        for g in (0x00, 0x04, 0x08, 0x0c, 0x10):
            lv, r = capture_mix(bar, quiet, 'adb', True, level=0x1000, in_gain=g)
            log(event='mix_gain', in_gain=hex(g), level=round(lv, 1))
    elif cmd == 'own':
        # a voice's own ADB source (0x80 + voice in the second half), the route the driver uses to
        # feed an A3D source from a wavetable voice when the ADB channels run out (run 11). The
        # level against the input, with DSREG set and clear and the pan at both ends and centre,
        # the gain byte at three codes, and the mixed pairs while it is routed.
        mute_others(bar, log)
        for ds in (True, False):
            for pan in (0, 0x3f, 0x7f):
                for lvl in (0x1000, 0x2000):
                    lv, r = capture_bus(bar, quiet, 'own', pan, 0x7f, 0x7f, ds=ds, level=lvl)
                    log(event='own', dsreg=ds, pan=hex(pan), level_in=lvl, level=[round(v, 1) for v in lv])
        for g in (0xcf, 0xc0, 0xa0):
            lv, r = capture_bus(bar, quiet, 'own', 0x3f, 0x7f, 0x7f, ds=False, level=0x1000, gain=g)
            log(event='own_gain', gain=hex(g), level=[round(v, 1) for v in lv])
    elif cmd == 'clear':
        mix_clear(bar, log)
        voice_stop(bar, log)
        sp.clear(bar, log)


if __name__ == '__main__':
    main()
