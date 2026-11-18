#!/usr/bin/env python3
"""Look for the Vortex 2's Sound Blaster hardware on an idle card, from the Linux host.

Run on the X79 host with the card bound to vfio-pci and no VM running, like srcprobe.py.

  sudo ./sbprobe.py scan  [--log FILE]   read the unexplored blocks (0x29800-0x29fff, 0x2a800-0x2a8ff)
                                         three times at rest: what each word holds, and which
                                         change on their own
  sudo ./sbprobe.py setup [--log FILE]   replay AU30DOS.COM's boot-time writes one step at a time,
                                         as traced (sbpro session, 2026-11-01), and after each step
                                         read 0x28000-0x2b44f and log every word that changed;
                                         then the Windows driver's write to 0x2a804; then put the
                                         control words back
  sudo ./sbprobe.py bits  [--log FILE]   which bits of each word in 0x29800-0x299ff and
                                         0x2a800-0x2a83f hold a value: write all ones, then zero,
                                         read back each, put the old value back
  sudo ./sbprobe.py ports [--log FILE]   with AU30DOS's control word set, read I/O ports 0x220-0x22f
                                         and 0x388-0x38b through /dev/port, and try a DSP reset at
                                         0x226. Only useful if the chipset forwards those legacy
                                         addresses to the card's slot; all 0xff means it does not
                                         (as with the MPU-401 and game port on this machine)
  sudo ./sbprobe.py fm    [--log FILE]   the FM ports at 388h-38bh, which this machine does forward (run 1:
                                         388h read 0x00 with the decode on, 0xff off): OPL register
                                         writes, and after each the latch block (0x29840-0x29854) and
                                         the 256-byte RAM at 0x29900; then the AdLib detection test
                                         (timer 1 started, status read: an OPL answers 0xc0)
  sudo ./sbprobe.py sbon  [--log FILE]   which control-word bit makes 220h-22fh answer: each candidate
                                         bit added on its own (and the driver's 0x2a804 word), the
                                         ports read, then a DSP reset at 226h and 22ah read (0xaa)
  sudo ./sbprobe.py fmmap [--log FILE]  the whole FM register file: every OPL register index in both banks
                                         written with a value that marks it, the RAM at 0x29900 read
                                         after each to find where it lands, the interrupt source word
                                         watched; then both timers, their masks and the status bits
  sudo ./sbprobe.py fmbit [--log FILE]  which control-word bit is the FM decode: 388h read with bit 1, bit 2,
                                         bit 19 and bit 24 each added alone to the reset word, and each
                                         taken away from AU30DOS's word
  sudo ./sbprobe.py clear

Why: with Sound Blaster emulation on, AU30DOS.COM runs from AUTOEXEC.BAT before Windows and
writes a block nobody has looked at: 0x29808 = 5 (the emulation's IRQ is 5), 0x2980c = 0,
0x29854 = 0xc00, clears 0x29900-0x299ec, and leaves ASCII '0's (0x30) at 0x2995c-0x29964 and
0x299d4-0x299dc. The word at 0x29850 reads 0x302, which is also the DSP version the emulation
reports (3.02). It then sets the control word to 0x0118326e (bits 1, 2, 3, 19 and 24 over
reset; bit 0 and bit 14 follow), and the Windows driver later writes 0x212eb to 0x2a804. If
the chip has Sound Blaster hardware (a DSP port block or legacy decode for 220h), it should
show up here; if AU30DOS only traps the ports through EMM386, these words are the driver's
own bookkeeping and nothing else will move.
"""
import importlib.util
import os
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('srcprobe', os.path.join(here, 'srcprobe.py'))
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)

CTRL, IRQ_CTRL, X804 = 0x2a00c, 0x2a004, 0x2a804
MIDI_DATA = 0x28800                      # reading it takes a byte from the MPU-401: skipped
BLOCKS = [(0x29800, 0x2a000), (0x2a800, 0x2a900)]
WIDE = (0x28000, 0x2b450)                # for the diffs: routes, MPU/game, codec, timers, control, EQ
SKIP = {MIDI_DATA}

