---
name: saturate
description: Add saturation, tape, console, distortion or clipping and judge it at equal loudness. Use when a chain move calls for any of them.
---

- Every one changes loudness, so judge it only at equal loudness. Render the target's tap without the step, then with the step and a `Gain` (`gain_db` 0) right after it. Copy each tap aside before the next render. Run `uv run .agents/skills/saturate/match.py <without.wav> <with.wav> [<start_s> <end_s>]` over the sections you judge, put its `match_gain_db` into the `Gain`, re-render, and confirm `loudness_change_lu` is within 0.1. If a dynamics step follows on the same target, repeat until it holds. Then compare peaks, bands, attack and trial-limiter dip.
- `match.py` also prints `lag_samples` (positive: the file with the step is late, so move it earlier or delay the dry path), `lag_samples_by_band` and `polarity_inverted`. Fix a lag before blending a path with its dry signal, or it comb-filters, but only when both bands agree: CHOW's low-band phase shift reads as a lag of 100-180 samples (and can read as inverted) while its highs are on time, and shifting it breaks the highs.
- Starting settings and their loudness change on a drum bus (at -6 / 0 / +6 dB input), to spot gross jumps; measure your own target:
  - `T-Saturator` `type` Tube, `saturation` 20: -0.1 LU. Tape 20: +1.5 LU. Its drive does not depend on input level, so set the amount with `saturation` and `type`, not a gain before it. `saturation` 0 still colours (`smash` and `crunch` stay on). `auto` doesn't match loudness. `make_up_db` is exact dB.
  - `CHOWTapeModel` defaults (`tape_drive` 0.5): -1.7 / -1.3 / -1.1. `tape_drive` is not monotonic (0.1 gives -14.8, 1.0 gives +4.2). It darkens 4-8 kHz as input rises. Its tape stage shifts phase below about 120 Hz, so a blend with its own dry signal loses low end; measure that band.
  - Airwindows `Console7Channel`: `fader` 0.772 is unity. `Console7Buss`: `master` 0.772 gives -2.0, 1.0 gives +0.3. `BussColors4` Dark, trims 0, `dry_wet` 1: -1.0 / -1.3 / -1.9.
  - `ToTape6` `input` 0, `soften` 0.5, `head_b` 0.5, `flutter` 0, `output` 0, `dry_wet` 1: +5.7 / +5.5 / +4.6, and 4-8 kHz falls 2.8 dB. `Density2` `density` 0.5, `highpass` 0, `output` 1, `dry_wet` 1: +2.2 / +2.0 / +1.7.
  - `ADClip8` mode `Normal`, `boost` 0, `ceiling` 1.0 does nothing below 0 dBFS; `boost` adds dB for dB before the clip; `ceiling` 0 clips at -1.84 dBFS. Mode `Atten` takes the boost back off after the clip. Never use mode `Clips`: it outputs only what was clipped.
  - `Distortion` default `drive_db` 25 adds 17 LU; `drive_db` 6 adds about 5.5. `Clipping` (`threshold_db`) does nothing below its threshold and has no makeup.
- All but T-Saturator drive harder as their input gets louder: set the drive with a `Gain` before them, Airwindows `input_level` (dB), or CHOW `input_gain`.
- Latency the render does not compensate: `ADClip8` 1 sample (2 in mode `Afterbr`); `ToTape6` about 16 samples, moving, when `flutter` is above 0, so keep `flutter` 0 on a parallel path. The rest have none.
- Aliasing on a loud 7 kHz tone: T-Saturator `quality` good (default) -74 dB, ultra -109 dB; CHOW default -52 dB, 8x -74 dB. `Distortion`, `Clipping` and Airwindows don't oversample (-10 to -28 dB when driven hard), so keep them light or filter above the band they should add to.
- Dry/wet: T-Saturator `mix` (%), CHOW `dry_wet` (%), BussColors4, ToTape6 and Density2 `dry_wet` (0-1). Console7, ADClip8, `Distortion` and `Clipping` have none: use a send to a parallel bus.
