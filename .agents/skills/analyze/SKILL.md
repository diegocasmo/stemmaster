---
name: analyze
description: Measure one WAV file (a stem, mix, master, or reference). Use to judge a step or compare against a profile.
---

- Run `uv run .agents/skills/analyze/analyze.py [--loud] <file.wav>`. It prints JSON and writes nothing.
- `--loud` measures the band and stereo fields on the loud parts only (3 s windows within 3 LU of the loudest), as a profile does, and adds `loud_seconds`. Loudness and peak fields stay whole-file.
- Copy the numbers you need into the step's review result in `recipe.json`.
- Fields:
  - `integrated_lufs`: loudness of the whole file.
  - `true_peak_dbtp`: peak between samples (4x oversampling). It can read up to about 0.5 dB low, so always check the exported file.
  - `peak_to_loudness_db`: how squashed it is. Lower means more compressed.
  - `max_short_term_lufs`: loudness of the loudest 3 seconds, which is what a clip plays.
  - `bands_db`: energy per octave band in dB, relative to the whole file. `partial_bands` reach past the file's highest frequency.
  - `side_mid_db`, `side_mid_below_120hz_db`: stereo width. Higher means wider.
  - `correlation`: 1 is mono, 0 is unrelated sides, below 0 cancels in mono.
- `null` means it can't be measured: silent, too short (under 0.4 s, or 3 s for short-term), mono for stereo fields, or no side signal at all.
- Compare files at the same sample rate and over the same section of the song.
- Copying a mono file unchanged to both stereo channels makes it read 3 dB louder. Panning it center at -3 dB per side keeps the same reading.
- Only mono and stereo files are supported.
- For valley, overlap, limiter dip, section loudness and profile range (defined in `docs/mixing-chains/README.md`), run `uv run .agents/skills/analyze/measure.py` with `valley --times <hits.json> <file>`, `overlap [--band <low> <high>]... [--times <hits.json> --window <from_ms> <to_ms>] <a> <b>`, `dip --times <hits.json> <master-in.wav> <trial.wav>`, `sections [--band <low> <high>] <recipe.json> <file>` or `range [--profile <name>] <file>`. A band edge of 0 leaves that side open. Times come from a `hits.py` run.
- For drum hits, run `uv run .agents/skills/analyze/hits.py <input.wav> [<output.wav>]`. It finds hits once, on the input (a jump of 12 dB, down to 45 dB under the loudest), and prints each measure's p0/p10/p50/p90: `level_db` (loudest 1 ms in the first 25 ms), `attack_db` (energy 0-25 ms over 30-100 ms), `tail_db` (80-200 ms over 0-30 ms), `off_hit_level_db` (1 ms levels from 20 ms before to 200 ms after no hit, floored at -120), and `bleed_db` (mean power off-hit over the first 100 ms of hits, overall and per band). With an output file it prints `change` on the same hits, so soft hits a processor lost still count, and the `after` values.
  - The detector misses some hits that follow another within about 80 ms, and on a mic it also fires on bleed: on song 000 it found 934 of 938 kicks in the kick sample, but missed 15 and added 28 on the kick-in mic. So take times from the cleanest stem (a sample stem), check `hits` against the expected count on a fast passage, and freeze them.
  - `--times-from <clean.wav>` detects on another file. It must be on the same timeline: render it with the same recipe and clip (a `--tap`), not the raw stem, once offsets are set.
  - `--times <hits.json>` reuses the `times_s` of an earlier run, so every trial is measured on the same hits.
  - `--min-level-db <dB>` keeps only hits at least that loud, so bleed onsets count as off-hit.
  - `--list` adds every hit's time and measures.
