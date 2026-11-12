"""The evidence each chain move needs, and its Needed if and Worked if as checks over that evidence.

Each leaf names a measure (outcomes.MEASURES), where it is read (`point`), on which events and window, and
the comparison. `needed` is read on the without signal: the off variant for kept and changed records, the
current recipe for dropped ones. `worked` compares with and without the move. `judged` leaves are review
questions: the docs give no number for them, so they never decide.
"""

HOLDS_DB = 0.5
OVERLAP_PRESENT_DB = -15
ONE_TAKE = 0.25
RANGE = ("range", {"point": "trial"})


def leaf(text, measure, op, value, **args):
    return {"kind": "leaf", "text": text, "measure": measure, "op": op, "value": value, "args": args}


def delta(text, measure, op, value, **args):
    return leaf(text, measure, op, value, delta=True, **args)


def judged(text, measure=None, **args):
    return {"kind": "judged", "text": text, "measure": measure, "args": args}


def fact(text, name, **args):
    return {"kind": "fact", "text": text, "name": name, "args": args}


def any_(*items):
    return {"kind": "any", "items": list(items)}


def all_(*items):
    return {"kind": "all", "items": list(items)}


ALWAYS = fact("always", "always")
TRIED = fact("always tried", "always")


def holds(text, measure, **args):
    return delta(f"{text} holds", measure, ">=", -HOLDS_DB, **args)


def rises(text, measure, **args):
    # A change inside the holds tolerance is no change, so it can't count as a rise either.
    return delta(f"{text} rises", measure, ">", HOLDS_DB, **args)


def falls(text, measure, **args):
    return delta(f"{text} falls", measure, "<", -HOLDS_DB, **args)


def no_rise(text, measure, **args):
    return delta(f"{text} doesn't rise", measure, "<=", HOLDS_DB, **args)


def overlap_present(text, **args):
    return leaf(text, "overlap", ">", OVERLAP_PRESENT_DB, **args)


def in_range(field):
    return leaf(f"mix {field} in the profile range", "range_outside_abs", "==", 0, point="trial", field=field)


def toward(field):
    return any_(in_range(field),
                delta(f"mix {field} moves toward the profile range", "range_outside_abs", "<", -HOLDS_DB, point="trial", field=field))


def blend(bands):
    return all_(*[leaf(f"sum gain {lo}-{hi} Hz is the best of the options tried", "blend_shortfall", "<=", 0.3, band=(lo, hi))
                  for lo, hi in bands])


def attack_holds(**args):
    return holds("attack", "attack_p50", **args)


def hits_kept(point="node", events="own"):
    return all_(
        delta("no original hit loses over 1 dB of level or attack", "damaged_hits", "==", 0, point=point, events=events),
        delta("attack of the softest hits holds", "attack_p10", ">=", -HOLDS_DB, point=point, events=events),
    )


def peaks_tamed(events="own"):
    return {
        "needed": leaf("some hits sit more than 3 dB over the median hit", "level_top", ">", 3, events=events),
        "worked": all_(falls("loudest hits over the median", "level_top", events=events), attack_holds(events=events)),
    }


def compress(extra=()):
    return {
        "needed": any_(
            judged("attack is low", "attack_p50"),
            leaf("hit levels spread more than 3 dB in the fast passage", "spread", ">", 3, window="fast"),
            leaf("hit levels spread more than 3 dB in the sparse passage", "spread", ">", 3, window="sparse"),
        ),
        "worked": all_(
            any_(rises("attack", "attack_p50"), falls("fast spread", "spread", window="fast"),
                 falls("sparse spread", "spread", window="sparse")),
            leaf("gain recovers before the next hit in the fastest run", "recovery", "<=", 1, delta=True, window="fast"),
            *extra,
        ),
    }


def saturate(gain_band, chain):
    return {
        "needed": TRIED,
        "worked": all_(
            any_(rises(f"{gain_band[0]}-{gain_band[1]} Hz at equal loudness", "band", band=gain_band),
                 falls(f"limiter dip on {chain} hits", "dip", point="trial", events=chain)),
            attack_holds(),
        ),
    }


def send_tail(events="own"):
    return rises("tail after the source's hits, dry plus return", "contribution_tail", point="join", events=events)


TOMS_FILLS = {"events": "own"}

