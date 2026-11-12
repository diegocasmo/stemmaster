# Mixing chains

The standard chain for each source in heavy instrumental music, with the reason for every move and how to measure it. This is the factory's only mixing reference. Settings are starting points; the song's measurements decide the final values.

Each move is a heading `### <chain>.<move>`, where `<chain>` is the file name. Its id never changes, so recipes and checks can name it. Under it: **Why**, **Start**, **Tool**, **Needed if**, **Worked if**. Where engineers disagree, the move says what the other approach is.

| File | Covers |
|---|---|
| [kick.md](kick.md) | Kick mics and samples |
| [snare.md](snare.md) | Snare top, bottom and samples |
| [toms.md](toms.md) | Toms |
| [overheads.md](overheads.md) | Overheads, hi-hat, cymbal spots, room mics |
| [drum-bus.md](drum-bus.md) | Drum groups, parallel paths, drum reverb |
| [bass.md](bass.md) | Bass |
| [guitars.md](guitars.md) | Rhythm and lead guitars |
| [synths.md](synths.md) | Synths, pads, piano |
| [mix-bus.md](mix-bus.md) | Whole mix while mixing |
| [master.md](master.md) | The master |

## Order

1. Gain staging, then polarity, timing and blend of every multi-mic source. The blend decides most of a drum's sound, so set it before any EQ.
2. Drums first, overheads before shells. Then bass with the drums playing, then rhythm guitars, then leads, synths and keys.
3. Within a source, remove problems before adding colour. By default: filters and cuts, then dynamics, then saturation, then space. A source may change this order when its chain says so (a bass can be compressed before EQ).
4. Mix through the mix-bus chain and a trial limiter from the start, so every move is judged at release loudness.
5. Corrective rides (notes and sections that jump or drop) come before heavy compression, so the compressor does less.

## How to try a move

Each move has **Needed if** (a measured fault that justifies a trial) and **Worked if** (the measured change that keeps it).

- Render the step on and off over the same sections. Compensate latency and match loudness before comparing.
- Find hits and notes once, on the input, and measure every one of them after processing. Finding them again on the output hides the soft hits a gate or compressor lost.
- Measure the source, its bus, and the full mix through the trial master. A move that helps the source and hurts the mix is dropped.
- Test a fast section and a sparse section separately. Fast double-kick and blast passages fail in ways a sparse passage hides.
- A move applies to each source it names: a song with five toms decides `toms.clean` for each tom.
- Full-mix numbers can be compared with the profile's references. Source settings (gate threshold, wet/dry, mic offset) cannot: the references have no stems.
- The limits in **Needed if** are starting proposals. Change one only with measurements from a song, and note why.

No published metric measures punch, tightness or depth on a finished master. A master can sit inside the references' range on every such proxy and still sound amateur; the first song 000 master did. So a move is proven on its own source and bus first, and the full-mix comparison only backs it up.

## Measures

- **Attack**: energy in the first 25 ms after a hit over energy 30-100 ms after it, in dB.
- **Tail**: energy 80-200 ms after a hit over its first 30 ms, in dB.
- **Valley**: the lowest 40-150 Hz energy between two kicks, over the kick's peak, in dB. Lower means tighter. The energy is the analytic envelope of a zero-phase band-pass, averaged over 10 ms; the peak is the highest point in the first 50 ms after the kick, the valley the lowest from there to 5 ms before the next kick. Gaps under 60 ms are skipped and counted.
- **Sum gain**: energy of the summed mics over the sum of each mic's energy, per band, across hits. Compare polarity and timing options at the same mic levels: higher is better, and below 0 dB the mics cancel. It is read on the mono sum, where timing and polarity errors between panned mics are heard.
- **Off-hit bleed**: a mic's mean power between its own hits over its mean power in the first 100 ms of them, per band. Mean power, so the count of hits doesn't change it.
- **Limiter dip**: the trial limiter's gain drop in the 60 ms after a kick or snare hit, against the 100 ms before it: the median of 5 ms gains before, minus the lowest after. Measured across the last master step only (`--tap master-in` against `trial.wav`), so EQ or clipping before it doesn't count as limiting. Compare two mixes with their trial masters at equal loudness.
- **Spread**: p90 minus p10 of hit, note or short-term levels in one passage.
- **Overlap**: in each band and 50 ms frame where both sources are within 6 dB of each other, the weaker source's energy; summed, over the band's total energy of both sources in every frame, in dB. "On kick hits" counts only the samples in the first 25 ms of kick hits, in the 6 dB test and in both sums.
- **Section loudness**: short-term (3 s) loudness of the windows wholly inside a section, as p10, p50 and p90; its error is the p50 minus the mix's integrated loudness minus the section's `loudness_lu`.
- **Profile range**: min to max across the profile's references, measured the same way on the mix. With no profile, record these checks as skipped.
- **Holds**: falls by at most 0.5 dB (0.5 LU for loudness), when a move gives no number. **Rises** and **falls** likewise mean by more than 0.5 dB; **toward** a range means landing in it or moving at least 0.5 dB closer.
- **Overlap present**: overlap above -15 dB, when a move gives no number.
- **Damaged hit**: a hit whose level or attack falls more than 1 dB.
- **Recovery**: the gain a step still takes 25-5 ms before each next hit in the fast passage, against the gain it gives when idle. Recovered is under 1 dB.

