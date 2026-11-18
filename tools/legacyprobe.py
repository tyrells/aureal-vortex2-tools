#!/usr/bin/env python3
"""Probe the Vortex 2's MPU-401 UART and game port on an idle card, from the Linux host.

Run on the X79 host with the card bound to vfio-pci and no VM running, like srcprobe.py.
Nothing here needs anything plugged in; what is plugged in changes what can be learned.

  sudo ./legacyprobe.py mpu [--notes] [--log FILE]
        the UART through its memory-mapped registers (data 0x28800, command/status 0x28804):
        status before and after reset, the reset and UART-mode acknowledgements, then a
        stream of bytes timed by the status bit, which gives the bit rate (31,250 baud if it
        is a real MIDI UART). With --notes it plays a C major scale on channel 1, so a MIDI
        synth on the game port's MIDI pins (adapter cable) or a wavetable board on the
        card's header can be heard; anything looped back to MIDI in is read and logged.
  sudo ./legacyprobe.py game [--seconds N] [--log FILE]
        the game port: the legacy byte (0x28808: buttons in bits 7-4, the four one-shots in
        bits 3-0) and the one-shots timed after a trigger write; then the digital axis
        registers (0x28810 + axis x 4, with CTRL2 bit 6 set) sampled for N seconds (default
        10). With a joystick plugged in, move it and press the buttons during that time.
  sudo ./legacyprobe.py ports [--log FILE]
        which I/O addresses the legacy enables put the MPU-401 and game port at. The Windows
        driver's control word (0x0110326f, ALSA's names: MIDI enable, game enable, MIDI port
        field 3, game port field 1) is set, then each MIDI port field 0-3 in turn, and the
        MPU-401's reset/acknowledge is tried through /dev/port at 0x300-0x330; the game port
        byte is read at 0x200-0x20f. This only works if the chipset forwards those legacy
        addresses to the card's slot; if every read is 0xff, it does not, and that is the
        answer for this machine (not for the card).
  sudo ./legacyprobe.py clear

Why: the driver gives Windows a "Vortex AU8830 MPU-401" device, the card has a game port and a
wavetable header, and none of it reaches a VM through vfio (the legacy I/O addresses are not
passed through), so none of it has been traced. An emulator can hang 86Box's own MPU-401 and
game port on these registers; this fixes what they are.

Register names are ALSA's (au8830.h, au88x0_mpu401.c, au88x0_game.c); the clock divider 0x61
in CTRL2 bits 15-8 is the value the Windows driver writes (0x6100, every trace).
"""
import importlib.util
import os
import sys
import time

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('srcprobe', os.path.join(here, 'srcprobe.py'))
sp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sp)

MIDI_DATA, MIDI_CMD = 0x28800, 0x28804
GAME_LEGACY, CTRL2, GAME_AXIS = 0x28808, 0x2880c, 0x28810
CTRL = 0x2a00c
IRQ_SOURCE = 0x2a000
CTRL_MIDI_EN, CTRL_MIDI_PORT, CTRL_GAME_EN, CTRL_GAME_PORT = 0x1, 0x60, 0x8, 0xe00
CTRL2_ADC = 0x40
ST_OUT_BUSY, ST_IN_EMPTY = 0x40, 0x80        # MPU-401 status: bit 6 = can't take a byte, bit 7 = nothing to read


def status(bar):
    return bar.rd(MIDI_CMD) & 0xff


def drain(bar, log, what):
    got = []
    for _ in range(64):
        if status(bar) & ST_IN_EMPTY:
            break
        got.append(bar.rd(MIDI_DATA) & 0xff)
    log(event='drain', what=what, bytes=[hex(b) for b in got], status=hex(status(bar)), irq_source=hex(bar.rd(IRQ_SOURCE)))
    return got


def send(bar, data, timeout=0.01):
    """Write bytes, each once the UART can take it; return the wait before each (us)."""
    waits = []
    for b in data:
        t0 = time.perf_counter()
        while status(bar) & ST_OUT_BUSY:
            if time.perf_counter() - t0 > timeout:
                break
        waits.append(round((time.perf_counter() - t0) * 1e6, 1))
        bar.wr(MIDI_DATA, b)
    return waits