CONTRACTS = {
    "kick.blend": {
        "needed": leaf("the kick has more than one mic or sample", "stem_count", ">", 1),
        "worked": all_(blend([(40, 150), (2000, 6000)]),
                       rises("attack in the fast passage", "attack_p50", window="fast"),
                       rises("attack in the sparse passage", "attack_p50", window="sparse")),
        "compare": "options",
    },
    "kick.gate": {
        "needed": any_(leaf("off-hit bleed in a kick mic is within 25 dB of the hits", "bleed", ">=", -25, point="stems"),
                       leaf("the kick rings past 150 ms", "decay_ms", ">", 150, window="sparse")),
        "worked": all_(falls("off-hit bleed", "bleed"), hits_kept()),
    },
    "kick.hpf": {
        "needed": leaf("energy below 40 Hz within 6 dB of 50-70 Hz", "band_diff", ">=", -6, band=(0, 40), other_band=(50, 70)),
        "worked": all_(falls("energy below 40 Hz", "band", band=(0, 40)), holds("50-70 Hz", "band", band=(50, 70)),
                       falls("limiter dip on kick hits", "dip", point="trial", events="kick")),
    },
    "kick.mud-cut": {
        "needed": any_(leaf("200-400 Hz less than 6 dB under 50-70 Hz", "band_diff", ">", -6, band=(200, 400), other_band=(50, 70)),
                       judged("200-400 Hz holds a narrow peak", "narrow_peak", band=(200, 400))),
        "worked": all_(falls("200-400 Hz", "band", band=(200, 400)), toward("bands_db.250")),
    },
    "kick.bass-room": {
        "needed": overlap_present("kick and bass overlap in 60-200 Hz on kick hits", other="chain:bass", band=(60, 200), events="kick"),
        "worked": all_(falls("kick-bass overlap in 60-200 Hz on kick hits", "overlap", other="chain:bass", band=(60, 200), events="kick"),
                       attack_holds(point="mix", events="kick"),
                       holds("bass level in the mix", "loud_rel", point="chain:bass")),
    },
    "kick.thump": {
        "needed": leaf("50-80 Hz sits below 100-200 Hz", "band_diff", "<", 0, band=(50, 80), other_band=(100, 200)),
        "worked": all_(toward("bands_db.63"), no_rise("limiter dip on kick hits", "dip", point="trial", events="kick")),
    },
    "kick.click": {
        "needed": leaf("kick 2-6 kHz under the guitars' in the first 25 ms of kick hits", "onset_gap", "<", 0,
                       other="chain:guitars", band=(2000, 6000), events="kick"),
        "worked": all_(rises("kick over guitars 2-6 kHz on kick hits", "onset_gap", other="chain:guitars", band=(2000, 6000), events="kick"),
                       holds("cymbal level", "loud_rel", point="chain:overheads"),
                       holds("guitar level", "loud_rel", point="chain:guitars")),
    },
    "kick.compress": compress(),
    "kick.saturate": saturate((100, 1000), "kick"),
    "kick.transient": {
        "needed": judged("attack is still low after blend, EQ and compression", "attack_p50"),
        "worked": all_(rises("attack in the full mix", "attack_p50", point="mix", events="kick"),
                       no_rise("low-band valley between kicks", "valley", point="mix", events="kick")),
    },
    "kick.limit": peaks_tamed(),
    "kick.duck-bass": {
        "duck": {"key": ("kick", None), "target": ("bass", None)},
        "needed": overlap_present("kick and bass overlap below 120 Hz on kick hits", other="chain:bass", band=(0, 120), events="kick"),
        "worked": all_(falls("kick-bass overlap below 120 Hz on kick hits", "overlap", other="chain:bass", band=(0, 120), events="kick"),
                       holds("bass level between kick hits", "off_hit_level", point="chain:bass", events="kick"),
                       leaf("overlap on kick hits no higher than a static cut of the same depth", "alt_overlap", "<=", 0,
                            other="chain:bass", band=(0, 120), events="kick"),
                       leaf("bass level between hits above the static cut's", "alt_off_hit", ">", 0, point="chain:bass", events="kick")),
        "compare": "alt",
    },
    "kick.rides": {
        "needed": leaf("low-band loudness in the fast passage over 2 dB above the sparse one", "fast_minus_sparse", ">", 2, band=(40, 150)),
        "worked": all_(leaf("low-band loudness evens out", "fast_minus_sparse", "<=", 2, band=(40, 150)), attack_holds()),
    },
    "snare.blend": {
        "needed": leaf("the snare has more than one mic or sample", "stem_count", ">", 1),
        "worked": all_(blend([(150, 400)]), rises("attack", "attack_p50")),
        "compare": "options",
    },
    "snare.bleed": {
        "needed": all_(fact("the snare has a top mic", "has_roles", roles=("top",)),
                       leaf("top mic off-hit energy above 3 kHz within 20 dB of its hits", "bleed", ">=", -20, point="stem:top", band=(3000, None))),
        "worked": all_(falls("top mic off-hit energy above 3 kHz", "bleed", point="stem:top", band=(3000, None)),
                       hits_kept(point="stem:top")),
    },
    "snare.hpf-lpf": {
        "needed": all_(fact("the snare has a top mic", "has_roles", roles=("top",)), any_(
            leaf("top mic below 70 Hz on kick hits within 20 dB of its 150-250 Hz on snare hits", "cross_onset", ">=", -20,
                 point="stem:top", band=(0, 70), events="kick", other_band=(150, 250), other_events="own"),
            leaf("top mic above 12 kHz within 20 dB of its 150-250 Hz", "band_diff", ">=", -20,
                 point="stem:top", band=(12000, None), other_band=(150, 250)))),
        "worked": all_(any_(falls("top mic below 70 Hz on kick hits", "onset_band", point="stem:top", band=(0, 70), events="kick"),
                            falls("top mic above 12 kHz", "band", point="stem:top", band=(12000, None))),
                       holds("150-250 Hz", "band", band=(150, 250)),
                       any_(fact("the snare has no bottom mic", "lacks_roles", roles=("bottom",)),
                            holds("top-bottom sum gain", "matched_sum_gain", point="stem:top", other="stem:bottom", band=(150, 400)))),
    },
    "snare.ring-cut": {
        "needed": any_(leaf("a narrow peak over 6 dB above its neighbours", "narrow_peak", ">", 6, band=(150, 1000)),
                       leaf("200-500 Hz within 3 dB of the 150-250 Hz body", "band_diff", ">", -3, band=(200, 500), other_band=(150, 250)),
                       judged("a ring decays slower than the rest of the snare")),
        "worked": all_(any_(falls("narrow peak", "narrow_peak", band=(150, 1000)), falls("200-500 Hz", "band", band=(200, 500)),
                            falls("ring tail", "tail_p50")),
                       holds("150-250 Hz body", "band", band=(150, 250))),
    },
    "snare.body-crack": {
        "needed": any_(leaf("snare 2-5 kHz under the guitars' in the first 25 ms of snare hits", "onset_gap", "<", 0,
                            other="chain:guitars", band=(2000, 5000), events="snare"),
                       judged("the body sits low in the mix", "band", band=(150, 250))),
        "worked": all_(rises("snare over guitars 2-5 kHz on snare hits", "onset_gap", other="chain:guitars", band=(2000, 5000), events="snare"),
                       no_rise("hats in the mix", "loud_rel", point="chain:overheads")),
    },
    "snare.compress": compress([no_rise("off-hit bleed", "bleed")]),
    "snare.saturate": saturate((1000, 5000), "snare"),
    "snare.transient": {
        "needed": judged("attack is still low after blend, EQ and compression", "attack_p50"),
        "worked": all_(rises("attack in the full mix", "attack_p50", point="mix", events="snare"),
                       no_rise("hats in the mix", "loud_rel", point="chain:overheads")),
    },
    "snare.clip": {
        "needed": any_(leaf("the trial limiter dips more than 2 dB on snare hits", "dip", ">", 2, point="trial", events="snare"),
                       leaf("snare crest loss through the master over 3 dB", "crest_loss", ">", 3, point="trial", events="snare")),
        "worked": all_(falls("limiter dip on snare hits", "dip", point="trial", events="snare"),
                       rises("snare attack in the master", "attack_p50", point="trial", events="snare")),
    },
    "snare.reverb": {
        "needed": fact("the snare has no room mic or reverb path", "no_space"),
        "worked": all_(send_tail(), attack_holds(),
                       no_rise("low-band valley between kicks", "valley", point="mix", events="kick")),
    },
    "toms.clean": {
        "needed": leaf("off-hit energy in the tom mic within 25 dB of its hits", "bleed", ">=", -25, point="stems"),
        "worked": all_(falls("off-hit energy", "bleed"), hits_kept(),
                       delta("every hit keeps its decay to the next hit", "decay_lost", "==", 0)),
    },
    "toms.hpf": {
        "needed": leaf("energy below the tom's fundamental on kick hits within 20 dB of its fundamental on its hits", "below_f0", ">=", -20),
        "worked": all_(falls("energy below the fundamental on kick hits", "below_f0"),
                       holds("the fundamental", "f0_band", point="node")),
    },
    "toms.eq": {
        "needed": any_(leaf("an octave band over 6 dB above both neighbours on tom hits", "octave_peak", ">", 6),
                       judged("attack is low on tom hits", "attack_p50")),
        "worked": all_(falls("the peak band", "octave_peak"), rises("attack in fills, in the mix", "attack_p50", point="mix")),
    },
    "toms.compress": {
        "needed": leaf("tom hit levels spread more than 3 dB", "spread", ">", 3),
        "worked": all_(falls("spread", "spread"), attack_holds()),
    },
    "toms.multiband": {
        "needed": leaf("80-220 Hz spreads more than 4 dB across hits of equal level", "band_vs_level_spread", ">", 4, band=(80, 220)),
        "worked": all_(falls("that spread", "band_vs_level_spread", band=(80, 220)), holds("80-220 Hz weight", "band", band=(80, 220))),
    },
    "toms.saturate-clip": {
        "needed": TRIED,
        "worked": all_(any_(rises("1-5 kHz in fills", "band", band=(1000, 5000)),
                            falls("limiter dip on tom hits", "dip", point="trial", events="own")),
                       attack_holds()),
    },
    "toms.transient": {
        "needed": judged("attack is still low after EQ and compression", "attack_p50"),
        "worked": all_(rises("attack in fills, in the mix", "attack_p50", point="mix"),
                       no_rise("the valley between tom hits", "gap_valley")),
    },
    "toms.limit": peaks_tamed(),
    "toms.reverb": {
        "needed": fact("the kit has a reverb and this tom doesn't feed it", "kit_reverb_unfed"),
        "worked": all_(send_tail(), attack_holds()),
    },
    "overheads.hpf": {
        "needed": leaf("energy below 250 Hz within 10 dB of 2-8 kHz", "band_diff", ">=", -10, band=(0, 250), other_band=(2000, 8000)),
        "worked": all_(falls("energy below 250 Hz", "band", band=(0, 250)), holds("2-8 kHz", "band", band=(2000, 8000)),
                       holds("kick sum gain with the overheads", "matched_sum_gain", point="chain:kick", other="node", band=(40, 150), events="kick"),
                       holds("snare sum gain with the overheads", "matched_sum_gain", point="chain:snare", other="node", band=(150, 400), events="snare")),
    },
    "overheads.snare-control": {
        "duck": {"key": ("snare", None), "target": ("overheads", None), "owner": "target"},
        "needed": leaf("snare hits over 6 dB above the cymbal level", "hit_over_off", ">", 6, events="snare"),
        "worked": all_(falls("snare peaks in the overheads", "level_p50", events="snare"),
                       holds("cymbal level", "off_hit_level", events="snare")),
    },
    "overheads.harsh-cut": {
        "needed": any_(leaf("mix 4 kHz over the profile range", "range_outside", ">", 0, point="trial", field="bands_db.4000"),
                       leaf("a narrow cymbal peak over 6 dB above its neighbours", "narrow_peak", ">", 6, band=(2000, 16000))),
        "worked": all_(any_(in_range("bands_db.4000"), falls("narrow peak", "narrow_peak", band=(2000, 16000))),
                       holds("8-12 kHz", "band", band=(8000, 12000))),
    },
    "overheads.air": {
        "needed": leaf("mix 8 kHz under the profile range", "range_outside", "<", 0, point="trial", field="bands_db.8000"),
        "worked": all_(in_range("bands_db.8000"), no_rise("mix 4 kHz", "band", point="mix", band=(2800, 5600))),
    },
    "overheads.compress": {
        "needed": any_(leaf("cymbal level spreads more than 3 dB in the fast passage", "st_spread", ">", 3, window="fast"),
                       leaf("cymbal level spreads more than 3 dB in the sparse passage", "st_spread", ">", 3, window="sparse")),
        "worked": all_(any_(falls("fast spread", "st_spread", window="fast"), falls("sparse spread", "st_spread", window="sparse")),
                       delta("no gain swing on snare hits", "hit_swing", ">=", -1, events="snare")),
    },
    "overheads.hihat-blend": {
        "needed": fact("a hi-hat or cymbal spot mic exists", "has_role", roles=("hat", "spot")),
        "worked": all_(judged("the hat sits where it does in the overheads", "lr_position"),
                       holds("snare sum gain with the cymbals", "matched_sum_gain", point="chain:snare", other="node", band=(150, 400), events="snare")),
    },
    "overheads.room-blend": {
        "needed": fact("a room mic exists", "has_role", roles=("room",)),
        "worked": all_(rises("tail after drum hits", "tail_p50", point="mix", events="drums"),
                       no_rise("low-band valley in the fast passage", "valley", point="mix", events="kick", window="fast"),
                       no_rise("low-band valley in the sparse passage", "valley", point="mix", events="kick", window="sparse")),
    },
    "overheads.room-reverb": {
        "needed": fact("the song has no room mic", "no_role", roles=("room",)),
        "worked": all_(rises("tail after drum hits", "tail_p50", point="mix", events="drums"),
                       holds("kick attack", "attack_p50", point="chain:kick", events="kick"),
                       no_rise("low-band valley between kicks", "valley", point="mix", events="kick"),
                       leaf("mix width in the profile range", "range_outside_abs", "==", 0, point="trial", field="side_mid_db")),
    },
    "drum-bus.groups": {"needed": ALWAYS, "worked": fact("every drum stem routes to a group and every group to the drums bus", "grouped")},
    "drum-bus.compress": {
        "needed": any_(leaf("drums-bus hit levels spread more than 3 dB in the fast passage", "spread", ">", 3, window="fast"),
                       leaf("drums-bus hit levels spread more than 3 dB in the sparse passage", "spread", ">", 3, window="sparse")),
        "worked": all_(any_(falls("fast spread", "spread", window="fast"), falls("sparse spread", "spread", window="sparse")),
                       holds("kick attack in the mix", "attack_p50", point="mix", events="kick"),
                       holds("snare attack in the mix", "attack_p50", point="mix", events="snare")),
    },
    "drum-bus.parallel": {
        "needed": all_(judged("shell body and sustain are weak in the sparse passage", "tail_p50", window="sparse"),
                       judged("more serial compression lowers attack", "attack_p50")),
        "worked": all_(rises("body", "body_p50", events="shells"), rises("tail", "tail_p50", events="shells"),
                       holds("dry attack", "attack_p50", events="shells"),
                       no_rise("valley before the next hit in the fast passage", "gap_valley", events="shells", window="fast"),
                       no_rise("low-band valley in the fast passage", "valley", point="mix", events="kick", window="fast")),
    },
    "drum-bus.distort-parallel": {
        "needed": leaf("shells under the guitars in 1-5 kHz in the first 25 ms of shell hits", "onset_gap", "<", 0,
                       other="chain:guitars", band=(1000, 5000), events="shells"),
        "worked": all_(rises("shells over guitars 1-5 kHz", "onset_gap", other="chain:guitars", band=(1000, 5000), events="shells"),
                       no_rise("cymbal wash", "loud_rel", point="chain:overheads")),
    },
    "drum-bus.saturate": {
        "needed": TRIED,
        "worked": all_(any_(falls("limiter dip on kick hits", "dip", point="trial", events="kick"),
                            falls("limiter dip on snare hits", "dip", point="trial", events="snare")),
                       no_rise("limiter dip on kick hits", "dip", point="trial", events="kick"),
                       no_rise("limiter dip on snare hits", "dip", point="trial", events="snare"),
                       holds("kick attack", "attack_p50", point="mix", events="kick"),
                       holds("snare attack", "attack_p50", point="mix", events="snare")),
    },
    "drum-bus.reverb": {
        "needed": fact("the kit has no reverb send", "no_kit_reverb"),
        "worked": all_(send_tail("drums"), no_rise("low-band valley between kicks", "valley", point="mix", events="kick"),
                       leaf("tail rises more in the sparse passage than in the fast one", "tail_sparse_minus_fast", ">", 0,
                            delta=True, point="join", events="drums")),
    },
    "drum-bus.rides": {
        "needed": leaf("the drums-to-rest ratio changes more than 2 dB within a section group", "group_ratio_range", ">", 2),
        "worked": leaf("the ratio stays within 2 dB across each group", "group_ratio_range", "<=", 2),
    },
    "bass.align": {
        "needed": leaf("the bass has more than one track", "stem_count", ">", 1),
        "worked": all_(blend([(40, 250)]),
                       judged("40-250 Hz sum gain is steady across notes (p90 minus p10 of per-note sum gain)",
                              "note_sum_gain_spread", band=(40, 250), reading=True)),
        "compare": "options",
    },
    "bass.split": {
        "needed": any_(leaf("the bass has a clean and an amp or distorted track", "stem_count", ">", 1),
                       judged("its one track lacks mid growl", "band", band=(700, 3000))),
        "worked": all_(falls("low-band note level spread", "note_spread", band=(40, 150)),
                       rises("700 Hz-3 kHz", "band", band=(700, 3000))),
    },
    "bass.rides": {
        "needed": any_(leaf("note levels spread more than 3 dB in the fast passage", "note_spread", ">", 3, window="fast"),
                       leaf("note levels spread more than 3 dB in the sparse passage", "note_spread", ">", 3, window="sparse"),
                       leaf("section loudness swings more than 3 dB within a group", "group_swing", ">", 3)),
        "worked": all_(leaf("fast note spread under 2 dB", "note_spread", "<", 2, window="fast"),
                       leaf("sparse note spread under 2 dB", "note_spread", "<", 2, window="sparse"),
                       leaf("group swing under 2 dB", "group_swing", "<", 2)),
    },
    "bass.hpf": {
        "needed": leaf("energy below the lowest note within 10 dB of it", "below_lowest_note", ">=", -10),
        "worked": all_(falls("energy below the lowest note", "below_lowest_note"),
                       delta("the lowest notes lose under 1 dB", "lowest_notes_level", ">=", -1)),
    },
    "bass.mud-cut": {
        "needed": any_(overlap_present("bass and guitars overlap in 150-500 Hz", other="chain:guitars", band=(150, 500)),
                       leaf("mix 250 Hz over the profile range", "range_outside", ">", 0, point="trial", field="bands_db.250")),
        "worked": all_(falls("bass-guitar overlap in 150-500 Hz", "overlap", other="chain:guitars", band=(150, 500)),
                       holds("note attacks in 700 Hz-3 kHz", "onset_band", band=(700, 3000), events="notes")),
    },
    "bass.growl": {
        "needed": any_(leaf("bass 700 Hz-3 kHz under the guitars'", "band_gap", "<", 0, other="chain:guitars", band=(700, 3000)),
                       judged("harsh fret noise at 2-4 kHz", "band", band=(2000, 4000))),
        "worked": all_(any_(rises("bass over guitars in 700 Hz-3 kHz", "band_gap", other="chain:guitars", band=(700, 3000)),
                            judged("fret noise falls", "band", band=(2000, 4000))),
                       holds("guitar level", "loud_rel", point="chain:guitars")),
    },
    "bass.compress": {
        "needed": any_(leaf("note levels spread more than 3 dB in the fast passage", "note_spread", ">", 3, window="fast"),
                       leaf("note levels spread more than 3 dB in the sparse passage", "note_spread", ">", 3, window="sparse")),
        "worked": all_(leaf("fast note spread under 3 dB", "note_spread", "<", 3, window="fast"),
                       leaf("sparse note spread under 3 dB", "note_spread", "<", 3, window="sparse"),
                       holds("pick attack", "note_attack")),
    },
    "bass.multiband": {
        "needed": leaf("low-band note level spreads more than 3 dB", "note_spread", ">", 3, band=(40, 150)),
        "worked": all_(falls("low-band note spread", "note_spread", band=(40, 150)),
                       holds("the lowest notes", "lowest_notes_level")),
    },
    "bass.saturate": {
        "needed": judged("700 Hz-3 kHz harmonics are weak after the split", "band", band=(700, 3000)),
        "worked": all_(rises("700 Hz-3 kHz", "band", band=(700, 3000)),
                       no_rise("low-band note spread", "note_spread", band=(40, 150))),
    },
    "bass.chorus": {
        "needed": TRIED,
        "worked": all_(rises("side energy above 500 Hz in the delivered bass", "side_share", point="delivered", band=(500, None)),
                       no_rise("side energy below 150 Hz in the delivered bass", "side_share", point="delivered", band=(0, 150)),
                       holds("mono-fold energy above 500 Hz in the delivered bass", "mono_band", point="delivered", band=(500, None)),
                       holds("mix mono loudness", "mono_lufs", point="mix")),
    },
    "bass.limit": {
        "needed": leaf("some notes sit more than 3 dB over the median", "note_top", ">", 3),
        "worked": all_(falls("loudest notes over the median", "note_top"), holds("attack", "note_attack")),
    },
    "guitars.align": {
        # Which tracks share a take isn't in the record's control: it names them, and their sum shows whether they are one.
        "needed": judged("two or more audible tracks capture the same take"),
        "worked": all_(leaf("the named take is one performance (best correlation at least 0.25)", "take_sum", ">=", ONE_TAKE,
                            band=(100, 2000)),
                       blend([(100, 2000)])),
        "compare": "options",
    },
    "guitars.pair": {
        "needed": leaf("more than one rhythm guitar track in the song", "stem_count", ">", 1, roles=("rhythm",)),
        "worked": all_(leaf("left and right rhythm loudness differ by under 1 dB", "lr_loudness", "<", 1, point="rhythm"),
                       leaf("mix width in the profile range", "range_outside_abs", "==", 0, point="trial", field="side_mid_db")),
    },
    "guitars.side-tones": {
        "needed": leaf("left and right rhythm spectra match within 1 dB in every octave", "lr_band_diff", "<=", 1, point="rhythm"),
        "worked": all_(rises("mix side energy in 500 Hz-4 kHz", "side", point="mix", band=(500, 4000)),
                       holds("mono loudness", "mono_lufs", point="mix"),
                       leaf("left and right rhythm loudness within 1 dB", "lr_loudness", "<=", 1, point="rhythm")),
    },
    "guitars.filters": {
        "needed": any_(overlap_present("guitars overlap the kick and bass below 120 Hz", other="chain:kick+bass", band=(0, 120)),
                       judged("fizz above 10 kHz", "band", band=(10000, None))),
        "worked": all_(leaf("each fault found falls", "faults_fall", "==", 1, other="chain:kick+bass"),
                       judged("80-120 Hz on palm mutes holds within 1 dB")),
    },
    "guitars.resonance": {
        "needed": judged("a narrow peak over 6 dB above its neighbours that stays across notes", "narrow_peak_persistence"),
        "worked": all_(falls("the peak", "narrow_peak", band=(100, 16000)), holds("guitar level", "loud_rel")),
    },
    "guitars.mud-cut": {
        "needed": any_(leaf("mix 250 Hz over the profile range", "range_outside", ">", 0, point="trial", field="bands_db.250"),
                       leaf("mix 500 Hz over the profile range", "range_outside", ">", 0, point="trial", field="bands_db.500"),
                       overlap_present("guitars and bass overlap in 150-500 Hz", other="chain:bass", band=(150, 500))),
        "worked": all_(leaf("each band found over range lands in it", "bands_in_range", "==", 1, point="trial",
                            fields=("bands_db.250", "bands_db.500")),
                       leaf("the 150-500 Hz overlap with the bass falls, if it was present", "overlap_falls", "==", 1,
                            other="chain:bass", band=(150, 500)),
                       holds("guitar 1-4 kHz level", "band", band=(1000, 4000))),
    },
    "guitars.multiband": {
        # Palm mutes can't be told from open chords on an amp track, whose compression flattens their decay.
        "needed": judged("80-260 Hz loudness differs more than 4 dB between palm mutes and open chords"),
        "worked": all_(judged("that difference falls"), holds("80-260 Hz", "band", band=(80, 260))),
    },
    "guitars.mirror-eq": {
        "needed": any_(fact("bass.growl is kept or changed", "move_active", move="bass.growl"),
                       overlap_present("guitars and bass overlap in 700 Hz-3 kHz", other="chain:bass", band=(700, 3000))),
        "worked": all_(falls("overlap in 700 Hz-3 kHz", "overlap", other="chain:bass", band=(700, 3000)),
                       holds("guitar level", "loud_rel"), holds("bass level", "loud_rel", point="chain:bass")),
    },
    "guitars.lead-room": {
        "duck": {"key": ("guitars", "lead"), "target": ("guitars", "rhythm")},
        "needed": any_(leaf("kick 2-5 kHz under the rhythms' on kick hits", "onset_gap", "<", 0, point="chain:kick", other="rhythm",
                            band=(2000, 5000), events="kick"),
                       leaf("snare 2-5 kHz under the rhythms' on snare hits", "onset_gap", "<", 0, point="chain:snare", other="rhythm",
                            band=(2000, 5000), events="snare"),
                       all_(fact("the song has a lead", "has_roles", roles=("lead",)),
                            leaf("a lead's 2-4 kHz under the rhythms' while it plays", "lead_gap", "<", 0, band=(2000, 4000)))),
        "worked": all_(leaf("each gap found closes", "gaps_close", "==", 1),
                       holds("rhythm 2-4 kHz away from hits and leads", "off_hit_band", point="rhythm", band=(2000, 4000), events="drums")),
    },
    "guitars.compress": {
        "needed": any_(leaf("level spreads more than 3 dB in the fast passage", "st_spread", ">", 3, window="fast"),
                       leaf("level spreads more than 3 dB in the sparse passage", "st_spread", ">", 3, window="sparse"),
                       leaf("level spreads more than 3 dB within a section", "section_spread", ">", 3)),
        "worked": all_(any_(falls("fast spread", "st_spread", window="fast"), falls("sparse spread", "st_spread", window="sparse"),
                            falls("section spread", "section_spread")),
                       no_rise("4-8 kHz", "band", band=(4000, 8000))),
    },
    "guitars.saturate": {
        "needed": TRIED,
        "worked": all_(no_rise("4-8 kHz at equal loudness", "band", band=(4000, 8000)),
                       falls("peaks at equal loudness", "peak_to_loudness")),
    },
    "guitars.rides": {
        "needed": leaf("the guitars-to-rest ratio changes more than 2 dB within a section group", "group_ratio_range", ">", 2),
        "worked": leaf("the ratio stays within 2 dB across each group", "group_ratio_range", "<=", 2),
    },
    "guitars.lead": {
        "needed": fact("the source has a lead", "has_roles", roles=("lead",)),
        "worked": all_(leaf("lead 1-4 kHz above the rhythms' while it plays", "lead_gap", ">", 0, band=(1000, 4000)),
                       leaf("a lead-fed Delay's tail in the 2 s after each phrase, as heard in the mix, within 30 dB of "
                            "the dry lead", "delay_gap_tail", ">", -30)),
    },
    "synths.role": {"needed": fact("the song has synth or keys stems", "always"), "worked": fact("every synth source has a role", "has_synth_role")},
    "synths.hpf": {
        "needed": all_(fact("a pad or keys part", "synth_role", roles=("pad",)),
                       judged("it holds energy below 150 Hz", "band", band=(0, 150))),
        "worked": all_(falls("overlap with the bass and kick below 150 Hz", "overlap", other="chain:kick+bass", band=(0, 150)),
                       holds("its main band", "main_band")),
    },
    "synths.cut-in-context": {
        "needed": overlap_present("a pad overlaps the guitars in 200 Hz-4 kHz", other="chain:guitars", band=(200, 4000)),
        "worked": all_(falls("overlap with the guitars", "overlap", other="chain:guitars", band=(200, 4000)),
                       holds("level in the sparse passage", "loud_rel", window="sparse")),
    },
    "synths.colour": {
        "needed": any_(judged("masked in its main band", "main_band"), judged("harsh in 4-8 kHz", "band", band=(4000, 8000))),
        "worked": any_(rises("main band against the guitars", "main_band_gap", other="chain:guitars"),
                       falls("4-8 kHz", "band", band=(4000, 8000))),
    },
    "synths.space": {
        "needed": leaf("its tail after notes is under the drums' tail after hits", "tail_vs_drums", "<", 0, events="notes"),
        "worked": all_(rises("tail after its notes", "contribution_tail", point="join", events="notes"),
                       leaf("pad note attacks stay under the guitars' level", "pad_attack_gap", "<", 0, other="chain:guitars")),
    },
    "mix-bus.trial-master": {"needed": ALWAYS, "worked": fact("the last master step is a limiter", "trial_limiter")},
    "mix-bus.console": {
        "needed": TRIED,
        "worked": all_(any_(falls("limiter dip on kick hits", "dip", point="trial", events="kick"),
                            falls("limiter dip on snare hits", "dip", point="trial", events="snare")),
                       no_rise("limiter dip on kick hits", "dip", point="trial", events="kick"),
                       no_rise("limiter dip on snare hits", "dip", point="trial", events="snare"),
                       holds("kick attack", "attack_p50", point="mix", events="kick"),
                       holds("snare attack", "attack_p50", point="mix", events="snare")),
    },
    "mix-bus.compress": {
        "needed": any_(leaf("mix level spreads more than 3 dB in the fast passage", "st_spread", ">", 3, point="mix", window="fast"),
                       leaf("mix level spreads more than 3 dB in the sparse passage", "st_spread", ">", 3, point="mix", window="sparse")),
        "worked": all_(any_(falls("fast spread", "st_spread", point="mix", window="fast"),
                            falls("sparse spread", "st_spread", point="mix", window="sparse")),
                       holds("kick attack", "attack_p50", point="mix", events="kick"),
                       holds("snare attack", "attack_p50", point="mix", events="snare"),
                       delta("section loudness differences hold", "section_error_change", "<=", HOLDS_DB, point="mix")),
    },
    "mix-bus.tone": {
        "needed": leaf("the lowest or highest octave bands are outside the profile range", "edge_bands_outside", ">", 0, point="trial"),
        "worked": leaf("those bands move into range", "edge_bands_outside", "==", 0, point="trial"),
    },
    "mix-bus.rides": {
        "needed": leaf("a section more than 1.5 LU off its intended loudness", "section_error", ">", 1.5, point="mix"),
        "worked": leaf("every section within 1.5 LU", "section_error", "<=", 1.5, point="mix"),
    },
    "master.clip": {
        "needed": any_(leaf("the limiter dips more than 2 dB on kick hits", "dip", ">", 2, point="trial", events="kick"),
                       leaf("the limiter dips more than 2 dB on snare hits", "dip", ">", 2, point="trial", events="snare")),
        "worked": all_(any_(falls("dip on kick hits", "dip", point="trial", events="kick"),
                            falls("dip on snare hits", "dip", point="trial", events="snare")),
                       holds("kick attack", "attack_p50", point="trial", events="kick"),
                       holds("snare attack", "attack_p50", point="trial", events="snare"),
                       no_rise("2-8 kHz", "band", point="trial", band=(2000, 8000))),
    },
    "master.compress": {
        "needed": leaf("the limiter does more than 4 dB beyond plain gain", "limiter_work", ">", 4, point="trial"),
        "worked": all_(falls("limiter work", "limiter_work", point="trial"),
                       holds("kick attack", "attack_p50", point="trial", events="kick"),
                       holds("snare attack", "attack_p50", point="trial", events="snare")),
    },
    "master.tone": {
        "needed": leaf("an octave band or the width is outside the profile range", "any_outside", ">", 0, point="trial"),
        "worked": leaf("they move into range", "any_outside", "==", 0, point="trial"),
    },
    "master.limit": {
        "needed": ALWAYS,
        "worked": all_(leaf("true peak at or below -1 dBTP", "true_peak", "<=", -1, point="trial"),
                       leaf("loudness within 0.005 LU of the profile", "loudness_error", "<=", 0.005, point="trial"),
                       leaf("kick crest loss under 3 dB", "crest_loss", "<", 3, point="trial", events="kick"),
                       leaf("snare crest loss under 3 dB", "crest_loss", "<", 3, point="trial", events="snare"),
                       leaf("limiter dip is the lowest of the settings tried", "dip_vs_tried", "<=", 0, point="trial")),
        "compare": "tried",
    },
}

