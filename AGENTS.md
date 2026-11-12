# AGENTS.md

Mix and master a song from its aligned audio stems. Signal processing only: never create or generate audio.

## Layout

- `songs/<id>/recipe.json`: the target profile and ordered processing steps (plugin, settings, reason, review result).
- `songs/<id>/stems/`, `renders/`, and `references/` (the song's original files, for reference only): git-ignored.
- `profiles/<name>/`: target sound measured from 3 to 5 lossless reference tracks, kept in `profiles/<name>/references/` (git-ignored). `profiles/<name>/profile.json` holds the targets, including `master_integrated_lufs`.
- `plugins/`: VST3 bundles copied from the owner's production machine (git-ignored).
- `docs/mixing-chains/`: the standard chain for each source, one file per source, each move with a reason, starting settings and measured checks.

## Skills

- One job per skill, in `.agents/skills/<name>/SKILL.md`, kept short and never repeating this file.
- Plain names. Frontmatter holds only `name` (same as the folder) and `description`.
- One skill per processor (eq, compress, limit, ...), with its presets and scripts next to its `SKILL.md`.
- A skill never pushes, publishes, or merges unless that is its stated job.
- Every change (songs, skills, docs, code) runs through `start`.

## Rules

- The recipe is the source of truth. Renders are a cache rebuilt from the stems. To undo a step, remove its entry.
- Install tools with `./setup.sh` and run Python with `uv run`.
- Test code may build signals as test inputs, never in a song.
- Process only with Python and pedalboard. Use a plugin from `plugins/` or a pedalboard built-in, whichever fits the step. Use a plugin only if it has an Apple Silicon VST3, loads without its window, and renders identically twice in fresh runs. Gain, pan, summing, sends, level rides (`Ride`) and keyed ducking (`Duck`) happen in the render script.
- Mix with the chains in `docs/mixing-chains/`. Read its README and the chain of every source in the song before planning, and follow its done rule: every move that applies is recorded in the recipe, by id, as kept, changed or dropped, with measurements.
- Order steps to suit the song, except:
  1. Gain staging comes first.
  2. The master limiter is the last processing step.
  3. Mastering starts only after the mix passes its adversarial review. A trial limiter to test headroom while mixing is allowed, but it is not the master.
- Judge each step by its measurements (loudness, true peak, dynamics, tonal balance, stereo width), against the previous step, and against the recipe's profile once it has one.
- Run an adversarial review (Claude and Codex through Herdr) on each completed mix or master version.
- Never wait for the owner to listen or approve. A mix or master is done when it passes its measured checks and its adversarial review.
- Export the master to `songs/<id>/renders/` as a 24-bit WAV at the stems' sample rate, with true peak at or below -1 dBTP and integrated loudness from the profile.
