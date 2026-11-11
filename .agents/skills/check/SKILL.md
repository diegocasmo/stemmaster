---
name: check
description: Check that a song's recipe records every chain move for every source, and compute the measured report reviewers judge it by. Use before any mix or master review.
---

- Run `uv run .agents/skills/check/check.py [--master] songs/<id>/recipe.json`. It exits 1 and lists every failure, or writes `renders/check/report.md` and `report.json`. `--no-report` runs only the structural checks.
- It lints the `### <chain>.<move>` headings of `docs/mixing-chains/` and reads each move's **Tool** list.
- `sources` names each instrument: `{"kick": {"chain": "kick", "stems": [...], "timing": "03_KickSample.wav"}}`.
  - Every audio file in `stems/` is in one source or in `excluded`, as `"vocal"` or `{"guitar-di": "<amp stem in a guitars source>"}`. Every recipe stem is in a source.
  - The chain must match the file name (kick, snare, tom, overhead/hat/cymbal/room, bass, guitar, synth/pad/keys/sfx), or the source gives `chain_reason`.
  - Each tom is its own source. Any other chain has one source, unless each of its sources gives `split_reason`.
  - A source with several stems routes them to one bus that nothing else feeds: its node, where the report measures it.
  - `timing` is the cleanest stem to find drum hits on, a sample stem if there is one. Hits are found on it unprocessed, at its offset.
- `moves` records every move of every source's chain, plus `drum-bus` (when there are drums), `mix-bus`, and with `--master` `master`: `{"move": "kick.gate", "source": "kick", "decision": "kept", "steps": ["gate-kick"], "fields": [], "note": "..."}`.
  - `decision` is kept, changed or dropped. A dropped move claims nothing. `note` gives the measured reason.
  - kept and changed claim at least one step whose plugin (or Airwindows `effect`) is in the move's Tool list, or a field its Tool names. `Gain` steps may ride along. Anything else needs `tool_reason`, which the check prints for the reviewers.
  - `fields` are `stems.<file>.invert`, `.offset_samples`, `.pan`, `.gain_db`, `.bus`, `buses.<bus>.bus`, and sends as `stems.<file>.sends.<bus>` or `buses.<bus>.sends.<bus>`.
  - Every step, every set `invert`, `offset_samples` and `pan`, and every send is claimed by at least one record. Routing (`bus`) may be claimed, as by `drum-bus.groups`; each source's node already checks it. Several records may claim one shared step, as each tom claims the tom-bus compressor.
  - A record acts only on its source's stems, buses fed only by its chain's stems, or a `Duck` keyed from them; a Duck on another source is printed for the reviewers. `drum-bus` acts on buses fed by several drum chains and on drum returns. `mix-bus` and `master` act only on `mix`.
  - Master moves are rejected until `--master`. They claim only master-stage steps, and with `--master` every master-stage step belongs to a master move, not the trial master.
  - `{"move": "balance", "steps": [...], "fields": [...], "note": "..."}` claims `Gain` steps and stem `gain_db` or `pan` that no chain move covers.
- The report renders the recipe once, then once without each kept or changed record's claims (steps removed, fields at their defaults; a claimed `gain_db` stays on and is listed), and lists every number that moved.
  - Per source node, and per other bus a step acts on (such as a shared tom bus): loudness, bands, width, section loudness, and on the frozen hits level, attack, tail, spread, bleed, and hit level over the node's loudness (peaks at equal loudness).
  - For the mix: bands and width on the loud parts, valley, and kick-bass, bass-guitar and synth-guitar overlap.
  - For the trial master, solved to the current trial's loudness (`matched` false if it can't be): limiter dip on kick and snare hits across the current last master step, loudness, true peak and profile range.
  - Results are cached in `renders/check/` by everything they depend on: recipe, hits, code, stems, plugins and profile.
- Judge each decision against its move's Needed if and Worked if from these numbers. The report does not decide; the review does.
