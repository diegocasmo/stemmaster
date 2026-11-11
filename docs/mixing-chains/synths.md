# Synths and keys

Goal: synths, pads and piano add colour and width without muddying the low end or masking the guitars.

### synths.role
- **Why:** each part needs the treatment of the role it plays: a sub synth is bass, a lead synth is a lead, a pad is ambience.
- **Start:** label each synth or keys stem as bass, lead or pad, and also apply that role's chain (bass, guitars.lead, or the moves below).
- **Tool:** stem `bus`.
- **Needed if:** the song has synth or keys stems.
- **Worked if:** every synth or keys stem has a role.

### synths.hpf
- **Why:** full-range synths, pads and piano stack up low end that the kick and bass need.
- **Start:** high-pass pads and keys well above the bass, judged in the mix.
- **Tool:** `HighpassFilter`.
- **Needed if:** a pad or keys part holds energy below 150 Hz.
- **Worked if:** its overlap with the bass and kick below 150 Hz falls, and its own main band holds.

### synths.cut-in-context
- **Why:** pads fill the same mids as distorted guitars and wash them out.
- **Start:** wide cuts where the guitars live (200 Hz-4 kHz), deeper than seems right soloed. Cutting only the centre of a stereo pad keeps it wide.
- **Tool:** `PeakFilter`; `ZL Equalizer 2` with the band's `lrmode<N>_lrmode` set to `Mid`.
- **Needed if:** a pad overlaps the guitars in 200 Hz-4 kHz.
- **Worked if:** overlap falls and the pad's level in sparse sections holds.

### synths.colour
- **Why:** saturation, hard compression or tape helps a synth cut through, or takes the edge off a digital one.
- **Start:** light saturation or tape; light compression.
- **Tool:** `CHOWTapeModel`, `T-Saturator`, `ZL Compressor`.
- **Needed if:** a synth is masked in its main band, or is harsh in 4-8 kHz.
- **Worked if:** its main band rises against the guitars, or 4-8 kHz falls, at equal loudness.

### synths.space
- **Why:** delay, chorus and reverb place synths behind or around the band, adding depth.
- **Start:** by role: pads get reverb or chorus, leads get delay.
- **Tool:** `DragonflyHallReverb` (`dry_level` 0), `Delay` (`mix` 1), Airwindows `StereoChorus` on buses fed by sends. pedalboard `Chorus` and `Delay` add no width to a mono source.
- **Needed if:** the synth's tail energy after its notes is lower than the drums' tail energy after hits.
- **Worked if:** the synth's tail energy rises; for a pad, its note attacks also stay under the guitars' level.