`analyze` measures whole-file loudness, peaks, octave bands and width; its `hits.py` measures hit level, spread, attack, tail and off-hit bleed, and its `measure.py` valley, overlap, limiter dip, section loudness and profile range; `notes.py` finds notes; `align` measures sum gain. `check` holds each move's evidence contract, computes its default outcome from these measures, and fails decisions that contradict it.

## Done rule

- **Mix:** every move in every chain except `master` that applies to the song's stems is rendered and recorded in the recipe, by id, as kept, changed or dropped, with measurements. A move whose **Needed if** finds no fault is dropped with that check's numbers. A move that is undecided blocks the mix.
- **Master:** starts only after the mix passes review. The same rule then applies to every `master` move.

## Limits

- Vocals are out of scope: vocal stems are not listed in the recipe. DI tracks (guitar or bass) are never used: not mixed in, re-amped, or used as a timing reference. Sound effects follow synths.md by role.
- No new audio: no sample triggering beyond sample stems that already exist.
- pedalboard's `HighpassFilter` and `LowpassFilter` are 6 dB/oct. For steeper slopes use `ZL Equalizer 2`.

## Where this comes from

Interviews and walkthroughs with metal mix engineers and mixing research, checked by adversarial review. Each move states its own reason; these links are only for auditing that a move wasn't invented.

- Nolly Getgood: [Audiofanzine](https://en.audiofanzine.com/sound-technique/editorial/articles/tracking-and-mixing-with-nolly.html); [*Heaviness in Metal Music Production II*](https://pure.hud.ac.uk/en/publications/heaviness-in-metal-music-production-volume-ii-learn-from-the-mast/) (CC BY 4.0; also Buster Odeholm, Mike Exeter, Andrew Scheps).
- [SOS, "The Masters' Art Of Mixing"](https://soundonsound.com/techniques/masters-art-mixing) (Odeholm, Nolly, Bogren, Otero, Scheps, Exeter).
- Andy Sneap: [SOS](https://www.soundonsound.com/techniques/inside-track-judas-priest). Joey Sturgis: [URM drums](https://urm.academy/tt4-drum-mixing-tips-tricks/), [URM mastering](https://urm.academy/tt3-joey-sturgis-mastering-tips-tricks/). Jens Bogren: [URM 29](https://urm.academy/ep29-jens-bogren/), [URM 195](https://urm.academy/ep195-jens-bogren/). Ermin Hamidovic and Nolly: [URM 151](https://urm.academy/ep151-nolly-getgood-ermin-hamidovic).
- Mark Mynett: [SOS 2009](https://www.soundonsound.com/techniques/mixing-metal), [SOS part 3](https://www.soundonsound.com/techniques/making-modern-metal-part-3). Mike Senior: [phase](https://www.soundonsound.com/techniques/mix-rescue-phase-relationships), [metal](https://www.soundonsound.com/techniques/mix-rescue-mixing-metal), [No Logo](https://www.soundonsound.com/techniques/mix-rescue-no-logo), [bass](https://www.soundonsound.com/techniques/mixing-bass), [guitars](https://www.soundonsound.com/sound-advice/q-how-do-make-guitars-sound-more-epic).
- Bass: Dave Lockwood ([SOS](https://www.soundonsound.com/techniques/mixing-di-bass-guitar)), Paul White ([SOS](https://www.soundonsound.com/techniques/bass-guitars-mix)).
- Others: Dan Lancaster ([SOS](https://www.soundonsound.com/techniques/inside-track-bring-me-horizon)), Ghost's mixers ([SOS](https://www.soundonsound.com/techniques/inside-track-producing-mixing-ghost)), Tom Lord-Alge ([SOS](https://www.soundonsound.com/techniques/inside-track-tom-lord-alge)), Chris Lord-Alge ([SOS](https://www.soundonsound.com/techniques/secrets-mix-engineers-chris-lord-alge)), Taylor Larson ([URM](https://urm.academy/ep26-taylor-larson/)), Will Putney ([Mix](https://www.mixonline.com/recording/getting-heavy-with-will-putney)), Ian Shepherd ([iZotope](https://izotope.com/en/learn/how-to-master-metal.html)).
- Research: [Fenton et al. 2015, punch](https://eprints.hud.ac.uk/id/eprint/26288/); [De Man et al. 2017, reverb preference](https://aes.org/publications/elibrary/elibrary-page/?id=18548).