# AU30DOS.COM's writes outside the shared audio set-up (routes, converters, mixer, DMA, codec),
# in trace order. The audio set-up is left out: it plays a short buffer, which needs host memory.
AU30DOS = [
    ('0x29808 = 5, 0x2980c = 0', [(0x29808, 0x5), (0x2980c, 0x0)]),
    ('ASCII zeros, first pass', [(0x299d4, 0x30303000), (0x299d8, 0x30303030), (0x299dc, 0x00003030),
                                 (0x2995c, 0x30303000), (0x29960, 0x30303030), (0x29964, 0x00003030)]),
    ('clear 0x29900-0x299ec', [(a, 0) for a in range(0x299ec, 0x298fc, -4)]),
    ('ASCII zeros again', [(0x299d4, 0x30303000), (0x299d8, 0x30303030)]),
    ('0x29854 = 0xc00', [(0x29854, 0xc00)]),
    ('0x22040 = 0x24 (parallel interface)', [(0x22040, 0x24)]),
    ('control word 0x0118326e', [(CTRL, 0x0118326e)]),
    ('control word bit 0 (MIDI enable): 0x0118326f', [(CTRL, 0x0118326f)]),
    ('control word bit 14: 0x0118726f', [(CTRL, 0x0118726f)]),
    ('Windows driver: 0x2a804 = 0x212eb', [(X804, 0x212eb)]),
]


def read_range(bar, lo, hi):
    return {a: bar.rd(a) for a in range(lo, hi, 4) if a not in SKIP}


def diff(before, after):
    return {hex(a): [hex(before[a]), hex(after[a])] for a in before if before[a] != after[a]}


def scan(bar, log):
    takes = []
    for _ in range(3):
        takes.append({a: v for lo, hi in BLOCKS for a, v in read_range(bar, lo, hi).items()})
        time.sleep(0.05)
    live = sorted(a for a in takes[0] if len({t[a] for t in takes}) > 1)
    nonzero = {hex(a): hex(v) for a, v in takes[0].items() if v not in (0, 0xffffffff)}
    fill = {}
    for a, v in takes[0].items():
        fill.setdefault(hex(v), 0)
        fill[hex(v)] += 1
    log(event='scan', live=[hex(a) for a in live], nonzero=nonzero,
        common=sorted(fill.items(), key=lambda kv: -kv[1])[:8])


def setup(bar, log):
    keep = {a: bar.rd(a) for a in (CTRL, IRQ_CTRL, X804, 0x29808, 0x2980c, 0x29854, 0x22040)}
    log(event='setup_before', regs={hex(a): hex(v) for a, v in keep.items()})
    # words that move on their own, so they can be left out of the diffs
    t0 = read_range(bar, *WIDE); time.sleep(0.05); t1 = read_range(bar, *WIDE)
    live = {a for a in t0 if t0[a] != t1[a]}
    log(event='live', words=[hex(a) for a in sorted(live)])
    prev = t1
    for name, writes in AU30DOS:
        for a, v in writes:
            bar.wr(a, v)
        time.sleep(0.02)
        now = read_range(bar, *WIDE)
        d = {k: v for k, v in diff(prev, now).items() if int(k, 16) not in live}
        log(event='step', step=name, wrote=[(hex(a), hex(v)) for a, v in writes][:8],
            readback=[(hex(a), hex(bar.rd(a))) for a, _ in writes][:8], changed=d)
        prev = now
    # put the control words back (the block's own words are left as AU30DOS left them; they
    # are what a card with the emulation on holds anyway)
    for a in (X804, CTRL, IRQ_CTRL):
        sp.w(bar, log, a, keep[a], 'restore')


def bits(bar, log):
    out = {}
    for lo, hi in ((0x29800, 0x29a00), (0x2a800, 0x2a840)):
        for a in range(lo, hi, 4):
            old = bar.rd(a)
            bar.wr(a, 0xffffffff); ones = bar.rd(a)
            bar.wr(a, 0); zeros = bar.rd(a)
            bar.wr(a, old); back = bar.rd(a)
            if ones != old or zeros != old:
                out[hex(a)] = dict(old=hex(old), ones=hex(ones), zeros=hex(zeros), restored=hex(back))
    log(event='bits', writable=out, count=len(out))