def mpu(bar, log, notes):
    w = sp.w
    log(event='mpu_before', status=hex(status(bar)), data=hex(bar.rd(MIDI_DATA)), ctrl2=hex(bar.rd(CTRL2)), ctrl=hex(bar.rd(CTRL)))
    w(bar, log, CTRL2, (bar.rd(CTRL2) & 0xffff00cf) | (0x61 << 8), 'ctrl2: MIDI clock divider 0x61 (driver value)')
    drain(bar, log, 'before reset')
    w(bar, log, MIDI_CMD, 0xff, 'MPU-401 reset')
    time.sleep(0.01)
    drain(bar, log, 'after reset (0xfe = acknowledge)')
    w(bar, log, MIDI_CMD, 0x3f, 'MPU-401 UART mode')
    time.sleep(0.01)
    drain(bar, log, 'after UART mode (0xfe = acknowledge)')
    # bit rate: 30 active-sensing bytes (0xfe, harmless to any synth) back to back
    waits = send(bar, [0xfe] * 30)
    busy = sorted(waits[2:])
    log(event='timing', waits_us=waits, median_us=busy[len(busy) // 2],
        note='one MIDI byte (10 bits at 31,250 baud) takes 320 us')
    if notes:
        scale = [60, 62, 64, 65, 67, 69, 71, 72]
        send(bar, [0xc0, 0x00])                     # program 0 (piano)
        for n in scale:
            send(bar, [0x90, n, 100])
            time.sleep(0.35)
            send(bar, [0x80, n, 0])
            time.sleep(0.05)
        send(bar, [0xb0, 0x7b, 0x00])               # all notes off
        log(event='notes', played=scale)
    time.sleep(0.05)
    drain(bar, log, 'after sending (anything here came back on MIDI in)')
    log(event='mpu_after', status=hex(status(bar)), irq_source=hex(bar.rd(IRQ_SOURCE)))


def game(bar, log, seconds):
    w = sp.w
    log(event='game_before', legacy=hex(bar.rd(GAME_LEGACY)), ctrl2=hex(bar.rd(CTRL2)),
        axes=[hex(bar.rd(GAME_AXIS + 4 * i)) for i in range(4)])
    # legacy mode: trigger the one-shots and time each axis bit
    w(bar, log, CTRL2, bar.rd(CTRL2) & ~CTRL2_ADC, 'ctrl2: game port legacy (timed) mode')
    for trial in range(3):
        bar.wr(GAME_LEGACY, 0xff)
        t0 = time.perf_counter()
        first = bar.rd(GAME_LEGACY) & 0xff
        done = [None] * 4
        while time.perf_counter() - t0 < 0.02:
            v = bar.rd(GAME_LEGACY) & 0xff
            t = (time.perf_counter() - t0) * 1e6
            for i in range(4):
                if done[i] is None and not (v >> i) & 1:
                    done[i] = round(t, 1)
            if all(d is not None for d in done):
                break
        log(event='game_oneshots', trial=trial, first=hex(first), last=hex(v), buttons=hex((~v >> 4) & 0xf),
            axis_us=done, note='None = never fell within 20 ms (nothing plugged in)')
    # ADC mode: the four 13-bit axis registers
    w(bar, log, CTRL2, bar.rd(CTRL2) | CTRL2_ADC, 'ctrl2: game port ADC mode')
    time.sleep(0.03)
    last, t0, n = None, time.time(), 0
    log(event='game_sampling', seconds=seconds, note='move the stick and press the buttons now')
    while time.time() - t0 < seconds:
        cur = tuple(bar.rd(GAME_AXIS + 4 * i) for i in range(4)) + (bar.rd(GAME_LEGACY) & 0xff,)
        if cur != last:
            log(event='game_adc', t=round(time.time() - t0, 3), axes=[c & 0x1fff for c in cur[:4]],
                raw=[hex(c) for c in cur[:4]], buttons=hex((~cur[4] >> 4) & 0xf))
            last = cur
            n += 1
            if n > 400:
                break
        time.sleep(0.02)
    w(bar, log, CTRL2, bar.rd(CTRL2) & ~CTRL2_ADC, 'ctrl2: game port legacy mode')


def ports(bar, log):
    try:
        fd = os.open('/dev/port', os.O_RDWR)
    except OSError as e:
        log(event='ports', error=str(e))
        return
    inb = lambda a: os.pread(fd, 1, a)[0]
    outb = lambda a, v: os.pwrite(fd, bytes([v]), a)
    w = sp.w
    old = bar.rd(CTRL)
    log(event='ports_before', ctrl=hex(old), at_330=[hex(inb(a)) for a in (0x330, 0x331)], at_200=hex(inb(0x200)))
    base = (old & ~(CTRL_MIDI_PORT | CTRL_GAME_PORT)) | CTRL_MIDI_EN | CTRL_GAME_EN
    for mp in range(4):
        w(bar, log, CTRL, base | (mp << 5) | (1 << 9), f'ctrl: MIDI enable, MIDI port field {mp}, game port field 1')
        found = {}
        for a in (0x300, 0x310, 0x320, 0x330):
            st = inb(a + 1)
            if st == 0xff:
                continue
            outb(a + 1, 0xff)
            time.sleep(0.005)
            found[hex(a)] = dict(status=hex(st), after_reset=hex(inb(a)), status2=hex(inb(a + 1)))
        log(event='midi_port', field=mp, found=found, mmio_status=hex(status(bar)))
    for gp in range(8):
        w(bar, log, CTRL, base | (3 << 5) | (gp << 9), f'ctrl: game port field {gp}')
        reads = {hex(a): hex(inb(a)) for a in range(0x200, 0x210) if inb(a) != 0xff}
        log(event='game_port', field=gp, non_ff=reads, mmio_legacy=hex(bar.rd(GAME_LEGACY)))
    w(bar, log, CTRL, old, 'ctrl: restored')
    os.close(fd)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd not in ('mpu', 'game', 'ports', 'clear'):
        sys.exit(__doc__)
    if sp.vm_running():
        sys.exit('a QEMU process is running; stop the VM first (this needs the card idle)')
    sp.wake()
    bar = sp.Bar()
    if bar.rd(CTRL) == 0xffffffff:
        sys.exit('the card reads 0xffffffff; see srcprobe.py')
    log = sp.Log(sp.arg('--log', 'legacyprobe-log.jsonl', str))
    log(event='start', cmd=cmd, argv=sys.argv[1:], chip_ctrl=hex(bar.rd(CTRL)))
    if bar.rd(CTRL) & (1 << 21):
        sp.release(bar, log)
    if cmd == 'mpu':
        mpu(bar, log, '--notes' in sys.argv)
    elif cmd == 'game':
        game(bar, log, float(sp.arg('--seconds', 10, float)))
    elif cmd == 'ports':
        ports(bar, log)
    elif cmd == 'clear':
        sp.w(bar, log, MIDI_CMD, 0xff, 'MPU-401 reset')
        sp.w(bar, log, CTRL2, bar.rd(CTRL2) & ~CTRL2_ADC, 'ctrl2: game port legacy mode')


if __name__ == '__main__':
    main()
