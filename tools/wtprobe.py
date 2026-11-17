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
LEVEL = 0x4000


def parm3(pan, cho, rev):
    return (0xcf << 24) | (7 << 21) | ((pan & 0x7f) << 14) | ((cho & 0x7f) << 7) | (rev & 0x7f)


def voice_setup(bar, log, pan, cho, rev):
    w = sp.w
    w(bar, log, WT_FIFO_PC, 0, 'wt fifo: off')
    for i in range(64):
        bar.wr(WT_FIFO_DATA + 4 * i, LEVEL)
    log(event='wt_preload', readback=[hex(bar.rd(WT_FIFO_DATA + 4 * i) & 0xffff) for i in range(4)])
    w(bar, log, WT_CTRL, 0x1aa3, 'half control (driver value)')
    w(bar, log, WT_SRAMP, 0x00830000, 'SRAMP (driver value)')
    w(bar, log, WT_DSREG, bar.rd(WT_DSREG) & ~(1 << LV), 'DSREG: voice clear (main pair and sends)')
    w(bar, log, WT_SYNC, 0, 'pair sync off')
    w(bar, log, WTDMA_CTRL, 0x22000, 'wt dma: signed 16-bit')
    w(bar, log, WT_PARM + 0, 0xcfcfa000, 'PARM0: filter open, gain unity, 48 kHz')
    w(bar, log, WT_PARM + 4, 0xcfcfa001, 'PARM1: same, state bit differing (base word used)')
    w(bar, log, WT_PARM + 8, 0, 'PARM2: no feedback, phase 0')
    w(bar, log, WT_PARM + 12, parm3(pan, cho, rev), f'PARM3: pan {pan:#04x} chorus {cho:#04x} reverb {rev:#04x}')
    w(bar, log, WT_RUN, 0, 'running flag (driver writes 0)')


def voice_go(bar):
    bar.wr(WT_FIFO_PC, (63 << 12) | sp.PC_VALID)      # valid, DMA pointer 63 ahead: the FIFO is full


def voice_stop(bar, log):
    sp.w(bar, log, WT_FIFO_PC, 0, 'wt fifo: off')
    sp.w(bar, log, WT_PARM + 12, 0, 'PARM3: hard mute')


def capture_bus(bar, log, bus, pan, cho, rev):
    sp.SRC_FEED = BUSES[bus]
    sp.setup(bar, log, 0, 13, 0x4000, pattern=[0] * 64)
    voice_setup(bar, log, pan, cho, rev)
    voice_go(bar)
    r, _, _ = sp.flush_and_read(bar, log, flush=False)
    voice_stop(bar, log)
    sp.clear(bar, log)
    nz = [x for x in r if x]
    # the plateau: the most common non-zero value (the DC level while the FIFO lasts)
    plateau = max(set(nz), key=nz.count) if nz else 0
    return plateau, r


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd not in ('start', 'bus', 'sweep', 'clear'):
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
            log(event='bus', bus=bus, pan=pan, rev=rev, cho=cho, plateau=plateau,
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
            rows.append((pan, cho, rev, *row))
            log(event='setting', pan=pan, cho=cho, rev=rev, **dict(zip(BUSES, row)))
        path = sp.arg('--out', 'wtprobe-sweep.npz', str)
        np.savez(path, rows=np.array(rows), cols=np.array(['pan', 'cho', 'rev', *BUSES]), level=LEVEL)
        log(event='saved', path=path, settings=len(rows))
    elif cmd == 'clear':
        voice_stop(bar, log)
        sp.clear(bar, log)


if __name__ == '__main__':
    main()