def ports(bar, log):
    try:
        fd = os.open('/dev/port', os.O_RDWR)
    except OSError as e:
        log(event='ports', error=str(e))
        return
    inb = lambda a: os.pread(fd, 1, a)[0]
    outb = lambda a, v: os.pwrite(fd, bytes([v]), a)
    old = bar.rd(CTRL)
    for word in (old, 0x0118326e, 0x0118726f):
        bar.wr(CTRL, word)
        time.sleep(0.01)
        sb = {hex(a): hex(inb(a)) for a in range(0x220, 0x230)}
        fm = {hex(a): hex(inb(a)) for a in range(0x388, 0x38c)}
        outb(0x226, 1); time.sleep(0.0001); outb(0x226, 0); time.sleep(0.001)
        log(event='ports', ctrl=hex(word), sb=sb, fm=fm,
            after_reset={'0x22a': hex(inb(0x22a)), '0x22e': hex(inb(0x22e))},
            block={hex(a): hex(bar.rd(a)) for a in (0x29800, 0x29804, 0x29808, 0x2980c, 0x29850, 0x29854)})
    sp.w(bar, log, CTRL, old, 'ctrl: restored')
    os.close(fd)


BLOCK_REGS = (0x29800, 0x29804, 0x29820, 0x29828, 0x2982c, 0x29830, 0x29840, 0x29844, 0x29848,
              0x2984c, 0x29850, 0x29854, 0x29858, 0x2985c)


def port_io():
    fd = os.open('/dev/port', os.O_RDWR)
    return fd, (lambda a: os.pread(fd, 1, a)[0]), (lambda a, v: os.pwrite(fd, bytes([v]), a))


def fm(bar, log):
    fd, inb, outb = port_io()
    old = bar.rd(CTRL)
    bar.wr(CTRL, 0x0118326e)
    time.sleep(0.01)
    regs = lambda: {a: bar.rd(a) for a in BLOCK_REGS}
    ram = lambda: [bar.rd(0x29900 + 4 * i) for i in range(64)]
    r0, m0 = regs(), ram()
    log(event='fm_before', status=hex(inb(0x388)), regs={hex(a): hex(v) for a, v in r0.items()})
    writes = [(0x388, 0x389, 0x01, 0x20), (0x388, 0x389, 0x20, 0x21), (0x388, 0x389, 0x40, 0x3f),
              (0x388, 0x389, 0xa0, 0x41), (0x388, 0x389, 0xbd, 0x00), (0x38a, 0x38b, 0x05, 0x01),
              (0x38a, 0x38b, 0x20, 0x22), (0x38a, 0x38b, 0xa0, 0x42)]
    for ia, da, reg, val in writes:
        outb(ia, reg); time.sleep(0.0001)
        r1, m1 = regs(), ram()
        outb(da, val); time.sleep(0.0001)
        r2, m2 = regs(), ram()
        log(event='fm_write', port=hex(ia), reg=hex(reg), val=hex(val), status=hex(inb(ia)),
            after_index={hex(a): hex(v) for a, v in r1.items() if v != r0[a]},
            after_data={hex(a): hex(v) for a, v in r2.items() if v != r1[a]},
            ram_changed={hex(0x29900 + 4 * i): [hex(m0[i]), hex(m2[i])] for i in range(64) if m2[i] != m0[i]})
        r0, m0 = r2, m2
    # AdLib detection: reset both timers and the IRQ flag, read status, start timer 1, wait, read
    def opl(reg, val):
        outb(0x388, reg); time.sleep(0.00004); outb(0x389, val); time.sleep(0.0001)
    opl(0x04, 0x60); opl(0x04, 0x80)
    s1 = inb(0x388)
    opl(0x02, 0xff); opl(0x04, 0x21)
    time.sleep(0.001)
    s2 = inb(0x388)
    opl(0x04, 0x60); opl(0x04, 0x80)
    log(event='adlib_detect', status_reset=hex(s1), status_after_timer=hex(s2),
        note='an OPL2/OPL3 reads 0x00 then 0xc0', regs={hex(a): hex(v) for a, v in regs().items()})
    sp.w(bar, log, CTRL, old, 'ctrl: restored')
    os.close(fd)


