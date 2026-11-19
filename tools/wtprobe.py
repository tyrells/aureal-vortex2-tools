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
  sudo ./wtprobe.py reso [--out FILE.npz]   the filter's feedback (resonance) word: impulse and step
                                             responses at four cut-offs and 16 feedback values
                                             (about 30 s)
  sudo ./wtprobe.py ramp [--out FILE.npz]   the attack and smooth-mute ramps (Aureal's description, relayed by
                                             the map's author): a smooth mute at five mute-ramp settings, then an
                                             attack from hard mute at each candidate state code, slope and attack-
                                             ramp setting, each watched for 0.4 s (about a minute)
  sudo ./wtprobe.py filtrate [--out FILE.npz] the low-pass against the voice's half and playback rate:
                                             impulses through four cut-offs at 48, 44.1 and 22.05 kHz
                                             rates, on second-half voice 8 and first-half voice 0
                                             (about 10 s)
  sudo ./wtprobe.py sendfilt [--out FILE.npz] the low-pass on each path (direct-stream, main pair, both
                                             sends): a full-scale square wave through cut-offs 0xa1,
                                             0xb1 and open, at 48, 44.1 and 12 kHz rates, on first-half
                                             voice 0 and second-half voice 8 (about 20 s)
  sudo ./wtprobe.py parmpoke [--out FILE.npz] whether rewriting PARM0/PARM1 (as the driver does to the
                                             reflection voices every 2.4 ms) disturbs the low-pass:
                                             a pulse through cut-off 0xa1 with the words rewritten
                                             once or continually during the capture (about 5 s)
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
import json
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


def set_voice(v):
    """Point the probe at another voice (global number 0-63): the reflection voices are first-half
    voices 0-3, the voice measured so far second-half voice 8 (global 40)."""
    global VOICE, LV, BANK, WT_CTRL, WT_SRAMP, WT_DSREG, WT_SYNC, WT_RUN, WT_PARM, WT_FIFO_DATA
    global WT_FIFO_PC, WTDMA_CTRL, WTDMA_STAT, BUSES, ALL
    VOICE, LV, BANK = v, v % 32, (v // 32) * 0x8000
    WT_CTRL, WT_SRAMP, WT_DSREG = BANK + 0x0, BANK + 0x4, BANK + 0x8
    WT_SYNC = BANK + 0x80 + (LV // 2) * 4
    WT_RUN = BANK + 0x180 + LV * 4
    WT_PARM = BANK + 0x200 + LV * 0x10
    WT_FIFO_DATA = 0x10000 + VOICE * 0x100
    WT_FIFO_PC = 0x16000 + VOICE * 4
    WTDMA_CTRL = 0x27900 + VOICE * 4
    WTDMA_STAT = 0x27d00 + VOICE * 4
    out = 0xa0 if v >= 32 else 0x60                  # first output of the half: 32/33, 34/35, 36/37
    BUSES = {'left': out + 2, 'right': out + 3, 'reverb': out + 4, 'chorus': out + 5}
    ALL = {'ds_left': out, 'ds_right': out + 1, **BUSES, 'own': (0x80 if v >= 32 else 0x40) + LV}


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


def voice_setup(bar, log, pan, cho, rev, ds=False, gain=0xcf, level=None, ingain=0xcf, state=7, filt=0xcf,
                pattern=None, fb=0, rate=0xa000):
    w = sp.w
    w(bar, log, WT_FIFO_PC, 0, 'wt fifo: off')
    for i in range(64):
        lv = (pattern[i] if pattern else LEVEL) if level is None else level
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
    w(bar, log, WT_PARM + 0, (filt << 24) | (rate & 0xfffe) | (gain << 16), f'PARM0: filter {filt:#04x}, gain, rate {rate:#06x} (direct pair: left gain)')
    w(bar, log, WT_PARM + 4, (filt << 24) | (rate & 0xfffe) | 1 | (gain << 16), 'PARM1: same, state bit differing (direct pair: right gain)')
    w(bar, log, WT_PARM + 8, (fb & 0xffff) << 16, f'PARM2: feedback {fb:#06x}, phase 0')
    w(bar, log, WT_PARM + 12, (parm3(pan, cho, rev) & 0x001fffff) | (state << 21) | (ingain << 24), f'PARM3: input gain {ingain:#04x} pan {pan:#04x} chorus {cho:#04x} reverb {rev:#04x}')
    w(bar, log, WT_RUN, 0, 'running flag (driver writes 0)')


def voice_go(bar):
    bar.wr(WT_FIFO_PC, (63 << 12) | sp.PC_VALID)      # valid, DMA pointer 63 ahead: the FIFO is full


def voice_stop(bar, log):
    sp.w(bar, log, WT_FIFO_PC, 0, 'wt fifo: off')
    sp.w(bar, log, WT_PARM + 12, 0, 'PARM3: hard mute')


def capture_bus(bar, log, bus, pan, cho, rev, ds=False, gain=0xcf, level=None, go=True, ingain=0xcf, state=7, filt=0xcf,
                pattern=None, fb=0, rate=0xa000, poke=None, poke_at=None):
    sp.SRC_FEED = ALL[bus]
    sp.setup(bar, log, 0, 13, 0x4000, pattern=[0] * 64)
    voice_setup(bar, log, pan, cho, rev, ds=ds, gain=gain, level=level, ingain=ingain, state=state, filt=filt,
                pattern=pattern, fb=fb, rate=rate)
    if go:
        voice_go(bar)
    r, _, _ = sp.flush_and_read(bar, log, flush=False, poke=poke, poke_at=poke_at)
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


# ---------------------------------------------------------------- ramps
WT_MRAMP, WT_GMODE, WT_ARAMP = BANK + 0xc, BANK + 0x10, BANK + 0x14
WT_MUTE = BANK + 0x100 + LV * 4               # ALSA: one word per voice; the description: one bit per voice
DRV_MRAMP, DRV_ARAMP = 0x10000000, 0x00830000  # the driver's values, written once at start-up


def long_capture(bar, dur, trig_at, trigger):
    """Watch the converter's output and the voice's registers for dur seconds; call trigger()
    trig_at seconds in. The voice and the chain must already be set up; the VDB rate is enabled
    here. Output samples are rebuilt from the double buffer as in srcprobe.flush_and_read, each
    with the time it was seen (a few may be missed when the host is busy; the times show where)."""
    k = sp.SRC_K
    PAR6, DB0, DB1 = sp.SRC_CHANNEL_PARAMS[6] + 4 * k, sp.SRC_OUTPUT_DOUBLE_BUFFER + 8 * k, sp.SRC_OUTPUT_DOUBLE_BUFFER + 8 * k + 4
    watch = {'parm0': WT_PARM, 'parm1': WT_PARM + 4, 'parm2': WT_PARM + 8, 'parm3': WT_PARM + 12,
             'mute': WT_MUTE, 'mute0': BANK + 0x100, 'gmode': WT_GMODE, 'run': WT_RUN}
    names, addrs = list(watch), list(watch.values())
    rd = bar.rd
    raw, regs, last = [], [], None
    bar.wr(sp.VDB_ACTIVE_SR, rd(sp.VDB_ACTIVE_SR) | (1 << sp.RATE))
    t0 = time.perf_counter()
    fired, t_fire = False, None
    n_pass = 0
    while True:
        t = time.perf_counter() - t0
        if t > dur:
            break
        if not fired and t >= trig_at:
            trigger(); fired, t_fire = True, t
        raw.append((t, rd(PAR6), rd(DB0), rd(DB1)))
        n_pass += 1
        if n_pass & 7:                        # the registers every 8th pass, so the samples keep up
            continue
        cur = tuple(rd(a) for a in addrs)
        if cur != last:
            regs.append((round(t * 1e6), {n: hex(v) for n, v, o in zip(names, cur, last or [None] * len(cur)) if v != o}))
            last = cur
    out, prev, pw = [], None, 1
    prp = None
    for t, p6, v0, v1 in raw:
        rp = (p6 >> 8) & 0xff
        if rp == prp:
            continue
        w0, w1 = sp.s16(v0 & 0xffff), sp.s16(v1 & 0xffff)
        if prev is None:
            val, pw = w0, 0
        elif w0 != prev[0] and w1 == prev[1]:
            val, pw = w0, 0
        elif w1 != prev[1] and w0 == prev[0]:
            val, pw = w1, 1
        else:
            pw = 1 - pw; val = w1 if pw else w0
        out.append((t, rp, val))
        prev, prp = (w0, w1), rp
    return out, regs, t_fire


def ramp_case(bar, log, name, before, trigger, dur=0.4, trig_at=0.02, mramp=DRV_MRAMP, aramp=DRV_ARAMP):
    """before(): writes after the voice is set up and playing, before the capture."""
    sp.SRC_FEED = ALL['ds_left']
    sp.setup(bar, log, 0, 13, 0x4000, pattern=[0] * 64)
    voice_setup(bar, log, 0, 0x7f, 0x7f, ds=True, level=0x1000)
    bar.wr(WT_MRAMP, mramp); bar.wr(WT_ARAMP, aramp)
    bar.wr(WT_MUTE, 0); bar.wr(BANK + 0x100, 0)
    voice_go(bar)
    before()
    out, regs, t_fire = long_capture(bar, dur, trig_at, trigger)
    voice_stop(bar, log)
    sp.clear(bar, log)
    bar.wr(WT_MRAMP, DRV_MRAMP); bar.wr(WT_ARAMP, DRV_ARAMP)
    return out, regs, t_fire


def ramp_summary(out, t_fire):
    """Level before the trigger, and the times (ms after it) the output crossed 10/50/90% of the
    way to its final level, from samples averaged in 0.5 ms bins."""
    if not out:
        return {}
    pre = [v for t, _, v in out if t < t_fire]
    post = [(t - t_fire, v) for t, _, v in out if t >= t_fire]
    a = sum(pre[-40:]) / max(1, len(pre[-40:]))
    tail = [v for _, v in post[-200:]]
    b = sum(tail) / max(1, len(tail))
    bins = {}
    for t, v in post:
        bins.setdefault(int(t / 0.0005), []).append(v)
    xs = sorted((k * 0.5, sum(v) / len(v)) for k, v in bins.items())
    cross = {}
    for f in (0.1, 0.5, 0.9):
        th = a + (b - a) * f
        for ms, v in xs:
            if (b >= a and v >= th) or (b < a and v <= th):
                cross[f'{int(f * 100)}%'] = ms
                break
    return {'before': round(a, 1), 'after': round(b, 1), 'cross_ms': cross, 'samples': len(out)}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd not in ('start', 'bus', 'sweep', 'levels', 'gains', 'state', 'mix', 'own', 'reso', 'ramp', 'filtrate', 'sendfilt', 'parmpoke', 'clear'):
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
    elif cmd == 'reso':
        # the filter's feedback (resonance) word, PARM2 bits 31-16, which the driver never sets.
        # The voice's FIFO holds one impulse in 64 words, so it plays an impulse every 64 samples
        # (1.33 ms); each capture holds about two of them as they come through the four-stage
        # filter. Three cut-offs, feedback from 0 to 0xffff, and a step for the DC gain.
        import numpy as np
        mute_others(bar, log)
        imp = [0x2000] + [0] * 63
        step = [0x1000] * 32 + [0] * 32
        fbs = [0, 0x0400, 0x0800, 0x1000, 0x2000, 0x3000, 0x4000, 0x5000, 0x6000, 0x7000, 0x7fff,
               0x8000, 0xa000, 0xc000, 0xe000, 0xffff]
        rows = []
        for filt in (0xb0, 0xb8, 0xc0, 0xcf):
            for fb in fbs:
                for name, pat in (('impulse', imp), ('step', step)):
                    lv, r = capture_bus(bar, quiet, 'ds_left', 0, 0x7f, 0x7f, ds=True, filt=filt, pattern=pat, fb=fb)
                    rows.append((filt, fb, 0 if name == 'impulse' else 1, *((r + [0] * 160)[:160])))
                log(event='reso', filt=hex(filt), fb=hex(fb), peak=max(rows[-2][3:], key=abs),
                    impulse=[x for x in rows[-2][3:] if x][:24])
        path = sp.arg('--out', 'wtprobe-reso.npz', str)
        np.savez(path, rows=np.array(rows))
        log(event='saved', path=path)
    elif cmd == 'filtrate':
        # The low-pass against the voice's half and its playback rate. A 3.0 replay (manual
        # reflections, 2026-11-17) put the card's reflections up to 11 dB above the model at
        # 0.8-1.6 kHz, as if the cut-off byte 0xa1 acted like 0xb1 (twice the coefficient), on
        # first-half voices 0-3 playing a 44.1 kHz source (rate 0x9acc) through the sends. The
        # filter law was measured on second-half voice 8 at 48 kHz (0xa000). Here: an impulse
        # every 64 FIFO words through each cut-off, at three rates, on both voices, read on the
        # direct-stream pair (pan 0), plus the reverb send at the reflections' settings.
        import numpy as np
        imp = [0x2000] + [0] * 63
        rows = []
        for v in (40, 0):
            set_voice(v)
            mute_others(bar, log)
            for rate in (0xa000, 0x9acc, 0x7a1c):
                for filt in (0xa1, 0xb1, 0xc0, 0xcf):
                    cases = [('ds_left', True, 0, 0x7f, 0x7f)]
                    if filt == 0xa1:
                        cases.append(('reverb', False, 0x3f, 0x7f, 0x7f))
                    for bus, ds, pan, cho, rev in cases:
                        lv, r = capture_bus(bar, quiet, bus, pan, cho, rev, ds=ds, filt=filt, pattern=imp, rate=rate)
                        rows.append((v, rate, filt, 0 if ds else 1, *((r + [0] * 160)[:160])))
                        log(event='filtrate', voice=v, rate=hex(rate), filt=hex(filt), bus=bus,
                            peak=max(rows[-1][4:], key=abs), head=[x for x in rows[-1][4:] if x][:16])
            for lv in range(32):                      # leave the half as the driver would find it
                bar.wr(BANK + 0x200 + lv * 0x10 + 12, 0)
        set_voice(40)
        path = sp.arg('--out', 'wtprobe-filtrate.npz', str)
        np.savez(path, rows=np.array(rows))
        log(event='saved', path=path, cases=len(rows))
    elif cmd == 'sendfilt':
        # Does the low-pass act the same on the sends (36/37) as on the direct-stream and main
        # pairs? The 3.0 replay had the card's reflections (first-half voices 0-3, cut-off 0xa1,
        # through the sends) brighter above 400 Hz than the model by up to 11 dB, while
        # filtrate found the cut-off itself as modelled; its send captures were too small to
        # read (peaks of 1-5). Here a square wave at full scale (32 words up, 32 down), so the
        # sends get edges thousands of steps high, read on each path at three rates.
        import numpy as np
        sq = [0x7fff] * 32 + [0] * 32
        rows = []
        paths = [('ds_left', True, 0, 0, 0, 0), ('left', False, 0x3f, 0, 0, 1),
                 ('reverb', False, 0x3f, 0x7f, 0x7f, 2), ('chorus', False, 0x3f, 0x7f, 0x7f, 3)]
        for v in (0, 40):
            set_voice(v)
            mute_others(bar, log)
            for rate in (0xa000, 0x9acc, 0x5000):
                for filt in (0xa1, 0xb1, 0xcf):
                    for bus, ds, pan, cho, rev, code in paths:
                        lv, r = capture_bus(bar, quiet, bus, pan, cho, rev, ds=ds, filt=filt, pattern=sq, rate=rate)
                        rows.append((v, rate, filt, code, *((r + [0] * 160)[:160])))
                        log(event='sendfilt', voice=v, rate=hex(rate), filt=hex(filt), bus=bus,
                            peak=max(rows[-1][4:], key=abs), head=[x for x in rows[-1][4:]][:24])
            for lv in range(32):
                bar.wr(BANK + 0x200 + lv * 0x10 + 12, 0)
        set_voice(40)
        path = sp.arg('--out', 'wtprobe-sendfilt.npz', str)
        np.savez(path, rows=np.array(rows))
        log(event='saved', path=path, cases=len(rows))
    elif cmd == 'parmpoke':
        # sendfilt found the same low-pass on every path, so the sends are not the cause of the
        # reflections' extra treble on the card. The driver rewrites each reflection voice's PARM0
        # about every 2.4 ms (the same cut-off and gain, the rate nudged between 0x9aa0 and
        # 0x9af8) and PARM1 with it. If a write disturbs the filter (resets its state, or
        # restarts a glide), the voice would let more treble through on the card than in the
        # model, which leaves the filter alone. Here a 32-word pulse at 12 kHz (128 output
        # samples high) through cut-off 0xa1 on the direct-stream pair of first-half voice 0,
        # with nothing written, then with PARM0/PARM1 rewritten once mid-pulse or on every
        # pass of the capture loop (every few microseconds), with the same words or the rate
        # nudged, and PARM1 alone.
        import numpy as np
        set_voice(0)
        mute_others(bar, log)
        pulse = [0x4000] * 32 + [0] * 32
        rate, filt, gain = 0x5000, 0xa1, 0xcf
        p0 = (filt << 24) | (gain << 16) | (rate & 0xfffe)
        p0n = (filt << 24) | (gain << 16) | ((rate + 0x20) & 0xfffe)
        cases = [('none', None, None),
                 ('PARM0 same, once at 1.2 ms', [(WT_PARM, p0)], 0.0012),
                 ('PARM0+1 same, once at 1.2 ms', [(WT_PARM, p0), (WT_PARM + 4, p0 | 1)], 0.0012),
                 ('PARM0 same, every pass', [(WT_PARM, p0)], None),
                 ('PARM0+1 same, every pass', [(WT_PARM, p0), (WT_PARM + 4, p0 | 1)], None),
                 ('PARM0+1 rate nudged, every pass', [(WT_PARM, p0n), (WT_PARM + 4, p0n | 1)], None),
                 ('PARM0+1 rate nudged, once at 1.2 ms', [(WT_PARM, p0n), (WT_PARM + 4, p0n | 1)], 0.0012),
                 ('PARM1 same, every pass', [(WT_PARM + 4, p0 | 1)], None)]
        rows = []
        for n, (name, poke, at) in enumerate(cases):
            for rep in range(2):
                lv, r = capture_bus(bar, quiet, 'ds_left', 0, 0, 0, ds=True, filt=filt, gain=gain,
                                    pattern=pulse, rate=rate, poke=poke, poke_at=at)
                rows.append((n, rep, *((r + [0] * 200)[:200])))
                log(event='parmpoke', case=name, rep=rep, peak=max(r, key=abs) if r else 0,
                    head=[x for x in r if x][:40])
        for lv in range(32):
            bar.wr(BANK + 0x200 + lv * 0x10 + 12, 0)
        set_voice(40)
        path = sp.arg('--out', 'wtprobe-parmpoke.npz', str)
        np.savez(path, rows=np.array(rows), cases=np.array([c[0] for c in cases]))
        log(event='saved', path=path, cases=len(rows))
    elif cmd == 'ramp':
        # Smooth mute: a voice playing at gain 0xcf, state 7, then 1 written to its mute word
        # (ALSA's layout) or to its bit in voice 0's word (the description's 'one bit per voice').
        # The description says the voice ends in HARD_MUTE with its mute bit cleared: the state
        # code it ends in is then the starting state for the attacks.
        # Attack: from that state, PARM0 (base) and PARM1 (target) with equal pitch and cut-off and
        # bit 0 (the bit the driver sets in PARM1 only) 0; PARM0's gain byte is the attack field
        # (phase 0, slope in bits 2-0), PARM1's the target gain 0xcf; then each candidate code in
        # PARM3's gain-state field. Slopes 0-7 at the driver's attack-ramp word, then three other
        # attack-ramp words, and three mute-ramp words, to decode the two period registers.
        import numpy as np
        mute_others(bar, log)
        caps, meta = [], []
        def keep(name, res, **kw):
            out, regs, tf = res
            summ = ramp_summary(out, tf)
            log(event='ramp', case=name, **kw, **summ, regs=regs[:60])
            meta.append(dict(case=name, **kw, t_fire=tf))
            caps.append(np.array([(round(t * 1e6), rp, v) for t, rp, v in out], dtype=np.int32))
            return regs
        hard = None
        for mramp in (DRV_MRAMP, 0x08000000, 0x20000000, 0x01000000, 0x00100000, 0x10000010):
            regs = keep('mute (word)', ramp_case(bar, quiet, 'mute', lambda: None, lambda: bar.wr(WT_MUTE, 1), mramp=mramp),
                        mramp=hex(mramp))
            p3 = [r[1]['parm3'] for r in regs if 'parm3' in r[1]]
            if p3 and hard is None:
                hard = (int(p3[-1], 16) >> 21) & 7
        keep('mute (bit in voice 0 word)', ramp_case(bar, quiet, 'mute', lambda: None,
             lambda: bar.wr(BANK + 0x100, 1 << LV)), mramp=hex(DRV_MRAMP))
        hard = 0 if hard is None or hard == 7 else hard
        log(event='hard_mute_state', state=hard)

        def attack(state, slope, bit0=0, target=0xcf):
            def before():
                bar.wr(WT_PARM + 12, (0xcf << 24) | (hard << 21))
                bar.wr(WT_PARM + 0, (0xcf << 24) | (slope << 16) | 0xa000)
                bar.wr(WT_PARM + 4, (0xcf << 24) | (target << 16) | 0xa000 | bit0)
            return before, lambda: bar.wr(WT_PARM + 12, (0xcf << 24) | (state << 21))
        cands = [s for s in range(1, 8) if s != hard]
        for st in cands:
            for slope in range(8):
                b, t = attack(st, slope)
                keep('attack', ramp_case(bar, quiet, 'attack', b, t), state=st, slope=slope, aramp=hex(DRV_ARAMP))
            b, t = attack(st, 4, bit0=1)
            keep('attack, PARM1 bit 0 set (as the driver)', ramp_case(bar, quiet, 'attack', b, t), state=st, slope=4, aramp=hex(DRV_ARAMP))
            for ar in (0x00410000, 0x01060000, 0x00830080, 0x00030000):
                b, t = attack(st, 4)
                keep('attack', ramp_case(bar, quiet, 'attack', b, t, aramp=ar), state=st, slope=4, aramp=hex(ar))
        path = sp.arg('--out', 'wtprobe-ramp.npz', str)
        np.savez_compressed(path, meta=np.array([json.dumps(m) for m in meta]), **{f'c{i}': c for i, c in enumerate(caps)})
        log(event='saved', path=path, captures=len(caps))
    elif cmd == 'clear':
        mix_clear(bar, log)
        voice_stop(bar, log)
        sp.clear(bar, log)


if __name__ == '__main__':
    main()
