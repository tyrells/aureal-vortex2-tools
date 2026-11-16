# Aureal Vortex 2 technical reference

What an Aureal Vortex 2 (AU8830, as on the SQ2500) does, and how Aureal's Windows 98 driver drives it, **reverse-engineered from a real card**. Aureal never published this, and nothing here comes from its source code or internal documents. It is written to be enough to emulate the chip or to replace its driver. Facts come from six places, and each section says which:

- the Linux ALSA `au88x0` driver, which names most registers;
- measurement of a real SQ2500 under Aureal's driver 4.06.2048, with every register access traced and the output recorded;
- host-side experiments that read and write the card's registers directly while it plays;
- an independent reverse engineer's register map of the chip and descriptions from Aureal that its author relayed, used as leads and checked against the card;
- Aureal's archived A3D SDKs, including the debug build of the 3.0 library, for what the library sends the driver;
- recordings of period PCs with the card playing games, to compare the emulator against.

Each finding is tagged **Confirmed**, **Strong**, **Plausible** or **Hypothesis** by how well it is established. Where the model still rests on an assumption, the text says so.

## Documents

- **[using-86box.md](using-86box.md)**: user guide to the 86Box emulation (setup, settings, limitations, reporting problems).
- **[reference.md](reference.md)**, the main document:
  - PCI identity, the host interface and the address map, with reset values;
  - the AC'97 codec;
  - the A3D engine: memory layout, HRTF filters, gain and atmospheric filter, glides, the driver's conversions from API settings, reflections, the crosstalk canceller, four-speaker output and the EQ;
  - what Aureal's A3D library sends the driver, and its software renderer;
  - routing, sample-rate conversion, DirectSound buffers, Sound Blaster and MIDI, wavetable voices, A3D 3.0 and the driver's reverb, comparisons with period machines, clocks, interrupts and DMA;
  - what an emulator needs, prior work, and open questions.

## Appendices

| | Appendix | Contents |
| --- | --- | --- |
| A | [Direction table](appendix-a-direction-table.md) | The structure of the driver's HRTF and ITD table, its interpolation, and how to read it out |
| B | [Crosstalk canceller](appendix-b-crosstalk-model.md) | The canceller's decoded arithmetic, Aureal's three speaker presets, and the evidence |
| C | [Measuring and testing](appendix-c-workflow.md) | Traced sessions, probes, the test programs, recording a period machine and trace replay, step by step |
