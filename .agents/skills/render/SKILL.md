---
name: render
description: Render a song's recipe into a stereo mix from its stems. Use after any recipe change, before analyze.
---

- Run `uv run .agents/skills/render/render.py [--master] [--tap <name>]... songs/<id>/recipe.json [<start_s> <end_s>]`. It writes `songs/<id>/renders/mix.wav` as 32-bit float, so nothing clips before the limiter.
- Pass start and end seconds to render only the clip you are working on. The whole song is still processed, so a clip sounds exactly like that part of the full render.
- `--tap <name>` also writes a stem's (after its steps and pan) or a bus's output from the same render to `renders/tap-stem-<name>.wav` (without the stem's `.wav`) or `renders/tap-bus-<name>.wav`, to measure a source or bus inside the full routing. On a whole-song render, `--tap master-in` writes `renders/tap-master-in.wav`, the input of the last master step, to measure the limiter alone against `trial.wav`.
- With `"stage": "master"` steps, a whole-song render also writes `trial.wav`, 24-bit at the stems' rate, and prints its loudness and true peak. `--master` writes `master.wav` instead, and only if it is within 0.005 LU of `master_integrated_lufs` in `profiles/<profile>/profile.json` and at or below -1 dBTP; otherwise the old `master.wav` stays. The trial limiter used while mixing goes here too, so `mix.wav` is the mix before it. Use `--master` only after the mix passes review.
- Recipe format:

```json
{
  "profile": null,
  "tail_seconds": 0,
  "sections": {
    "verse1": {"start_s": 0, "end_s": 31.2, "group": "verse", "loudness_lu": -1},
    "blast1": {"start_s": 31.2, "end_s": 48.9, "group": "blast", "loudness_lu": 1}
  },
  "stems": {
    "01_KickIn.wav": {"gain_db": 0, "pan": 0, "invert": false, "offset_samples": 0, "bus": "kick"},
    "06_SnareUp.wav": {"bus": "snare", "sends": [{"bus": "drum-verb", "gain_db": -12}]}
  },
  "buses": {
    "kick": {"bus": "drums"},
    "snare": {"bus": "drums"},
    "drums": {},
    "drum-verb": {"bus": "drums"}
  },
  "steps": [
    {"id": "drums-glue", "target": "drums", "plugin": "Compressor", "settings": {"threshold_db": -18, "ratio": 4}, "reason": "...", "review": "..."}
  ]
}
```

- Only listed stems play. Every stem field is optional: unity gain, center, normal polarity, no offset, straight to `mix`.
- `gain_db` is the input trim, before the stem's steps. For a level after the steps, end them with a `Gain` step.
- `pan` runs from -1 (left) to 1 (right). Mono stems use -3 dB at center. Stereo stems use balance.
- `offset_samples` moves a stem later (positive) or earlier (negative). Short stems are padded with silence.
- Each stem goes to a bus or to `mix`. A bus goes to `mix` unless `buses` gives it a parent `bus`, to any depth. Declare in `buses` every bus no stem names directly.
- `sends` copy a stem or bus, after its steps and pan, to another bus at `gain_db`; the source still feeds its own bus. Use them for reverb, delay and parallel paths. A bus fed by a send must be fully wet, or the dry sound is added twice. The render stops on Dragonfly or `Reverb` without `dry_level` 0 and `Delay` or `Chorus` without `mix` 1; set any other effect fully wet yourself. A stem whose `bus` is the reverb bus feeds only the reverb.
- `sections` name parts of the song, without overlaps and within the song. `group` marks sections meant to be equal. `loudness_lu` is a section's intended loudness against the mix's integrated loudness, given for every section or none. The render only validates sections; `Ride` and the chain checks use them.
- A step's `target` is a stem file name, a bus name, or `mix`. Steps run in recipe order on each target: stem steps before pan, bus steps after summing the bus, then `mix` steps.
- A step's `stage` is `mix` (default) or `master`. Master steps target `mix`, run after every mix step, and the last one must be `ZL Limiter` or `Limiter`.
- `plugin` is a pedalboard class name, with `settings` as its arguments, or the name of a bundle in `plugins/`, with `settings` as parameter names from `pedalboard.load_plugin("plugins/<name>.vst3").parameters`. `Airwindows Consolidated` also takes `effect`, an Airwindows effect name; its parameter names depend on the effect, and every one must be set, because switching effects keeps the previous effect's values.
- `prepared.py` whitelists measured plugin bundle identities. The first use of each bundle/dependency identity, ordered settings, rate and channel count loads normally and caches exact native parameter values after the settings loop. Later uses write those values into fresh instances with discovery skipped and require exact readback. Preset selectors (program, preset or bank controls) always load normally, as do changed layouts, failed writes and inexact readback. The bounded cache lives only in this process; no plugin instances or state blobs are saved. Adding a plugin requires identical normal/replayed output bytes across at least four varied settings in fresh processes.
- Two plugins are built into the render. Both change level only. With `band_hz` [low, high] (an open end is 0 or null) they change only that band, without phase shift and never the opposite way: full depth at the band's centre (or below a low-pass edge, above a high-pass edge), less toward the edges. Without it they change the whole signal.
  - `Ride`: level automation. `segments` is a list of `[start_s, end_s, gain_db]` or `[section, gain_db]`; `ramp_ms` (default 10) is a smooth ramp centred on each edge. Segments may touch but not overlap, and each must be at least `ramp_ms` long. Put rides before compressors.
  - `Duck`: keyed gain reduction. `key` is a stem (after its steps, before pan) or a bus; `key_band_hz` filters it. Settings: `threshold_db`, `ratio`, `range_db` (most reduction), `attack_ms`, `release_ms`. One gain for all channels, from the key's loudest channel.
- Mono stems stay mono through their steps, so put stereo effects (reverb, delay, chorus) on a bus.
- `tail_seconds` adds silence at the end so reverb and delay tails can ring out.
- The render stops with an error on unknown fields, unknown targets, keys or sends, a bus nothing feeds, a bus named like a stem or `mix`, any loop through buses, sends or keys, and band edges at or above half the sample rate.
