---
name: eq
description: Shape tone or cut ringing with EQ. Use when a stem, bus, or mix needs a tonal change.
---

- Start from the source's chain in `docs/mixing-chains/`. Cut before boosting: a cut removes a problem without making the target louder.
- Find the problem first: run `uv run .agents/skills/eq/spectrum.py <wav> <start_s> <end_s> <low_hz> <high_hz>` on the mix and on stems or isolated renders. It prints 1/12-octave energy relative to that file, not each source's share of the mix.
- Compare the mix's `analyze.py --loud` octave bands with the profile's range (see `profile`), not its mean. Use `spectrum.py` only to find where inside an octave a problem sits; below about 100 Hz its fine bins show each song's notes, not its tone.
- Use a broad cut (Q about 1) for excess weight and a narrow one (Q about 4) only for a ring. A spectral peak alone is not a fault.
- EQ a multi-mic source (drums) on its bus, so every mic and its bleed get the same filter and their phase stays matched. Move a source the cut should skip (toms under a snare cut) to its own bus, and copy the other bus steps it still needs.
- A boost makes that target louder against everything else. Re-measure the balance and record the change instead of trimming it away by default.
- `PeakFilter`, `LowShelfFilter`, and `HighShelfFilter` take `cutoff_frequency_hz`, `gain_db`, and `q`. Keep shelves at `q: 0.7071`; higher Q overshoots.
- `HighpassFilter` and `LowpassFilter` take only `cutoff_frequency_hz`: 6 dB per octave, -3 dB at cutoff. Two in a row give 12 dB per octave and -6 dB at cutoff.
- Built-in bells and shelves are not exact copies of `ZL Equalizer 2`'s: gentle settings matched within about 0.1 dB, but stronger or higher bands differed by up to 2.4 dB at 16 kHz. Measure before swapping one for another. Use a `ZL Equalizer 2` dynamic band only when a band should react to excess energy. Read each parameter back after setting it, and turn off `auto_gain_auto_gain` and `static_gain_static_gain` while judging.
