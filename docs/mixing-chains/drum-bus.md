# Drum bus

Goal: one kit, not separate mics: shells and cymbals glued, with depth and sustain that don't blur fast playing.

### drum-bus.groups
- **Why:** kick, snare, toms and cymbals each need their own bus, so each can be processed and ridden as one source, and the whole kit needs a bus above them. Kick and bass never share a bus.
- **Start:** buses for kick, snare, toms, cymbals and room, summed into a drums bus.
- **Tool:** stem `bus`; `buses` with parent `drums`.
- **Needed if:** always.
- **Worked if:** every drum stem routes to one group, and every group to the drums bus. Reverb and parallel returns are not groups; a sample that only feeds the reverb counts as routed.

### drum-bus.compress
- **Why:** light bus compression glues the kit and evens it against the guitars.
- **Start:** VCA, slow attack, auto or fast release, 2:1 to 4:1, 1-3 dB.
- **Tool:** `TDR Kotelnikov`, Airwindows `Logical4`.
- **Needed if:** drums-bus level spreads more than 3 dB between hits in a steady section.
- **Worked if:** spread falls and kick and snare attack in the mix hold.

### drum-bus.parallel
- **Why:** a heavily compressed copy under the dry drums adds sustain and density without killing the dry attack. In fast technical metal it often blurs the playing, and some engineers never use it.
- **Start:** send shells only (no cymbals) to a compressor: medium-fast attack, very fast release, high ratio, 10 dB or more. Raise the return until the balance just starts to change, then stop.
- **Tool:** `TDR Molotok` (`drymix`), or `ZL Compressor` on a bus fed by sends.
- **Needed if:** shell body and sustain are weak in sparse sections and more serial compression lowers attack.
- **Worked if:** body and tail rise at equal bus loudness, dry attack holds, and the valley before the next hit holds in the fastest section.

### drum-bus.distort-parallel
- **Why:** a filtered, distorted copy adds length and presence that EQ can't.
- **Start:** send shells to a distortion, filter before and after, blend low.
- **Tool:** `T-Saturator`, `Distortion` on a bus fed by sends.
- **Needed if:** shells are masked by guitars in 1-5 kHz after EQ.
- **Worked if:** shell energy in 1-5 kHz rises in the mix at equal loudness, and cymbal wash doesn't.

### drum-bus.saturate
- **Why:** tape or console saturation softens harsh transients and thickens the kit. Without it, drums can sound pokey and disconnected.
- **Start:** console or tape on the drums bus, light drive.
- **Tool:** Airwindows `Console7Channel`, `BussColors4`; `CHOWTapeModel`.
- **Needed if:** always tried; drop it if it costs attack.
- **Worked if:** at equal loudness, drum peaks cost the trial limiter less and kick and snare attack hold.

### drum-bus.reverb
- **Why:** one shared reverb puts the whole kit in one space and gives depth. Dense mixes hide reverb, so the right amount can sound too wet soloed.
- **Start:** a plate or room fed by the snare (most), toms, and a little overheads and kick. Pre-delay 10-20 ms, decay 0.8-1.4 s, high-pass the return at 150-200 Hz. More in sparse sections, less in dense ones.
- **Tool:** `DragonflyPlateReverb`, `DragonflyRoomReverb`, `DragonflyHallReverb` with `dry_level` 0, on a bus fed by sends.
- **Needed if:** the kit has no reverb send yet.
- **Worked if:** tail energy after hits rises, the low-band valleys hold, and the tail rises more in sparse than in fast sections.

### drum-bus.rides
- **Why:** the drum balance and space that suit a verse are wrong for a blast section. Rides keep the kit right in each section.
- **Start:** automate the drums bus and the reverb level by section.
- **Tool:** `Ride` on the drums bus and on the reverb bus: first on it to ride what feeds the reverb, last to ride the return.
- **Needed if:** the drums-to-rest loudness ratio changes more than 2 dB between sections the recipe marks as equal (same `group`).
- **Worked if:** the ratio stays within 2 dB across those sections.