def sbon(bar, log):
    fd, inb, outb = port_io()
    old = bar.rd(CTRL)
    base = 0x0118326e
    trials = [('AU30DOS word', base, None), ('+ bit 0', base | 1, None), ('+ bit 4', base | 0x10, None),
              ('+ bit 14', base | 0x4000, None), ('+ bits 4 and 14', base | 0x4010, None),
              ('+ 0x2a804 = 0x212eb', base, 0x212eb), ('+ bit 4, 0x2a804', base | 0x10, 0x212eb),
              ('+ bits 4 and 14, 0x2a804', base | 0x4010, 0x212eb), ('bit 19 clear', base & ~(1 << 19), None),
              ('bit 24 clear', base & ~(1 << 24), None)]
    for name, word, x804 in trials:
        bar.wr(CTRL, word)
        bar.wr(X804, x804 if x804 is not None else 0)
        time.sleep(0.005)
        rd = {hex(a): hex(inb(a)) for a in range(0x220, 0x230) if inb(a) != 0xff}
        before = {a: bar.rd(a) for a in BLOCK_REGS}
        outb(0x226, 1); time.sleep(0.0001); outb(0x226, 0); time.sleep(0.001)
        after = {a: bar.rd(a) for a in BLOCK_REGS}
        log(event='sbon', trial=name, ctrl=hex(word), x804=hex(x804 or 0), non_ff=rd,
            reset_read={'0x22a': hex(inb(0x22a)), '0x22e': hex(inb(0x22e))},
            block_changed={hex(a): [hex(before[a]), hex(after[a])] for a in BLOCK_REGS if before[a] != after[a]},
            irq_source=hex(bar.rd(0x2a000)))
    bar.wr(X804, 0)
    sp.w(bar, log, CTRL, old, 'ctrl: restored')
    os.close(fd)


def fmmap(bar, log):
    fd, inb, outb = port_io()
    old, oldmask = bar.rd(CTRL), bar.rd(IRQ_CTRL)
    bar.wr(CTRL, 0x0118326e)
    time.sleep(0.01)
    ram = lambda: bytes(b for i in range(64) for b in bar.rd(0x29900 + 4 * i).to_bytes(4, 'little'))
    # start from a cleared file, as AU30DOS does
    for i in range(64):
        bar.wr(0x29900 + 4 * i, 0)
    bar.wr(0x2a000, bar.rd(0x2a000))                # clear what is pending (the driver writes the bits back)
    src0 = bar.rd(0x2a000)
    rows = []
    for bank, (ia, da) in enumerate(((0x388, 0x389), (0x38a, 0x38b))):
        for reg in range(0x100):
            if reg in (0x02, 0x03, 0x04) and bank == 0:
                continue                              # timers: below
            m0 = ram()
            outb(ia, reg); time.sleep(0.00004)
            val = 0x5a if reg != 0x05 else 0x01       # keep OPL3 mode on in bank 1
            outb(da, val); time.sleep(0.00005)
            m1 = ram()
            hit = [i for i in range(256) if m0[i] != m1[i]]
            src = bar.rd(0x2a000)
            rows.append((bank, reg, hit, [m1[i] for i in hit], src))
            if src != src0:
                bar.wr(0x2a000, src & ~src0)
            # put the byte back to 0 so the next register's hit is clean
            for i in hit:
                a = 0x29900 + (i & ~3); w_ = bar.rd(a) & ~(0xff << (8 * (i & 3))); bar.wr(a, w_)
        log(event='fmmap_bank', bank=bank, hits={hex(r): [[hex(x) for x in h], [hex(x) for x in v], hex(sv)]
                                                  for b, r, h, v, sv in rows if b == bank and (h or sv != src0)})
    # timers: status at rest, timer 1 (80 us) and timer 2 (320 us) each alone, masked, and the reset bit
    def opl(reg, val):
        outb(0x388, reg); time.sleep(0.00004); outb(0x389, val); time.sleep(0.00005)
    res = {}
    for name, seq, wait in (('rest', [(4, 0x60), (4, 0x80)], 0.001),
                            ('timer1 0xff', [(4, 0x60), (4, 0x80), (2, 0xff), (4, 0x01)], 0.001),
                            ('timer2 0xff', [(4, 0x60), (4, 0x80), (3, 0xff), (4, 0x02)], 0.002),
                            ('timer1 masked', [(4, 0x60), (4, 0x80), (2, 0xff), (4, 0x41)], 0.001),
                            ('timer1 0x00 (20 ms)', [(4, 0x60), (4, 0x80), (2, 0x00), (4, 0x01)], 0.005),
                            ('timer1 0x00 after 25 ms', [], 0.025)):
        for r, v in seq:
            opl(r, v)
        t0 = time.perf_counter(); seen = []
        while time.perf_counter() - t0 < wait:
            st = inb(0x388)
            if not seen or seen[-1][1] != st:
                seen.append((round((time.perf_counter() - t0) * 1e6), st))
        res[name] = dict(status=[(t, hex(v)) for t, v in seen][:8], irq_source=hex(bar.rd(0x2a000)),
                         latch=hex(bar.rd(0x29854)), view=hex(bar.rd(0x2985c)))
    opl(4, 0x60); opl(4, 0x80)
    log(event='fm_timers', **res)
    for i in range(64):
        bar.wr(0x29900 + 4 * i, 0)
    bar.wr(IRQ_CTRL, oldmask)
    sp.w(bar, log, CTRL, old, 'ctrl: restored')
    os.close(fd)


