# Kick

Goal: a tight low-end thump and a thin, clear click that both survive the master, even in fast double-kick runs.

### kick.blend
- **Why:** each kick mic or sample holds a different part: the inside mic gives click, the outside mic body, the sub weight, the sample consistency. Their balance is most of the kick sound, and mics out of phase thin it.
- **Start:** set each mic's level on its own. Check polarity and timing against the main kick mic and against the overheads. Sum to one kick bus and process the bus.
- **Tool:** stem `gain_db`, `invert`, `offset_samples`.
- **Needed if:** the kick has more than one mic or sample.
- **Worked if:** at fixed levels, sum gain in 40-150 Hz and 2-6 kHz is the highest of the polarity and timing options tried, and attack rises at equal kick-bus loudness, in fast and sparse sections.

### kick.gate
- **Why:** ringing and spill from other drums between kick hits blur the low end.
- **Start:** gate or expand the kick bus. Threshold just under the softest real hit, fast attack, release shorter than the gap in the fastest double-kick run. Some engineers skip gating and keep the bleed; measure.
- **Tool:** pedalboard `NoiseGate`; Airwindows `Gatelope`.
- **Needed if:** off-hit bleed in the kick mics is within 25 dB of the hits, or the kick rings past 150 ms.
- **Worked if:** off-hit bleed falls and every original hit keeps its attack: no lost soft hits.

### kick.hpf
- **Why:** sub energy below the thump is mud and wasted headroom in fast music.
- **Start:** a steep high-pass at about 36 Hz.
- **Tool:** `ZL Equalizer 2` (steep slopes); pedalboard `HighpassFilter` is only 6 dB/oct.
- **Needed if:** the kick bus holds energy below 40 Hz within 6 dB of its 50-70 Hz.
- **Worked if:** energy below 40 Hz falls, 50-70 Hz holds, and the trial limiter dips less on kick hits at equal loudness.

### kick.mud-cut
- **Why:** 200-400 Hz is boxy. It hides the click and fights the guitars.
- **Start:** a wide, deep cut somewhere in 200-400 Hz.
- **Tool:** `PeakFilter`.
- **Needed if:** the kick's 200-400 Hz sits less than 6 dB under its 50-70 Hz, or holds a narrow peak.
- **Worked if:** that energy falls and the mix's 250 Hz band moves toward the profile range.

### kick.bass-room
- **Why:** the kick and bass fight for 60-200 Hz. Deciding which owns which band makes both clearer.
- **Start:** the kick owns 50-70 Hz. Cut the kick a few dB in 100-200 Hz where the bass lives, or at a measured build-up (one metal mix cut 3 dB at 130 Hz).
- **Tool:** `PeakFilter`.
- **Needed if:** the kick and bass overlap in 60-200 Hz on kick hits.
- **Worked if:** overlap falls and both kick attack and bass level hold in the mix.

### kick.thump
- **Why:** sets the weight felt on small and large speakers. Lower thump gives more depth; higher gives more presence on small speakers but fights the bass more.
- **Start:** low shelf, or a bell for a peakier sound, centred at 50-80 Hz.
- **Tool:** `LowShelfFilter` or `PeakFilter`.
- **Needed if:** the kick's 50-80 Hz sits below its 100-200 Hz in the blend.
- **Worked if:** the mix's 63 Hz band moves toward the profile range without more limiter dip.

### kick.click
- **Why:** the click makes each hit audible in fast runs under dense guitars.
- **Start:** boost at 6-10 kHz for slap, or 2-4 kHz if the attack is still buried. If it needs a lot, the guitars or cymbals are too bright instead; prefer shaping the attack with compression over large boosts.
- **Tool:** `PeakFilter` or `HighShelfFilter`.
- **Needed if:** in the first 25 ms of kick hits, the kick's 2-6 kHz energy is below the guitars' 2-6 kHz energy.
- **Worked if:** that gap closes in the full mix at equal loudness, and cymbal and guitar levels hold.

### kick.compress
- **Why:** shapes punch. A slow attack lets the hit through, then the body is clamped.
- **Start:** VCA-style, slow attack, moderately fast release, 4:1 to 8:1. A slower release ducks fast double-kick runs on purpose; use it only if runs pile up.
- **Tool:** `TDR Kotelnikov`, `ZL Compressor`, `TDR Molotok`.
- **Needed if:** attack is low after EQ, or hit levels spread more than 3 dB in a steady section.
- **Worked if:** attack rises or hit spread falls, and gain recovers before the next hit in the fastest run (unless ducking the run was the aim).

### kick.saturate
- **Why:** harmonics make the kick read on small speakers, and rounded peaks save headroom.
- **Start:** tape or console, light drive.
- **Tool:** `CHOWTapeModel`, Airwindows `Console7Channel`, `T-Saturator`.
- **Needed if:** always tried; drop it if it costs attack.
- **Worked if:** at equal loudness, 100 Hz-1 kHz harmonics rise or limiter dip on kick hits falls, with no attack loss.

### kick.transient
- **Why:** adds attack when compression can't, without raising the bleed.
- **Start:** raise `point` a little; `reaction_speed` sets how short a transient it catches. A last resort after blend and compression.
- **Tool:** Airwindows `Point`.
- **Needed if:** attack is still low after blend, EQ and compression.
- **Worked if:** attack rises in the full mix and the valleys between hits hold.

### kick.limit
- **Why:** catches the odd hit that jumps out, so the master limiter doesn't react to it.
- **Start:** brickwall, a few dB on the loudest hits only.
- **Tool:** `ZL Limiter`, `Limiter`.
- **Needed if:** some hits sit more than 3 dB over the median hit.
- **Worked if:** those hits come down and the median hit's attack holds.

### kick.duck-bass
- **Why:** dips the bass's low end only while the kick hits, so each owns the low end in turn without a static cut.
- **Start:** key a band below about 120 Hz on the bass from the kick, a few dB, fast release.
- **Tool:** `Duck` on the bass, keyed from the kick bus, `band_hz` [0, 120].
- **Needed if:** after kick.bass-room, kick and bass still overlap below 120 Hz on kick hits.
- **Worked if:** overlap on kick hits falls, bass level between hits recovers, and it beats a static cut of the same depth.

### kick.rides
- **Why:** double-kick runs pile up low end and attack. A tone set on a normal section is too much there.
- **Start:** set the base tone on a normal section. Automate a low shelf, a high shelf or the level down during dense runs.
- **Tool:** `Ride` on the kick bus over the dense sections, with `band_hz` for a shelf or without it for level.
- **Needed if:** short-term low-band loudness in double-kick sections (the highest kick hit rate) is more than 2 dB above normal sections.
- **Worked if:** low-band loudness evens out between sections and kick attack holds.
