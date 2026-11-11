---
name: limit
description: Bring a reviewed mix to the profile's master loudness under -1 dBTP. Use to master.
---

- Start from `docs/mixing-chains/master.md`. Add `"stage": "master"` steps on `mix`: one `Gain` first, then any width or tone fix, then `ZL Limiter` last.
- Run `uv run .agents/skills/limit/solve.py songs/<id>/recipe.json` after any master change. It finds the `Gain` that hits the profile's loudness and saves it. Then render: the master steps go to `trial.wav`.
- A plain render writes `trial.wav` whatever its loudness and true peak; only `--master` refuses a master off target. If the true peak fails, lower the limiter's ceiling 0.1 dB and solve again.
- `ZL Limiter` needs a ceiling below -1.0 (at -1.0 it measured -0.94 dBTP). Start from attack 5, release 30, recovery 3 ms. Slow recovery (10 ms or more) holds a steady gain reduction, so the solver drives it 10 dB harder for the same loudness.
- Limiting widens a mix, because it turns down the centered drums and bass more than the sides. Check width against the profile and fix it before the limiter.
- Keep the master steps in the recipe while mixing, as the trial limiter. Once the mix passes its review, render with `--master` to export `master.wav`. Measure the limiter's cost (solved gain minus the loudness it gained), the dip on each kick and snare hit, kick and snare crest loss against the mix (at most 3 dB), and section spread against the guitars and bass.
