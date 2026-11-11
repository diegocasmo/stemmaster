# Toms

Goal: full, round fills that cut through, with no wash from open tom mics between fills.

### toms.clean
- **Why:** tom mics stay open all song and add cymbal and snare spill. That washes out the kit and costs tightness.
- **Start:** keep each tom only around its own hits, with the full natural decay and a short fade. Use a gate or expander if hit times aren't marked.
- **Tool:** pedalboard `NoiseGate`, Airwindows `Gatelope`.
- **Needed if:** off-hit energy in a tom mic is within 25 dB of its hits.
- **Worked if:** off-hit energy falls, every tom hit is kept with its decay, and no cut-offs show at the closings.

### toms.hpf
- **Why:** removes kick spill and rumble below each tom's note.
- **Start:** high-pass 50-90 Hz, higher for smaller toms; about 40 Hz for a floor tom that should shake.
- **Tool:** `HighpassFilter`.
- **Needed if:** a tom holds energy below its fundamental on kick hits.
- **Worked if:** that energy falls and the tom's fundamental holds.

### toms.eq
- **Why:** toms get boxy (200-400 Hz) and flat (600-800 Hz), and lose stick attack.
- **Start:** wide cut 200-400 Hz; cut 600-800 Hz, sometimes hard; boost 2-4 kHz for attack and 6-8 kHz for air, judged with the cymbals playing; tame 100-200 Hz resonances, then a low shelf near 100 Hz for weight.
- **Tool:** `PeakFilter`, shelves, `ZL Equalizer 2`.
- **Needed if:** a band rises more than 6 dB over its neighbours, or attack is low on tom hits.
- **Worked if:** the peak falls and attack rises in the mix during fills.

### toms.compress
- **Why:** evens hit levels in fills and adds density.
- **Start:** per tom: slow attack, moderately fast release, about 4:1, 3-6 dB. Then a fast FET compressor on the tom bus. Some engineers skip tom compression and saturate and clip instead (toms.saturate-clip).
- **Tool:** `ZL Compressor`, `TDR Kotelnikov`; `WetCompressor` on the bus.
- **Needed if:** tom hit levels spread more than 3 dB within fills.
- **Worked if:** spread falls and attack holds.

### toms.multiband
- **Why:** calms booming low mids on some hits without thinning the others.
- **Start:** one band around 80-220 Hz, a few dB on the loudest hits only.
- **Tool:** `ZaMultiCompX2`, `TDR Nova`.
- **Needed if:** 80-220 Hz energy varies more than 4 dB between tom hits of equal level.
- **Worked if:** that spread falls and the toms' weight holds.

### toms.saturate-clip
- **Why:** harmonics and clipping make toms cut through guitars and control their peaks.
- **Start:** light saturation, then a clipper on the loudest hits.
- **Tool:** `T-Saturator`, `CHOWTapeModel`, Airwindows `ADClip8`.
- **Needed if:** always tried; drop it if it costs attack.
- **Worked if:** at equal loudness, 1-5 kHz energy in fills rises or limiter dip falls.

### toms.transient
- **Why:** gives weak hits or poor recordings their attack back.
- **Start:** raise `point` a little on the tom bus.
- **Tool:** Airwindows `Point`.
- **Needed if:** tom attack is still low after EQ and compression.
- **Worked if:** attack rises in fills and the valleys between hits hold.

### toms.limit
- **Why:** catches what the compressors miss, so fills don't spike the master limiter.
- **Start:** brickwall on the tom bus, a few dB on the loudest hits.
- **Tool:** `ZL Limiter`, `Limiter`.
- **Needed if:** some tom hits sit more than 3 dB over the median hit.
- **Worked if:** those hits come down and the median hit's attack holds.

### toms.reverb
- **Why:** toms that share the snare's space make fills sound like one kit in one room.
- **Start:** send the toms to the drum reverb, lower than the snare.
- **Tool:** a send to the drum reverb bus.
- **Needed if:** the kit has a reverb and the toms don't feed it.
- **Worked if:** tail after tom hits rises and tom attack holds.