def fmbit(bar, log):
    fd, inb, outb = port_io()
    old = bar.rd(CTRL)
    out = {}
    for name, word in [('reset word', 0x00103260)] + [(f'reset + bit {b}', 0x00103260 | (1 << b)) for b in (1, 2, 4, 19, 24)] + \
                      [('AU30DOS word', 0x0118326e)] + [(f'AU30DOS - bit {b}', 0x0118326e & ~(1 << b)) for b in (1, 2, 19, 24)]:
        bar.wr(CTRL, word); time.sleep(0.002)
        out[name] = dict(ctrl=hex(word), at_388=hex(inb(0x388)), at_22a=hex(inb(0x22a)), at_22c=hex(inb(0x22c)))
    log(event='fmbit', results=out)
    sp.w(bar, log, CTRL, old, 'ctrl: restored')
    os.close(fd)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd not in ('scan', 'setup', 'bits', 'ports', 'fm', 'sbon', 'fmmap', 'fmbit', 'clear'):
        sys.exit(__doc__)
    if sp.vm_running():
        sys.exit('a QEMU process is running; stop the VM first (this needs the card idle)')
    sp.wake()
    bar = sp.Bar()
    if bar.rd(CTRL) == 0xffffffff:
        sys.exit('the card reads 0xffffffff; see srcprobe.py')
    log = sp.Log(sp.arg('--log', 'sbprobe-log.jsonl', str))
    log(event='start', cmd=cmd, argv=sys.argv[1:], chip_ctrl=hex(bar.rd(CTRL)))
    if bar.rd(CTRL) & (1 << 21):
        sp.release(bar, log)
    if cmd == 'scan':
        scan(bar, log)
    elif cmd == 'setup':
        setup(bar, log)
    elif cmd == 'bits':
        bits(bar, log)
    elif cmd == 'ports':
        ports(bar, log)
    elif cmd == 'fm':
        fm(bar, log)
    elif cmd == 'sbon':
        sbon(bar, log)
    elif cmd == 'fmmap':
        fmmap(bar, log)
    elif cmd == 'fmbit':
        fmbit(bar, log)
    elif cmd == 'clear':
        sp.w(bar, log, X804, 0, '0x2a804: 0')
        sp.w(bar, log, CTRL, bar.rd(CTRL) & ~((1 << 24) | 0x4007), 'ctrl: legacy bits off')


if __name__ == '__main__':
    main()
