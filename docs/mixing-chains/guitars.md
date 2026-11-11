# Guitars

Goal: wide, heavy, tight rhythm guitars that leave room for the kick, bass, snare crack and leads; leads that sit on top.

### guitars.align
- **Why:** two mics or amps that capture the same take comb-filter when they are out of time or polarity. Separate takes (double tracks) are never aligned to each other.
- **Start:** for tracks that share one DI, measure each one's offset against the DI, then align them to each other and check polarity. The DI is never in the mix.
- **Tool:** stem `offset_samples`, `invert`.
- **Needed if:** two or more audible tracks capture the same take (they share a DI). A single amp track with its DI is dropped with its measured offset.
- **Worked if:** at fixed levels, their sum gain in 100 Hz-2 kHz is the highest of the offsets and polarities tried.

### guitars.pair
- **Why:** double-tracked rhythms panned hard left and right make the width. Unequal levels pull the image to one side.
- **Start:** main pair hard left and right at equal loudness. Extra layers lower, or panned nearer the centre.
- **Tool:** stem `gain_db`, `pan`.
- **Needed if:** the song has more than one rhythm guitar track.
- **Worked if:** left and right guitar loudness differ by under 1 dB, and the mix's width is in the profile range.

### guitars.side-tones
- **Why:** slightly different tones left and right sound wider than the same tone twice.
- **Start:** a different EQ on each side: for example, cut some mids on one side and boost them on the other.
- **Tool:** `PeakFilter` per stem.
- **Needed if:** the left and right guitars' spectra match within 1 dB in every octave.
- **Worked if:** the mix's side energy in 500 Hz-4 kHz rises, mono loudness holds, and left and right loudness still match within 1 dB.

### guitars.filters
- **Why:** guitar lows fight the kick and bass; highs above 10 kHz are fizz.
- **Start:** a 24 dB/oct high-pass from about 60 Hz, raised (up to 140 Hz) only while 80-120 Hz on palm mutes holds within 1 dB; gentle low-pass at 6-12 kHz. A hard low cut can kill the "chug"; some engineers use a shelf instead.
- **Tool:** `ZL Equalizer 2` for the high-pass; `LowpassFilter` for the gentle low-pass.
- **Needed if:** guitars overlap the kick and bass below 120 Hz, or hold fizz above 10 kHz.
- **Worked if:** that overlap and fizz fall, and 80-120 Hz on palm mutes holds within 1 dB.

### guitars.resonance
- **Why:** cab and room resonances (100-250 Hz) and fizz nodes (above 4 kHz) make guitars boomy or harsh. A note's fundamental is not a resonance.
- **Start:** narrow cuts on peaks found by a tight sweep, keeping a little 70-90 Hz thump.
- **Tool:** `PeakFilter`, `ZL Equalizer 2`.
- **Needed if:** a narrow peak rises more than 6 dB over its neighbours and stays across different notes.
- **Worked if:** the peak falls and the guitars' level holds.

### guitars.mud-cut
- **Why:** broad build-ups make guitars woolly (200-500 Hz) or papery (600-800 Hz).
- **Start:** a wide cut at 200-500 Hz; a cut at 600-800 Hz (as low as 500 Hz) if cardboard. Cut more than you'd boost: distorted guitars rarely need boosts.
- **Tool:** `PeakFilter`.
- **Needed if:** the mix's 250 Hz or 500 Hz band is over the profile range, or guitars and bass overlap in 150-500 Hz.
- **Worked if:** that band moves into range and the guitars' 1-4 kHz level holds.

### guitars.multiband
- **Why:** palm mutes bloom in the lows. A multiband on 70-250 Hz holds them without dulling open chords.
- **Start:** one band about 80-260 Hz, fast-to-medium attack, quick release, up to 8 dB.
- **Tool:** `ZaMultiCompX2`, `TDR Nova`.
- **Needed if:** 80-260 Hz loudness spreads more than 4 dB between palm mutes and open chords.
- **Worked if:** that spread falls and the chug holds.

### guitars.mirror-eq
- **Why:** guitars and bass share the mids. Boosting one where the other is cut lets both be heard.
- **Start:** where the bass is cut, boost the guitars near it; where the bass is boosted, cut the guitars. One mix cut the bass at 1.4 kHz and boosted the guitars at 1.1 kHz.
- **Tool:** `PeakFilter`.
- **Needed if:** bass.growl is kept, or bass and guitars overlap in 700 Hz-3 kHz.
- **Worked if:** overlap falls and both keep their level in the mix.

### guitars.lead-room
- **Why:** 2-4 kHz is where leads, snare crack and kick click compete with the rhythms.
- **Start:** cut 2-4 kHz on the rhythms a few dB, or duck them there while a lead plays.
- **Tool:** `PeakFilter`; or `Duck` on the rhythm bus keyed from the lead, `band_hz` [2000, 4000].
- **Needed if:** kick or snare 2-5 kHz energy in the first 25 ms of hits, or a lead's 2-4 kHz, is below the rhythms' in the same moments.
- **Worked if:** that gap closes and the rhythms' 2-4 kHz level away from hits and leads holds.

### guitars.compress
- **Why:** distorted guitars are already compressed. Broad compression makes them fizzier and harsher; it rarely helps.
- **Start:** none by default. If tried: unlinked left and right (linked narrows the image), a few dB.
- **Tool:** `ZL Compressor`.
- **Needed if:** guitar level spreads more than 3 dB within a section after rides.
- **Worked if:** spread falls with no rise in 4-8 kHz.

### guitars.saturate
- **Why:** tape or console saturation smooths, fattens and controls distorted guitars.
- **Start:** 15 ips tape or console, driven fairly hot.
- **Tool:** `CHOWTapeModel`, Airwindows `ToTape6`, `Console7Channel`.
- **Needed if:** always tried; drop it if it adds harshness.
- **Worked if:** at equal loudness, 4-8 kHz doesn't rise and the guitars' peaks fall.

### guitars.rides
- **Why:** guitars that suit a verse can bury a chorus or a lead.
- **Start:** ride the guitar bus by section.
- **Tool:** `Ride` on the guitar bus.
- **Needed if:** the guitars-to-rest loudness ratio changes more than 2 dB between sections the recipe marks as equal (same `group`).
- **Worked if:** the ratio stays within 2 dB across those sections.

### guitars.lead
- **Why:** a lead must sit above the rhythms and have its own space.
- **Start:** high-pass up to 160 Hz, a little more mids, smooth compression. A tempo delay on a send with its return filtered dark, and a short reverb with pre-delay.
- **Tool:** filters, `MJUCjr`; `Delay` (`mix` 1) and `DragonflyHallReverb` (`dry_level` 0) on buses fed by sends.
- **Needed if:** the song has a lead guitar.
- **Worked if:** the lead's 1-4 kHz energy sits above the rhythms' while it plays, and its delay return adds tail energy in the gaps between phrases.
