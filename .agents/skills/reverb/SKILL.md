---
name: reverb
description: Set a reverb return's pre-delay, decay, low cut, level and the song's tail. Use when a chain move calls for reverb, room or early reflections.
---

- Put the Dragonfly on a bus fed by `sends`, with `dry_level` 0 (default 80). Its levels are percentages of amplitude, so halving one is -6 dB. Leave the plugin's levels fixed and set the amount with the send `gain_db` or a `Gain` after it, so the amount never changes the sound.
- Return loudness from a snare sent at 0 dB, at default levels: Plate -16 LU, Room -16 LU, Hall -9 LU, EarlyReflections -11 LU. Set the level by comparing the return's tap with the dry source's tap (`render.py --tap`) over the same section.
- Pre-delay (`predelay_ms`, 0-100) adds to a built-in delay. Plate's `Nested` and `Simple` algorithms start 56 ms late and `Tank` 9 ms. Room and Hall delay only the late part; Hall's early reflections always arrive at about 48 ms, so a pre-delay under 48 ms puts the late part first.
- Decay (`decay_s`, 0.1-10): Hall measures what you set. Plate does from 0.4 to 2 s (8 s measures 7.3 s). Room can't go below about 0.85 s at its default `size_m` 12; lower `size_m` for a shorter room (8 m gives 0.66 s). EarlyReflections has no decay; `size_m` (10-60 m gives 0.1-0.5 s) and `program` set its length.
- Room and Hall: `early_level` and `late_level` set their balance; `early_send` feeds the early part into the late one (at 100 the late part is about 5 dB louder and starts later).
- `low_cut_hz` (0-200) is gentle: at 200 it cuts 30-60 Hz by 12 dB and 60-120 Hz by 7 dB. Plate already defaults to 200, Room and Hall to 4, EarlyReflections to 50. For a steeper cut, add `HighpassFilter` steps after the reverb. `high_cut_hz` defaults to 16 kHz (Hall 7.6 kHz, EarlyReflections 10 kHz).
- Width from a mono source: Room and Hall come out uncorrelated, Plate 0.17, EarlyReflections 0.11. A hard-left source stays left in EarlyReflections, reaches the right at -14 dB in Room and -1.7 dB in Hall, and spreads evenly in Plate.
- A render ends at the song's end, cutting any tail. Run `uv run .agents/skills/reverb/tail.py <plugin> '<settings json>' [<sample_rate>]` with the step's exact settings: it prints `onset_ms`, `decay_rt60_s`, `tail_60db_s`, `tail_90db_s` and the left-right correlation. Set `tail_seconds` to at least the longest return's `tail_90db_s`, plus any delay feeding it.
- `buffer_size_frames` and `sample_rate_frames` change nothing.
