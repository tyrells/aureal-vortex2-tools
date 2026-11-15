# Appendix B: the crosstalk canceller

The Vortex 2 has a speaker crosstalk canceller at `0x24000–0x25fff` between the A3D engine and the mixer ([reference](reference.md#reflections-stereo-mode-and-speaker-output)). For loudspeakers it feeds each side an inverted, delayed, low-passed copy of the other side, so that the HRTF cues reach each ear without being smeared by the opposite speaker. Aureal's control panel picks one of three coefficient sets, which are the tables in ALSA's `au88x0_xtalk.c`:

| Control panel setting | ALSA table | Delay | EQ gain k, shift | XT gain k, shift |
| --- | --- | --- | --- | --- |
| Satellite speakers (the default) | `Wide` | 13 samples | `0x269c`, 1 | `0xf25e`, 0 |
| Monitors | `Narrow` | 7 samples | `0x468d`, 3 | `0xf82e`, 0 |
| Headphones | `Pipe` | 0 | 0, 1 | 0, 0 |

The arithmetic is decoded and checked against a real card (Confirmed). The 86Box model and the reference implementation here run the register contents as the chip does; they no longer use measured filters.

## The arithmetic

    out_L = EQ_L(mix_L) + XT_R(delay_R(EQ_R(mix_R)))
    out_R = EQ_R(mix_R) + XT_L(delay_L(EQ_L(mix_L)))

    block(x) = 2^shift × (k·x + the sum of the block's five sections, each fed x)
    section:   y[n] = b0·x[n] + b1·x[n−1] + b2·x[n−2] + a1·y[n−1] + a2·y[n−2]

- **Input mix.** Each side sums five inputs (the four A3D slices and a spare), each times its input gain: ten 1.15 words at `0x244d0–0x244f4`. The driver writes `0x3fff` (0.5) for the four slices and 0 for the spare.
- **Blocks.** There are four: EQ left (`0x24200`), EQ right (`0x242b4`), XT left (`0x24368`) and XT right (`0x2441c`). Each has five sections of nine words (a1, a2, b0, b1, b2, then the chip's own y1, y2 and two error-feedback words), a gain k and a shift (`0x24538` + 8 × block, and + 4). Coefficients and k are signed 2.14.
- **The sections are in parallel, and k is a direct path.** All five sections filter the block's input and their outputs are added, together with k times the input. The whole sum is shifted left by the shift word. This is a partial-fraction form: k is its constant term. (In every preset b2 is 0, as a partial-fraction section's would be.)
- **The cross path** takes the other side's EQ output through a delay line of up to 31 samples (`0x24660`: left bits 17–13, right 12–8) and that side's XT block. "XT left" filters the left signal into the right output.
- **Headphones** are the same arithmetic: flat sections adding up to 1.25, k = 0, shift 1, and an empty XT block. That is a plain gain; nothing is bypassed.

## Evidence

All from a real SQ2500 under driver 4.06.2048, recorded through the line input.

| Check | Result |
| --- | --- |
| Same-side response of satellites relative to headphones (runs 2 and 3, seven positions each, same recording gain), against (k + sections) × 2^shift from the registers | Shape 0.22 dB rms, 200 Hz–10 kHz; level factor 1.02 with nothing fitted |
| The same for monitors (run 5) | 0.47 dB rms; level factor 0.99 (so a shift of 3 is exactly ×8) |
| Cross path relative to same side, against (k + sections) of the XT block and the register's delay | 0.58 dB rms below 3 kHz; scale 0.98; delay 13.07 and 7.06 samples (registers: 13 and 7) |
| A front source in satellites mode against a native click in the same recording, in absolute level | Within 0.3 dB in each of nine bands from 200 Hz to 7.5 kHz |
| Host writes of single coefficients while a click played (run 11) | Adding to a section's b0 moved the output by exactly that much of the k path's size; b1 and b2 behaved as one- and two-sample delays; a1 and a2 as feedback of the stated sign and scale; the output grew linearly in k and doubled with shift 0 → 1 |
| With every k at 0 and the satellites sections still loaded (run 11's "pass-through" step) | The cross path was still there at 1.02 × the XT sections, delayed 12–13 samples: k is not a gain on the sections |

The last row is what the earlier attempts missed. Treating k as the gain of the section sum made the sections look unresponsive to edits and made the monitors preset come out 4.9 dB wrong; treating it as the direct term removes both problems and the unexplained constant the partial decode needed.

**Level of the 3D path (Confirmed).** In headphone mode a 3D click (source in front, gain 1.0 and 0.5) came out +1.37 dB against a native click in the same recording, the same in every octave from 300 Hz to 10 kHz, taking the HRTF taps as Q15. The arithmetic above gives headphones × 1.25 (0.5 × 2 × 1.25) and the driver's `0x0a` on the canceller's mixer inputs is another × 1.25, which is +3.88 dB. The measured figure is that times 0.75 (75/64 = +1.38 dB). Where the 0.75 arises (the A3D engine's output scaling, most likely) is not known; the model applies it at the canceller's output.

**An earlier level correction was wrong.** Before the decode, the model's measured filters were scaled down by 5.5 dB (satellites) and 6.2 dB (monitors) on the strength of a DirectSound3D comparison between a real card and 86Box on different machines. The three in-session measurements above all say the speaker modes are not quieter than headphones: on average the same-side gain equals the headphone gain. The 86Box model was therefore about 5.6 dB too quiet on 3D sound in both speaker modes until this change. The likely cause is a volume setting that differed between the two machines.

**What is not modelled.** The chip computes in fixed point (16 × 16-bit multiplies into a 40-bit accumulator, double-precision states with error feedback, per Aureal's description relayed by the author of a register map of the chip); the model uses floating point, so it is not bit-exact. The enable bit (bit 0 of `0x24660`) is ignored: the driver only drops it while loading a preset.

## Files

| File | Contents |
| --- | --- |
| `data/xtalk/vortex_xtalk.py` | Reference implementation of the arithmetic with the three presets: `process(in_l, in_r, mode)` |
| `data/xtalk/vortex_xtalk_fir.npz` | The measured 512-tap responses the model used before (superseded; normalised, so their absolute level is not meaningful) |
| `core/snd_vortex2_core.c` | The same arithmetic in C (`xt_run`), reading the live registers, so it follows whatever a driver loads |
| `tools/vortex-poke.py` (`xtsweep`, `xtident`, `xtident2`, `xtshift`) | The host-side experiments |

## The EQ block

The chip's ten-band EQ at `0x2b000` is a separate block in the 2D path and is left transparent by the Windows driver; the [reference](reference.md#reflections-stereo-mode-and-speaker-output) describes the band model for software that programs it.
