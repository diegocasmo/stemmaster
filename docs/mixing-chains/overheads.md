# Overheads, hi-hat, cymbals and room

Goal: clear, wide cymbals that never get harsh, and a room that adds size without mud. In metal, overheads are mostly cymbal mics; the close mics carry the drums.

### overheads.hpf
- **Why:** the low end in overheads is drum spill that blurs the close mics and fights the kick.
- **Start:** sweep a high-pass up until the drums' body is gone (often 300-550 Hz). For a whole-kit sound, 90-250 Hz with a wide cut at 300-700 Hz. Some engineers use a low shelf instead of a filter.
- **Tool:** `HighpassFilter`, `LowShelfFilter`.
- **Needed if:** overhead energy below 250 Hz is within 10 dB of their 2-8 kHz energy.
- **Worked if:** low energy falls, cymbal energy holds, and kick and snare sum gain with the overheads holds or rises.

### overheads.snare-control
- **Why:** snare hits in the overheads arrive late and sound different, smearing the snare.
- **Start:** a brickwall limiter on the snare peaks before any compression, or overheads ducked from the snare.
- **Tool:** `ZL Limiter`; or `Duck` on the overheads keyed from the snare.
- **Needed if:** snare hits are more than 6 dB above the cymbal level in the overheads.
- **Worked if:** snare peaks in the overheads fall and the cymbal level holds.

### overheads.harsh-cut
- **Why:** cymbals pile up at 3-4 kHz, where guitars and leads live, and ring at narrow frequencies.
- **Start:** wide cut at 3-4 kHz; narrow notches on ringing cymbal notes; a low-pass if harsh and thin.
- **Tool:** `PeakFilter`, `LowpassFilter`, `TDR Nova` (dynamic).
- **Needed if:** the mix's 4 kHz band is over the profile range, or a narrow cymbal peak rises more than 6 dB over its neighbours.
- **Worked if:** the band or peak falls into range and 8-12 kHz holds.

### overheads.air
- **Why:** a little air makes cymbals open without harshness.
- **Start:** a gentle, small high shelf at 8-10 kHz.
- **Tool:** `HighShelfFilter`.
- **Needed if:** the mix's 8 kHz band is under the profile range.
- **Worked if:** 8 kHz moves into range and 4 kHz doesn't rise.

### overheads.compress
- **Why:** evens the cymbal level. Heavy compression pumps and tears.
- **Start:** slight: slow attack, 2:1, 1-3 dB. Ride the level instead if it is uneven across sections.
- **Tool:** `ZL Compressor`, `TDR Kotelnikov`.
- **Needed if:** cymbal level spreads more than 3 dB within a steady section.
- **Worked if:** spread falls with no gain swing on snare hits.

### overheads.hihat-blend
- **Why:** a hi-hat or cymbal spot mic adds detail, but too loud it harms the snare tone and the stereo picture.
- **Start:** blend low under the overheads, panned where the hat sits in them. High-pass 300-500 Hz; low-pass near 12 kHz if it only adds weight.
- **Tool:** stem `gain_db`, `pan`; `HighpassFilter`, `LowpassFilter`.
- **Needed if:** a hi-hat or cymbal spot mic exists.
- **Worked if:** the hat's position in the overheads and in its own mic match, and snare sum gain holds.

### overheads.room-blend
- **Why:** room mics give the kit size and glue, but add mud and cymbal wash in fast music. Some engineers barely use them in metal; others build the drum sound on them.
- **Start:** low-pass for cymbal spill; cut 200-400 Hz and near 700 Hz; a low shelf near 100 Hz only if the valleys between kicks hold. Then heavy compression with a fast release (or about 8:1 for a cleaner room), and re-check the low-pass. Blend low; raise it in sparse sections.
- **Tool:** `LowpassFilter`, `PeakFilter`, `LowShelfFilter`; `TDR Molotok` or `WetCompressor` (mode FAST).
- **Needed if:** a room mic exists.
- **Worked if:** tail energy after drum hits rises and the low-band valleys between kicks hold, in fast and sparse sections.

### overheads.room-reverb
- **Why:** with no room mic, a dry kit sounds small and close. A short room reverb replaces it.
- **Start:** send the shells, and optionally a little of the overheads, to a short room: early reflections up, decay under 1 s, high-pass the return at 150-200 Hz.
- **Tool:** `DragonflyRoomReverb`, `DragonflyEarlyReflections` with `dry_level` 0, on a bus fed by sends.
- **Needed if:** the song has no room mic.
- **Worked if:** tail energy after drum hits rises, kick attack and the low-band valleys hold, and the mix's width stays in the profile range.