# Starting trials for always-tried moves, from each move's Start and the saturate skill's light settings.
# `core` is the step that must be in the Tool list; the rest is the routing and gain the trial needs.
SATURATOR = {"plugin": "T-Saturator", "settings": {"type": "Tube", "saturation": 20}}
TAPE = {"plugin": "CHOWTapeModel", "settings": {}}
# bass.chorus sends at -12 dB through a 500 Hz high-pass, so its return sits 12 dB under the node's own 500 Hz+ band.
DEFAULT_TRIALS = {
    "kick.saturate": {"core": SATURATOR},
    "snare.saturate": {"core": SATURATOR},
    "toms.saturate-clip": {"core": SATURATOR},
    "drum-bus.saturate": {"core": TAPE},
    "guitars.saturate": {"core": TAPE},
    "mix-bus.console": {"core": {"plugin": "Airwindows Consolidated",
                                 "settings": {"effect": "BussColors4", "color": "Dark", "input_trim": 0.0, "output_trim": 0.0, "dry_wet": 1.0}}},
    "bass.chorus": {
        "core": {"plugin": "Airwindows Consolidated", "settings": {"effect": "StereoChorus", "speed": 0.5, "depth": 0.5}},
        "send_db": -12,
        "before": [{"plugin": "ZL Equalizer 2", "settings": {
            "filter_status0_filter_status": "On", "filter_type0_filter_type": "High Pass", "order0_order": "24 dB/oct",
            "freq0_freq": 500.0}}],
    },
}


def lint(docs, tools):
    """Contracts and chain docs name the same moves, and each default trial uses a plugin its move lists."""
    errors = []
    moves = {move: bullets for chain in docs.values() for move, bullets in chain.items()}
    errors += [f"{move}: no evidence contract in contracts.py" for move in sorted(set(moves) - set(CONTRACTS))]
    errors += [f"contracts.py: {move} is not a move in docs/mixing-chains" for move in sorted(set(CONTRACTS) - set(moves))]
    for move, trial in DEFAULT_TRIALS.items():
        core = trial["core"]
        name = core["settings"].get("effect") if core["plugin"] == "Airwindows Consolidated" else core["plugin"]
        if move in moves and name not in tools(moves[move])[0]:
            errors.append(f"contracts.py: {move}'s default trial uses {name}, which its Tool list doesn't name")
    return errors
