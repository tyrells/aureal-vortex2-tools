#!/usr/bin/env python3
"""Read the sample-rate converter's interpolation table out of an idle card, from the Linux host.

Run on the X79 host with the card bound to vfio-pci and no VM running (the card's blocks are
then idle and nothing else writes to it). Nothing here needs a guest or a driver.

  sudo ./srcprobe.py ram   [--log FILE]           stage 1: which RAMs take host writes, and how
                                                  the converter FIFOs are split between channels
  sudo ./srcprobe.py run   [--frac F] [--wing W] [--ratio R] [--alias] [--rate N] [--dmaptr P]
                           [--noflush] [--log FILE]
                                                  stage 2: one pass: route FIFO -> SRC -> FIFO,
                                                  preload an impulse, enable, flush, read back
                                                  (--alias: send the output to FIFO b's alias)
  sudo ./srcprobe.py sweep [--step S] [--wing W] [--out FILE.npz] [--log FILE]
                                                  stage 3: 'run' for every S-th time fraction
                                                  (0..16383), saving each pass's output
  sudo ./srcprobe.py release                      take the codec out of reset and start the AC-link
                                                  (the chip's audio clock); a cold card has it held
  sudo ./srcprobe.py clear                        undo everything this script sets

The chain, following the method of the register map's author (names from his map):
  VDB FIFO a (preloaded by the host) -> SRC k input -> SRC k output -> VDB FIFO b
Both VDB links hang off sample-rate header SRCk (VDB_HDR_SRC0 + 4k), and the converter's own
work-to-do list for that rate (SRC_HDR_SRC0 + 4k) holds channel k, so the converter clocks itself
from its throttle counter. With the conversion ratio at 1, every output sample is the impulse
weighted by the table at one fixed phase, set by the current time fraction (SRC_CHANNEL_PARAMS4,
14 bits); the samples-per-wing field (SRC_CHANNEL_PARAMS0 bits 7-4) sets the window width.
The order is the one he gives: program the routes without enabling them, program the converter
and its work-to-do entry, preload the FIFO, enable the routes' sample rate, then set the FIFO's
flush bit (bit 21 of its pointer/control word) and read the far end.

The converter set-up values (one-entry work-to-do list with the header's bit 4 clear, throttle
counter 0x1ff, FIFO target depth 15 with the write pointer preset to 15, wing field 12) are the
ones Aureal's own Linux core (asp30.o in au88xx 1.1.2, CAsp4Src::SetupChannel and
CAsp4SrcBlock::AddWTD) writes for a ratio of 1 or below.

Every step's register writes and readbacks go to the JSON log, so a run that does not behave
still shows where the chain stopped.
"""
import ctypes
import datetime
import json
import mmap
import os
import sys
import time

DEV = os.environ.get('HOSTDEV', '0000:0c:01.0')
RES = f'/sys/bus/pci/devices/{DEV}/resource0'
SIZE = 0x40000

# register map names (see the map's author's AU8830.h; addresses also in ALSA's au8830.h)
FIFO_VDB_CHANNEL_DATA = 0x14000          # + ch * 0x100, 64 words, low 16 bits = sample
FIFO_VDB_CHANNEL_PC = 0x16100            # + ch * 4: pointers and control
PC_FLUSH = 1 << 21
PC_FREEZE = 1 << 20
PC_VALID = 1 << 4
PC_EMPTY = 1 << 5
SRC_INPUT_FIFO = 0x26000
SRC_OUTPUT_DOUBLE_BUFFER = 0x26800       # 32 words: 2 per channel (the double buffer), run 1 of stage 1
SRC_NEXT_CHANNEL = 0x26C00               # + k * 4: bit 4 valid, bits 3-0 next channel
SRC_SR_HEADER = 0x26C40                  # + rate * 4: bit 4 valid, bits 3-0 first channel
SRC_ACTIVE_SAMPLE_RATE = 0x26CC0         # bit per sample rate
SRC_THROTTLE_SOURCE = 0x26CC4            # bit per channel: 0 = counter / 2
SRC_THROTTLE_COUNT_SIZE = 0x26CC8        # 10 bits: system clocks per period
SRC_CHANNEL_PARAMS = [0x26E00 + 0x40 * i for i in range(7)]   # + k * 4
VDB_WTDRAM_DESTLINKS = 0x28000           # + dest * 4: next link (src << 8 | dest), 0xffff = end
VDB_WTDRAM_HEADERLINKS = 0x282B4         # + rate * 4: first link
VDB_ACTIVE_SR = 0x28400                  # bit per sample rate (ALSA VORTEX_ADB_SR)
END = 0xffff

