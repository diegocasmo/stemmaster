# Snare

Goal: a crack that cuts through dense guitars, a body with weight, and hits that survive the master limiter.

### snare.blend
- **Why:** the top mic gives body and attack, the bottom mic adds crack and wire sizzle, the sample gives consistency. Their balance and phase set the snare sound before any EQ.
- **Start:** top as the main mic. Flip the bottom mic's polarity if the sum loses body. Bottom and sample under the top. Check against the overheads.
- **Tool:** stem `gain_db`, `invert`, `offset_samples`.
- **Needed if:** the snare has more than one mic or sample.
- **Worked if:** at fixed levels, sum gain in 150-400 Hz is the highest of the polarity and timing options tried, and attack rises at equal snare-bus loudness.

### snare.bleed
- **Why:** hi-hat and cymbal spill in the top mic gets louder with every compressor and EQ boost, and smears the cymbals.
- **Start:** expand only the band above 2-3 kHz between hits, then maybe a few dB of broadband expansion. Or a de-esser near 4.5 kHz; one engineer found software de-essers no match for his hardware one, so measure it.
- **Tool:** `TDR Nova` (dynamic band), Airwindows `DeBess`, `T-De-Esser 2`.
- **Needed if:** off-hit energy above 3 kHz in the top mic is within 20 dB of its hits.
- **Worked if:** off-hit high-band energy falls and every hit, ghost notes included, keeps its attack.

### snare.hpf-lpf
- **Why:** removes kick spill below the snare body and cymbal spill above it.
- **Start:** top: high-pass 70 Hz; low-pass down to about 12 kHz if the bottom mic gives the highs. Bottom: high-pass 90 Hz.
- **Tool:** `HighpassFilter`, `LowpassFilter`.
- **Needed if:** the top mic holds kick energy below 70 Hz on kick hits, or cymbal energy above 12 kHz.
- **Worked if:** that energy falls, 150-250 Hz holds, and sum gain with the bottom mic holds.

### snare.ring-cut
- **Why:** a ringing note or boxy low mids make the snare sound small and tubby.
- **Start:** narrow cuts on the ring (300-900 Hz) for a dry snare; a wide cut at 200-500 Hz if tubby, 500 Hz-1 kHz if flat. A dynamic cut at the ~200 Hz fundamental stops soft hits sounding boxy.
- **Tool:** `PeakFilter`, `TDR Nova`.
- **Needed if:** a narrow peak rises more than 6 dB over its neighbours or decays slower than the rest of the snare, or 200-500 Hz sits within 3 dB of the snare's 150-250 Hz body.
- **Worked if:** the peak, its decay or the build-up falls and the body holds.

### snare.body-crack
- **Why:** sets weight (100-200 Hz), crack (2-4 kHz) and air (6-10 kHz) against the guitars.
- **Start:** a bell at 100-200 Hz for a tight body (a 100 Hz shelf is fatter but muddier); a small boost at 2-4 kHz, which gets hard fast; a shelf at 6-10 kHz if the hats allow.
- **Tool:** `PeakFilter`, `LowShelfFilter`, `HighShelfFilter`.
- **Needed if:** in the first 25 ms of snare hits, the snare's 2-5 kHz energy is below the guitars' 2-5 kHz energy, or its body sits low in the mix.
- **Worked if:** that gap closes in the full mix at equal loudness, and the hats don't rise with it.

### snare.compress
- **Why:** shapes the hit. A slow attack keeps the crack; the release sets how long the body rings.
- **Start:** top: attack 10-30 ms, release 50-100 ms, 4:1 to 8:1. Bottom: fast attack, fast release, about 8:1, heavy, so a sizzle sits under the top. Some engineers compress the snare only lightly.
- **Tool:** `TDR Kotelnikov`, `ZL Compressor`, `TDR Molotok`; `WetCompressor` (mode FAST) for the bottom.
- **Needed if:** attack is low, or hit levels spread more than 3 dB in a steady section.
- **Worked if:** attack rises or spread falls, off-hit bleed doesn't rise, and gain recovers before the next hit.

### snare.saturate
- **Why:** harmonics make the crack read on small speakers and thicken the body.
- **Start:** light tape or tube saturation.
- **Tool:** `CHOWTapeModel`, `T-Saturator`, Airwindows `Console7Channel`.
- **Needed if:** always tried; drop it if it costs attack.
- **Worked if:** at equal loudness, 1-5 kHz energy rises or limiter dip on snare hits falls, without attack loss.

### snare.transient
- **Why:** more crack without raising the bleed a compressor brings up.
- **Start:** raise `point` a little, attack only.
- **Tool:** Airwindows `Point`.
- **Needed if:** attack is still low after blend, EQ and compression.
- **Worked if:** attack rises in the full mix and the hats don't.

### snare.clip
- **Why:** shaving the first milliseconds of peaks means the master limiter does less to the snare. Without it, mastering often kills the snare.
- **Start:** hard clip, sparingly, judged through the trial master. Then a limiter for the few hits still over.
- **Tool:** Airwindows `ADClip8` or pedalboard `Clipping`; then `ZL Limiter`.
- **Needed if:** the trial limiter dips more than 2 dB on snare hits, or snare crest loss through the master is over 3 dB.
- **Worked if:** at equal master loudness, limiter dip on snare hits falls and snare attack in the master rises.

### snare.reverb
- **Why:** a short plate or room gives the snare size and places the kit in a space. A dry snare sounds small and boxed in.
- **Start:** see drum-bus.reverb; the snare feeds it most. A clean snare sample can feed it without being in the dry mix.
- **Tool:** a send to the drum reverb bus.
- **Needed if:** the snare has no room mic or reverb path.
- **Worked if:** tail energy after snare hits rises, while snare attack and the low-band valleys between hits hold.
