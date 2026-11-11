---
name: compress
description: Control uneven levels or shape attacks with compression. Use when a stem or bus needs less dynamic range.
---

- Start from the source's chain in `docs/mixing-chains/`. Check individual hits or notes: more hits can raise section loudness without louder hits.
- Use `Compressor` with explicit `threshold_db`, `ratio`, `attack_ms`, and `release_ms`. Set threshold against the level entering the compressor; ratio 1 does nothing.
- Pedalboard 0.9.26 has a hard knee, a rectified peak envelope, independent stereo channels, and no automatic makeup gain.
- Its envelope time constants are the labeled milliseconds divided by about 6.28. Gain-reduction timing also depends on level and threshold; do not copy another compressor's timings unchanged.
- For bus glue, use `TDR Kotelnikov`: its default `mode` "Stereo" links both channels. Set `sc_hp_slope` to "12dB/Oct" (the default "3dB/oct" barely filters) so the kick drives less gain reduction and measure the dip on each kick hit. On this song a mix-bus compressor at 4 dB gain reduction flattened the sections it was meant to glue.
- Keep input `gain_db` fixed while tuning compression; set stem output balance with a `Gain` step after the compressor. It still feeds any bus or mix compressor.
- For parallel compression, use the plugin's own blend: Kotelnikov `dry_mix_mode` "Dry Wet" with `dry_wet`, Molotok `drymix`, or ZL Compressor `wet_wet`.
- Measure each change per hit (peak, body level, crest), per section (3 s loudness), and at the trial limiter (see `limit`). Recheck earlier EQ on the compressed target.