SRC_K, FIFO_IN, FIFO_OUT = 0, 0, 1       # converter 0, VDB FIFOs 0 (in) and 1 (out)
RATE = SRC_K                             # sample-rate header: SRC0 (the converter's own rate) by
                                         # default, --rate 17 for the AC'97 port's 48 kHz
SRC_SRCADDR = 0x20 + SRC_K               # VDB source address of the converter's output
SRC_DSTADDR = 0x40 + SRC_K               # VDB destination address of its input
OUT_DST = FIFO_OUT                       # VDB destination of the output: FIFO b, or with --alias
                                         # its alias (0x20 + b), which the map lists separately


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class Bar:
    def __init__(self):
        self.fd = os.open(RES, os.O_RDWR | os.O_SYNC)
        self.mm = mmap.mmap(self.fd, SIZE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        self.base = ctypes.addressof(ctypes.c_char.from_buffer(self.mm))

    def rd(self, a):
        return ctypes.c_uint32.from_address(self.base + a).value

    def wr(self, a, v):
        ctypes.c_uint32.from_address(self.base + a).value = v & 0xffffffff


class Log:
    def __init__(self, path):
        self.f = open(path, 'a')

    def __call__(self, **kw):
        kw['utc'] = utc()
        line = json.dumps(kw)
        self.f.write(line + '\n'); self.f.flush()
        print(line)


def w(bar, log, addr, val, what):
    old = bar.rd(addr); bar.wr(addr, val); back = bar.rd(addr)
    log(event='write', what=what, addr=hex(addr), old=hex(old), wrote=hex(val), readback=hex(back))
    return back


def regs(bar):
    """The words this script touches, for before/after snapshots."""
    k, a, b = SRC_K, FIFO_IN, FIFO_OUT
    r = {
        'fifo_in_pc': FIFO_VDB_CHANNEL_PC + 4 * a, 'fifo_out_pc': FIFO_VDB_CHANNEL_PC + 4 * b,
        'src_next': SRC_NEXT_CHANNEL + 4 * k, 'src_hdr': SRC_SR_HEADER + 4 * RATE,
        'src_active': SRC_ACTIVE_SAMPLE_RATE, 'throttle_src': SRC_THROTTLE_SOURCE,
        'throttle_size': SRC_THROTTLE_COUNT_SIZE,
        'vdb_hdr': VDB_WTDRAM_HEADERLINKS + 4 * RATE, 'vdb_link_srcin': VDB_WTDRAM_DESTLINKS + 4 * SRC_DSTADDR,
        'vdb_link_out': VDB_WTDRAM_DESTLINKS + 4 * OUT_DST, 'vdb_active': VDB_ACTIVE_SR,
    }
    r.update({f'params{i}': SRC_CHANNEL_PARAMS[i] + 4 * k for i in range(7)})
    return {n: hex(bar.rd(addr)) for n, addr in r.items()}


def window(bar, base, n):
    return [bar.rd(base + 4 * i) & 0xffff for i in range(n)]


def s16(v):
    return v - 0x10000 if v & 0x8000 else v


# ---------------------------------------------------------------- stage 1

def stage_ram(bar, log):
    """Write a pattern to each RAM and read it back; restore what was there.
    For the converter FIFOs, a write to one address and reads of its neighbours show how the
    0x800 (input) and 0x400 (output) byte areas are split between the 16 channels."""
    log(event='stage', name='ram')
    for name, base, n in (('vdb_fifo_data', FIFO_VDB_CHANNEL_DATA + 0x100 * FIFO_IN, 64),
                          ('src_input_fifo', SRC_INPUT_FIFO, 0x200),
                          ('src_output_double_buffer', SRC_OUTPUT_DOUBLE_BUFFER, 0x100)):
        saved = [bar.rd(base + 4 * i) for i in range(n)]
        for i in range(n):
            bar.wr(base + 4 * i, (0x5a00 + i) & 0xffff)
        back = [bar.rd(base + 4 * i) & 0xffff for i in range(n)]
        ok = [i for i in range(n) if back[i] == (0x5a00 + i) & 0xffff]
        log(event='ram', name=name, base=hex(base), words=n, writable=len(ok),
            first_bad=next((hex(base + 4 * i) for i in range(n) if i not in ok), None),
            sample=[hex(x) for x in back[:8]])
        for i in range(n):
            bar.wr(base + 4 * i, saved[i])
    log(event='stage_done', name='ram')


# ---------------------------------------------------------------- stage 2

def setup(bar, log, frac, wing, ratio, impulse_at=8, amp=0x4000, dmaptr=32):
    k, a, b = SRC_K, FIFO_IN, FIFO_OUT
    log(event='before', regs=regs(bar))
    # 1. VDB routes, not yet enabled: header SRCk -> FIFO a to SRC k in -> SRC k out to FIFO b
    w(bar, log, VDB_ACTIVE_SR, bar.rd(VDB_ACTIVE_SR) & ~(1 << RATE), 'vdb rate off')
    w(bar, log, VDB_WTDRAM_HEADERLINKS + 4 * RATE, (a << 8) | SRC_DSTADDR, 'vdb hdr: FIFO a -> SRC in')
    w(bar, log, VDB_WTDRAM_DESTLINKS + 4 * SRC_DSTADDR, (SRC_SRCADDR << 8) | OUT_DST, 'vdb link: SRC out -> FIFO b')
    w(bar, log, VDB_WTDRAM_DESTLINKS + 4 * OUT_DST, END, 'vdb link: end')
    # 2. the converter and its work-to-do entry on its own rate
    w(bar, log, SRC_ACTIVE_SAMPLE_RATE, bar.rd(SRC_ACTIVE_SAMPLE_RATE) & ~(1 << RATE), 'src rate off')
    w(bar, log, SRC_THROTTLE_SOURCE, bar.rd(SRC_THROTTLE_SOURCE) & ~(1 << k), 'throttle: counter/2')
    w(bar, log, SRC_THROTTLE_COUNT_SIZE, 0x1ff, 'throttle count 0x1ff (Aureal core default)')
    w(bar, log, SRC_CHANNEL_PARAMS[0] + 4 * k, ((wing - 1) << 4) | k, 'params0: wing, throttle, self')
    w(bar, log, SRC_CHANNEL_PARAMS[1] + 4 * k, ratio, 'params1: 1/ratio 3.14')
    w(bar, log, SRC_CHANNEL_PARAMS[2] + 4 * k, 0, 'params2: drift error 0')
    w(bar, log, SRC_CHANNEL_PARAMS[3] + 4 * k, 0, 'params3: drift last 0')
    w(bar, log, SRC_CHANNEL_PARAMS[4] + 4 * k, frac & 0x3fff, 'params4: time fraction')
    w(bar, log, SRC_CHANNEL_PARAMS[5] + 4 * k, 1, 'params5: drift count 1')
    w(bar, log, SRC_CHANNEL_PARAMS[6] + 4 * k, (1 << 17) | 15, 'params6: depth 15, dir out, write pointer 15 (as Aureal core)')
    # a one-entry list: the header holds the channel with bit 4 clear (bit 4 = 'next is valid'),
    # as Aureal's core writes it (CAsp4SrcBlock::AddWTD)
    w(bar, log, SRC_SR_HEADER + 4 * RATE, k, 'src hdr: channel k, no next')
    for i in range(0x80 // 4):
        bar.wr(SRC_INPUT_FIFO + 0x80 * k + 4 * i, 0)
    for i in range(2):
        bar.wr(SRC_OUTPUT_DOUBLE_BUFFER + 8 * k + 4 * i, 0)
    # 3. FIFOs: input preloaded with an impulse, output cleared
    for i in range(64):
        bar.wr(FIFO_VDB_CHANNEL_DATA + 0x100 * a + 4 * i, amp if i == impulse_at else 0)
        bar.wr(FIFO_VDB_CHANNEL_DATA + 0x100 * b + 4 * i, 0)
    log(event='preload', fifo=a, impulse_at=impulse_at, amp=hex(amp),
        readback=[hex(x) for x in window(bar, FIFO_VDB_CHANNEL_DATA + 0x100 * a, 16)])
    # the FIFO counts as holding data when its DMA pointer is ahead of its VDB pointer: put the
    # DMA pointer where a DMA of dmaptr samples would have left it (run 2: with both at 0 the
    # VDB moved nothing and the FIFO raised a DMA request)
    w(bar, log, FIFO_VDB_CHANNEL_PC + 4 * a, ((dmaptr & 0x3f) << 12) | PC_VALID, 'fifo a: valid, DMA pointer ahead, VDB pointer 0')
    w(bar, log, FIFO_VDB_CHANNEL_PC + 4 * b, PC_VALID | PC_EMPTY, 'fifo b: valid, empty')
    w(bar, log, SRC_ACTIVE_SAMPLE_RATE, bar.rd(SRC_ACTIVE_SAMPLE_RATE) | (1 << RATE), 'src rate on')
    # 4. enable the route's sample rate
    w(bar, log, VDB_ACTIVE_SR, bar.rd(VDB_ACTIVE_SR) | (1 << RATE), 'vdb rate on')


def flush_and_read(bar, log, reads=40, gap=0.0005, flush=True):
    k, a, b = SRC_K, FIFO_IN, FIFO_OUT
    # 5. pump: the flush bit on the input FIFO (mask bits 31-22 left 0 = use new values)
    if flush:
        pc = bar.rd(FIFO_VDB_CHANNEL_PC + 4 * a)
        w(bar, log, FIFO_VDB_CHANNEL_PC + 4 * a, (pc & 0x3fffff) | PC_FLUSH, 'fifo a: flush')
    snaps = []
    for i in range(reads):
        snaps.append({'t_ms': round(i * gap * 1000, 2),
                      'pc_in': hex(bar.rd(FIFO_VDB_CHANNEL_PC + 4 * a)),
                      'pc_out': hex(bar.rd(FIFO_VDB_CHANNEL_PC + 4 * b)),
                      'params4': hex(bar.rd(SRC_CHANNEL_PARAMS[4] + 4 * k)),
                      'params6': hex(bar.rd(SRC_CHANNEL_PARAMS[6] + 4 * k)),
                      'src_in0_15': [hex(bar.rd(SRC_INPUT_FIFO + 0x80 * k + 4 * j) & 0xffff) for j in range(16)],
                      'dbuf': [hex(bar.rd(SRC_OUTPUT_DOUBLE_BUFFER + 8 * k + 4 * j) & 0xffff) for j in range(2)]})
        time.sleep(gap)
    out = [s16(x) for x in window(bar, FIFO_VDB_CHANNEL_DATA + 0x100 * b, 64)]
    dbuf = [s16(x) for x in window(bar, SRC_OUTPUT_DOUBLE_BUFFER + 8 * k, 2)]
    sin = [s16(x) for x in window(bar, SRC_INPUT_FIFO + 0x80 * k, 32)]
    log(event='snapshots', snaps=snaps)
    log(event='result', fifo_out=out, src_output_double_buffer=dbuf, src_input_fifo=sin)
    log(event='after', regs=regs(bar))
    return out, dbuf, sin


def clear(bar, log):
    k, a, b = SRC_K, FIFO_IN, FIFO_OUT
    w(bar, log, VDB_ACTIVE_SR, bar.rd(VDB_ACTIVE_SR) & ~(1 << RATE), 'vdb rate off')
    w(bar, log, SRC_ACTIVE_SAMPLE_RATE, bar.rd(SRC_ACTIVE_SAMPLE_RATE) & ~(1 << RATE), 'src rate off')
    w(bar, log, SRC_SR_HEADER + 4 * RATE, 0, 'src hdr: none')
    w(bar, log, VDB_WTDRAM_HEADERLINKS + 4 * RATE, END, 'vdb hdr: none')
    w(bar, log, VDB_WTDRAM_DESTLINKS + 4 * SRC_DSTADDR, END, 'vdb link: none')
    w(bar, log, FIFO_VDB_CHANNEL_PC + 4 * a, 0, 'fifo a: off')
    w(bar, log, FIFO_VDB_CHANNEL_PC + 4 * b, 0, 'fifo b: off')


def wake():
    """With no VM attached, vfio-pci may leave the card in D3hot with memory decoding off,
    and then every read returns 0xffffffff. Keep it powered and turn decoding on."""
    d = f'/sys/bus/pci/devices/{DEV}'
    try:
        with open(f'{d}/power/control', 'w') as f:
            f.write('on')
    except OSError as e:
        print(f'could not set power/control: {e}')
    try:
        with open(f'{d}/config', 'r+b') as f:
            f.seek(4); cmd = int.from_bytes(f.read(2), 'little')
            if not cmd & 2:
                f.seek(4); f.write((cmd | 2).to_bytes(2, 'little'))
                print(f'memory decoding was off (COMMAND {cmd:#06x}); turned it on')
    except OSError as e:
        print(f'could not read/write config space: {e}')
    try:
        print('power state:', open(f'{d}/power_state').read().strip())
    except OSError:
        pass
    time.sleep(0.05)


def release(bar, log):
    """Run 3 on the card: nothing moved on either rate. A cold card's global control word
    (PCI_GCR, 0x2a00c) reads 0x00303260, with bit 21 (External Reset, which drives the reset pin
    of the AC'97 codec) set, and the codec supplies the AC-link bit clock (12.288 MHz), the chip's
    audio clock. The Windows driver ends with the bit clear (0x01103260). This clears it and runs
    the codec link start-up that ALSA's vortex_codec_init uses (CODEC_CTRL, 0x29184)."""
    GCR, CODEC_CTRL = 0x2a00c, 0x29184
    log(event='release_before', gcr=hex(bar.rd(GCR)), codec_ctrl=hex(bar.rd(CODEC_CTRL)))
    w(bar, log, GCR, bar.rd(GCR) & ~(1 << 21), 'GCR: External Reset off (codec out of reset)')
    time.sleep(0.02)
    for v in (0x00a8, 0x80a8, 0x80e8, 0x80a8, 0x00a8, 0x00e8):
        w(bar, log, CODEC_CTRL, v, 'codec link start-up (ALSA sequence)')
        time.sleep(0.002)
    time.sleep(0.05)
    log(event='release_after', gcr=hex(bar.rd(GCR)), codec_ctrl=hex(bar.rd(CODEC_CTRL)))


def vm_running():
    try:
        return any(open(f'/proc/{p}/comm').read().startswith('qemu') for p in os.listdir('/proc') if p.isdigit())
    except OSError:
        return False


def arg(name, default, conv=lambda s: int(s, 0)):
    return conv(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ('ram', 'run', 'sweep', 'clear', 'release'):
        sys.exit(__doc__)
    if vm_running():
        sys.exit('a QEMU process is running; stop the VM first (this needs the card idle)')
    wake()
    bar = Bar()
    if bar.rd(0x2a00c) == 0xffffffff:
        sys.exit('the card still reads 0xffffffff after waking it; check '
                 f'/sys/bus/pci/devices/{DEV}/power_state and try vfio-pci with disable_idle_d3=1')
    global OUT_DST, RATE
    RATE = arg('--rate', RATE)
    if '--alias' in sys.argv:
        OUT_DST = 0x20 + FIFO_OUT
    log = Log(arg('--log', 'srcprobe-log.jsonl', str))
    cmd = sys.argv[1]
    log(event='start', cmd=cmd, argv=sys.argv[1:], chip_ctrl=hex(bar.rd(0x2a00c)))
    wing = arg('--wing', 13)
    ratio = arg('--ratio', 0x4000)
    if cmd == 'ram':
        stage_ram(bar, log)
    elif cmd == 'run':
        setup(bar, log, arg('--frac', 0), wing, ratio, dmaptr=arg('--dmaptr', 32))
        flush_and_read(bar, log, flush='--noflush' not in sys.argv)
        clear(bar, log)
    elif cmd == 'sweep':
        import numpy as np
        step = arg('--step', 256)
        fracs = list(range(0, 0x4000, step))
        outs, dbufs = [], []
        quiet = Log(os.devnull)
        for f in fracs:
            setup(bar, quiet, f, wing, ratio)
            o, d, _ = flush_and_read(bar, quiet, reads=4)
            clear(bar, quiet)
            outs.append(o); dbufs.append(d)
            log(event='sweep_point', frac=f, peak=max(o, key=abs), nonzero=sum(1 for x in o if x))
        path = arg('--out', 'srcprobe-sweep.npz', str)
        np.savez(path, fracs=np.array(fracs), fifo_out=np.array(outs), dbuf=np.array(dbufs), wing=wing, ratio=ratio)
        log(event='saved', path=path, points=len(fracs))
    elif cmd == 'release':
        release(bar, log)
    elif cmd == 'clear':
        for r in (0, 17):                       # both rates this script uses
            RATE = r
            clear(bar, log)


if __name__ == '__main__':
    main()
