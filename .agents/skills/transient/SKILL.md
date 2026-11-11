---
name: transient
description: Raise or soften drum attack with Airwindows Point. Use when attack is still low after blend, EQ and compression.
---

- Airwindows `Point` takes `input_trim` (-12 to 12 dB, default 0), `point` (-1 to 1; above 0 adds attack), and `reaction_speed` (0 to 1, default 0.5; not a time).
- Try `point` 0.25 and 0.5 with `reaction_speed` 0.5 first. On song 000 at 0.5/0.5, median attack rose 0.29 to 0.37 dB on kick, snare and tom, tails fell up to 0.38 dB, and the level rose about 1 dB. A speed of 0 or 1 can turn the attack change negative. `point` 1 added over 20 dB of level and 30 dB of peak: never use it.
- `input_trim` only changes level: ±6 dB moved the output ±6 dB and left attack and tail as they were.
- Point is not attack-only and changes level, so measure with hits fixed on the input: `uv run .agents/skills/analyze/hits.py <without.wav> <with.wav>` (add `--times-from` a tap of the sample stem when the mic has bleed). Keep it only if `change.attack_db` rises and `change.tail_db` doesn't fall more than the chain allows, then match loudness (see `saturate`) before judging the bus and the mix.
- No latency; mono and each stereo channel are processed on their own.
