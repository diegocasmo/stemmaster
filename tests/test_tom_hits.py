import numpy as np

import evidence
import render
from helpers import RATE, write

BLEED = 10 ** (-31 / 20)


def strikes(times, amps, modes, length):
    """Damped sines, one per (frequency, decay seconds, gain) mode, struck at each time."""
    out = np.zeros(length)
    for t, amp in zip(times, amps):
        start = round(t * RATE)
        n = np.arange(length - start) / RATE
        for freq, decay, gain in modes:
            out[start:] += amp * gain * np.sin(2 * np.pi * freq * n) * np.exp(-n / decay)
    return out


def own_hits(song, tom1, tom3):
    write(song, "tom1.wav", tom1)
    write(song, "tom3.wav", tom3)
    sources = {"tom1": {"chain": "toms", "stems": ["tom1.wav"]}, "tom3": {"chain": "toms", "stems": ["tom3.wav"]}}
    recipe = {"stems": {"tom1.wav": {}, "tom3.wav": {}}, "sources": sources}
    rate, length = render.timeline(recipe, song)
    raw = {name: evidence.timing(recipe, song, source, rate, length)[1] for name, source in sources.items()}
    return {name: evidence.own_tom_hits(recipe, song, sources, raw, f"{name}.wav", raw[name], rate, length) / RATE
            for name in sources}


def test_a_fill_hit_while_the_last_tom_still_rings_louder_is_kept(song):
    length = 8 * RATE
    tom1 = strikes([0.5, 2.5, 4.5, 6.0], [1.0] * 4, [(80, 0.5, 1)], length)
    tom3 = strikes([1.5, 3.5, 5.0, 6.12], [0.3] * 4, [(120, 0.5, 1)], length)
    own = own_hits(song, tom1 + BLEED * tom3, tom3 + BLEED * tom1)
    assert np.allclose(own["tom1"], [0.5, 2.5, 4.5, 6.0], atol=0.01)
    assert np.allclose(own["tom3"], [1.5, 3.5, 5.0, 6.12], atol=0.01)


def test_spill_of_a_strike_masked_in_its_own_mic_is_not_the_other_tom(song):
    length = 4 * RATE
    times, amps = [0.5, 0.65, 1.5, 1.65, 2.5, 2.65], [1, 0.3] * 3
    tom1 = strikes(times, amps, [(80, 0.5, 1), (1000, 0.03, 1)], length)
    tom3 = strikes(times, amps, [(80, 0.5, 10 ** (-60 / 20)), (1000, 0.03, BLEED)], length)
    assert len(own_hits(song, tom1, tom3)["tom3"]) == 0


def test_toms_struck_together_keep_the_louder_in_each_mic(song):
    length = 8 * RATE
    times = [0.5 + i for i in range(8)]
    tom1 = strikes(times, [1, 0.9] * 4, [(80, 0.1, 1)], length)
    tom3 = strikes(times, [0.9, 1] * 4, [(120, 0.1, 1)], length)
    own = own_hits(song, tom1 + BLEED * tom3, tom3 + BLEED * tom1)
    assert np.allclose(own["tom1"], times[0::2], atol=0.01)
    assert np.allclose(own["tom3"], times[1::2], atol=0.01)
