---
name: profile
description: Build a target profile from 3 to 5 lossless reference tracks and compare a mix or master to it. Use when starting a profile or judging a step against one.
---

- Put the references in `profiles/<name>/references/`, then run `uv run .agents/skills/profile/profile.py profiles/<name> <sample_rate>` with the stems' sample rate. It rewrites `profile.json` and keeps `master_integrated_lufs`, which only the owner sets.
- Tone and width are measured on the loud parts, loudness and dynamics on the whole track. Measure the mix or master with `analyze.py --loud` to compare.
- Quote each value against the references' min to max and how far outside it is.
- In mix reviews, count a band as outside only when it is more than 1.2 dB past the range, and flag width only above the widest reference: limiting a mix moved its bands up to 1.2 dB and always widened it. Judge the master against the plain range.
- Judge a step's own effect with whole-file before and after numbers.
