# Mix bus

Goal: glue the mix gently and judge every move at release loudness. These steps are part of the mix; the master comes later.

### mix-bus.trial-master
- **Why:** a move that sounds right at mix level can fall apart when the limiter pushes it 10 dB. Mixing into a trial limiter shows it early.
- **Start:** a `Gain` and a `ZL Limiter` as `stage: master` steps, driven to the profile loudness, on from the first step. `mix.wav` is then the mix before the master steps and `trial.wav` the trial master; limiter dip is measured across the limiter alone (`--tap master-in`). Judge it on whole-song renders. It is a trial, not the master.
- **Tool:** `Gain`, `ZL Limiter`.
- **Needed if:** always.
- **Worked if:** every step's review includes trial-master numbers.

### mix-bus.console
- **Why:** console saturation adds density and transient control across the whole mix.
- **Start:** console buss emulation, low drive, gain staged into it.
- **Tool:** Airwindows `Console7Buss`, `BussColors4`.
- **Needed if:** always tried; drop it if it costs attack.
- **Worked if:** at equal loudness, the trial limiter does less and kick and snare attack hold.

### mix-bus.compress
- **Why:** a slow-attack bus compressor glues the parts and controls the mix's level. Some engineers find it makes little difference, or use none.
- **Start:** VCA, attack 30 ms, auto or fast release, 2:1 (4:1 for dense songs), 2-4 dB.
- **Tool:** `TDR Kotelnikov`, Airwindows `Logical4`.
- **Needed if:** the mix's level spreads more than 3 dB within a steady section.
- **Worked if:** spread falls, kick and snare attack hold, and section loudness differences hold.

### mix-bus.tone
- **Why:** gentle shelves set the mix's overall weight and air.
- **Start:** low shelf at 60-125 Hz and high shelf at 10-16 kHz, 1-2 dB each. Some engineers push the high shelf further.
- **Tool:** `LowShelfFilter`, `HighShelfFilter`, `TDR VOS SlickEQ`.
- **Needed if:** the mix's lowest or highest octave bands are outside the profile range.
- **Worked if:** those bands move into range.

### mix-bus.rides
- **Why:** sections should differ in loudness where the arrangement does, and match where it doesn't.
- **Start:** mark the song's `sections` with their `loudness_lu` (against the mix's integrated loudness), then ride the mix bus to match.
- **Tool:** `Ride` on `mix`.
- **Needed if:** a section's short-term loudness is more than 1.5 LU off its intended loudness.
- **Worked if:** every section is within 1.5 LU of its intended loudness.
