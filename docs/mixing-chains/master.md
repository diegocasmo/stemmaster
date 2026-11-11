# Master

Goal: reach the profile's loudness under -1 dBTP while keeping the kick and snare punch. Starts only after the mix passes review. The master steps replace the `mix-bus.trial-master` steps.

### master.clip
- **Why:** a clipper shaves the shortest peaks, so the limiter has less to do and dips less on drum hits. Some engineers never clip a master; clipping the low end can make the kick fold.
- **Start:** a few dB of clipping before the limiter.
- **Tool:** Airwindows `ADClip8` or pedalboard `Clipping`.
- **Needed if:** the limiter alone dips more than 2 dB on kick or snare hits.
- **Worked if:** at equal loudness, limiter dip on hits falls and kick and snare attack hold, with no rise in 2-8 kHz.

### master.compress
- **Why:** shares the gain with the limiter, so neither works too hard.
- **Start:** slow attack, low ratio, 1-2 dB. These numbers are a starting guess.
- **Tool:** `TDR Kotelnikov`.
- **Needed if:** the limiter does more than 4 dB beyond plain gain.
- **Worked if:** limiter work falls and attack holds.

### master.tone
- **Why:** a final small EQ lands the profile's band balance and width.
- **Start:** shelves or wide bells, 1 dB or less; mid/side EQ to adjust width.
- **Tool:** `ZL Equalizer 2`, `TDR VOS SlickEQ`.
- **Needed if:** an octave band or the width is outside the profile range.
- **Worked if:** it moves into range.

### master.limit
- **Why:** a transparent true-peak limiter at the end brings the master to loudness safely.
- **Start:** ceiling below -1 dBTP, true peak on, oversampled; attack and release short enough to recover between hits.
- **Tool:** `ZL Limiter`.
- **Needed if:** always.
- **Worked if:** the 24-bit export (`render.py --master`) hits the profile loudness and -1 dBTP, kick and snare crest loss stays under 3 dB, and limiter dip on hits is the lowest of the settings tried.
