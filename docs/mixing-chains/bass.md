# Bass

Goal: a solid, even low end that locks with the kick, and a growl in the mids that is heard on small speakers.

### bass.align
- **Why:** a bass recorded on more than one track (amp mics, a processed copy) arrives at different times on each. Out of time, they cancel lows and smear notes.
- **Start:** align each track to the first one (the time reference); check polarity.
- **Tool:** stem `offset_samples`, `invert`.
- **Needed if:** the bass has more than one track.
- **Worked if:** at fixed levels, sum gain in 40-250 Hz is the highest of the offsets and polarities tried, and steady across notes.

### bass.split
- **Why:** a clean path gives solid lows; an amp or distorted copy gives the mid growl. Each does its band best.
- **Start:** low-pass the clean path at 200-300 Hz. High-pass the amp or distorted path at 400-700 Hz; low-pass it near 4 kHz or above. The hole between is intended. Sum to one bass bus. Some engineers distort the low end too, for a dirtier bass.
- **Tool:** `LowpassFilter`, `HighpassFilter` or `ZL Equalizer 2` on the clean path's bus (not its stem, since a send carries the stem's steps) and the amp; a send from the bass to a bus with `Distortion` or `T-Saturator`.
- **Needed if:** the bass has a clean and an amp or distorted track, or its one track lacks mid growl.
- **Worked if:** low-band level spread across notes falls and 700 Hz-3 kHz energy rises at equal bus loudness.

### bass.rides
- **Why:** levelling notes and sections by hand first means less compression later and more life kept. Picking and dead spots make single notes drop out.
- **Start:** ride single notes that jump or vanish, then sections, before any compressor.
- **Tool:** `Ride` on the bass stem, before any compressor.
- **Needed if:** note levels spread more than 3 dB (p90-p10) in a steady passage, or section loudness swings more than 3 dB between sections the recipe marks as equal (same `group`).
- **Worked if:** note spread and section swing both fall under 2 dB.

### bass.hpf
- **Why:** rumble below the lowest note eats headroom.
- **Start:** high-pass below the lowest note you keep: low B is 31 Hz, low E 41 Hz. Higher (40-60 Hz) only if the loss on the lowest notes is measured and small. Some engineers use a low shelf instead.
- **Tool:** `HighpassFilter`, `LowShelfFilter`.
- **Needed if:** energy below the lowest note's fundamental is within 10 dB of it.
- **Worked if:** that energy falls and the lowest notes lose under 1 dB.

### bass.mud-cut
- **Why:** 160-500 Hz is mud and cardboard that clashes with the guitars.
- **Start:** wide cut at 250-500 Hz; narrow cut at 160-200 Hz if muddy; keep 60-80 Hz clear for the kick. If the bass is only muddy in the full mix, the guitars have too much low end instead.
- **Tool:** `PeakFilter`, `ZL Equalizer 2`.
- **Needed if:** bass and guitars overlap in 150-500 Hz, or the mix's 250 Hz band is over the profile range.
- **Worked if:** overlap falls and the bass's note attacks (first 25 ms) in 700 Hz-3 kHz hold.

### bass.growl
- **Why:** mids make the bass audible on small speakers and against the guitars. Engineers split here: some boost near 900 Hz-1 kHz or 2-3.5 kHz, others cut 500-900 Hz (cardboard) and 2-4 kHz (fret clack).
- **Start:** try both: a boost near 1 kHz or 2-3 kHz, and the cuts. Mirror whichever wins on the guitars (guitars.mirror-eq).
- **Tool:** `PeakFilter`.
- **Needed if:** the bass is masked by the guitars in 700 Hz-3 kHz, or harsh fret noise shows at 2-4 kHz.
- **Worked if:** bass energy there rises against the guitars, or fret noise falls, and the guitars' level holds.

### bass.compress
- **Why:** a heavy-music bass must be almost flat in level so the low end is solid. Two stages control it with fewer side effects than one.
- **Start:** a FET first: attack 3 ms or faster, fast release, 4:1 to 8:1, 4-10 dB. Then a gentler second compressor catching a few dB. Some engineers find more than 4-5 dB usually too much; measure the attack.
- **Tool:** `ZL Compressor` or `WetCompressor` (mode FAST), then `MJUCjr` or `TDR Kotelnikov`.
- **Needed if:** note levels spread more than 3 dB (p90-p10) in a steady passage after rides.
- **Worked if:** spread falls under 3 dB and pick attack holds.

### bass.multiband
- **Why:** some notes boom in the lows. A multiband evens them without squashing the mids.
- **Start:** two bands: below 70-100 Hz, and up to 180-250 Hz. Fast attack and release, a few dB each.
- **Tool:** `ZaMultiCompX2`, `TDR Nova`.
- **Needed if:** low-band level spreads more than 3 dB across notes after compression.
- **Worked if:** that spread falls and the lowest notes hold.

### bass.saturate
- **Why:** harmonics let the bass be heard where speakers can't play its fundamental.
- **Start:** saturate or distort the amp or high path, filtered before and after.
- **Tool:** `T-Saturator`, `CHOWTapeModel`.
- **Needed if:** the bass's 700 Hz-3 kHz harmonics are weak after the split.
- **Worked if:** those harmonics rise and the low end's level spread doesn't.

### bass.chorus
- **Why:** a little chorus on the upper bass adds width and presence without moving the lows.
- **Start:** chorus on a copy high-passed well above the lows, blended low.
- **Tool:** Airwindows `StereoChorus` on a bus fed by a send. pedalboard `Chorus` adds no width to a mono bass.
- **Needed if:** always tried; drop it if it costs mono.
- **Worked if:** side energy above 500 Hz rises, side energy below 150 Hz doesn't, and mono loudness holds.

### bass.limit
- **Why:** the last few dB of note peaks cost the master limiter.
- **Start:** brickwall until the meter barely moves. If it needs more than 6 dB, add earlier compression instead.
- **Tool:** `ZL Limiter`, `Limiter`.
- **Needed if:** some notes sit more than 3 dB over the median after compression.
- **Worked if:** those notes come down and attack holds.

The kick-keyed duck on the bass is kick.duck-bass.
