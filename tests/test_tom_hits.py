import numpy as np
import soundfile
import pytest

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


def own_hits(song, **mics):
    """Each tom's own hits, in seconds, with every mic its own tom source."""
    for name, audio in mics.items():
        write(song, f"{name}.wav", audio)
    sources = {name: {"chain": "toms", "stems": [f"{name}.wav"]} for name in mics}
    recipe = {"stems": {f"{name}.wav": {} for name in mics}, "sources": sources}
    rate, length = render.timeline(recipe, song)
    raw = {name: evidence.timing(recipe, song, source, rate, length)[1] for name, source in sources.items()}
    return {name: evidence.own_tom_hits(recipe, song, sources, raw, f"{name}.wav", raw[name], rate, length) / RATE
            for name in sources}


@pytest.mark.parametrize("modes, scale", [([(80, 0.5, 1)], 1), ([(80, 0.5, 1), (128, 0.5, 0.3)], 0.5)],
                         ids=["one-mode", "two-modes"])
def test_a_fill_hit_while_the_last_tom_still_rings_louder_is_kept(song, modes, scale):
    length = 8 * RATE
    tom1 = scale * strikes([0.5, 2.5, 4.5, 6.0], [1.0] * 4, modes, length)
    tom3 = scale * strikes([1.5, 3.5, 5.0, 6.12], [0.3] * 4, [(120, 0.5, 1)], length)
    own = own_hits(song, tom1=tom1 + BLEED * tom3, tom3=tom3 + BLEED * tom1)
    assert np.allclose(own["tom1"], [0.5, 2.5, 4.5, 6.0], atol=0.01)
    assert np.allclose(own["tom3"], [1.5, 3.5, 5.0, 6.12], atol=0.01)


@pytest.mark.parametrize("gap", [0.15, 0.15625], ids=["150ms", "156ms-cancels-the-ring"])
def test_spill_of_a_strike_masked_in_its_own_mic_is_not_the_other_tom(song, gap):
    length = 4 * RATE
    times, amps = [t + d for t in (0.5, 1.5, 2.5) for d in (0, gap)], [1, 0.3] * 3
    tom1 = strikes(times, amps, [(80, 0.5, 1), (1000, 0.03, 1)], length)
    tom3 = strikes(times, amps, [(80, 0.5, 10 ** (-60 / 20)), (1000, 0.03, BLEED)], length)
    assert len(own_hits(song, tom1=tom1, tom3=tom3)["tom3"]) == 0


def test_toms_struck_together_keep_the_louder_in_each_mic(song):
    length = 8 * RATE
    times = [0.5 + i for i in range(8)]
    tom1 = strikes(times, [1, 0.9] * 4, [(80, 0.1, 1)], length)
    tom3 = strikes(times, [0.9, 1] * 4, [(120, 0.1, 1)], length)
    own = own_hits(song, tom1=tom1 + BLEED * tom3, tom3=tom3 + BLEED * tom1)
    assert np.allclose(own["tom1"], times[0::2], atol=0.01)
    assert np.allclose(own["tom3"], times[1::2], atol=0.01)


@pytest.mark.parametrize("delay_ms", [3, 4, 25])
def test_bleed_that_arrives_late_is_not_the_quiet_tom(song, delay_ms):
    length = 5 * RATE
    tom1 = strikes([0.5, 1.5, 2.5, 3.5], [0.5] * 4, [(80, 0.5, 1)], length)
    delay = round(delay_ms / 1000 * RATE)
    tom3 = BLEED * np.concatenate([np.zeros(delay), tom1[:-delay]])
    own = own_hits(song, tom1=tom1, tom3=tom3)
    assert len(own["tom1"]) == 4
    assert len(own["tom3"]) == 0


@pytest.mark.parametrize("start_ms", [0.5, 2, 4])
def test_a_hit_at_the_very_start_belongs_to_its_own_tom(song, start_ms):
    start = round(start_ms / 1000 * RATE)
    n = np.arange(RATE - start) / RATE
    x = np.concatenate([np.zeros(start), np.sin(2 * np.pi * 80 * n) * np.exp(-n / 0.5)])
    own = own_hits(song, played=x, near=0.028 * x, far=0.001 * x)
    assert [len(own[name]) for name in ("played", "near", "far")] == [1, 0, 0]


def test_another_toms_ring_that_beats_does_not_take_a_hit(song):
    length = 2 * RATE
    a = strikes([0.5, 0.664], [1, 0.1], [(80, 0.5, 1), (90, 0.5, 1), (1000, 0.01, 0.5)], length)
    b = strikes([0.5], [1], [(80, 0.5, 1), (90, 0.5, 1)], length)
    own = own_hits(song, a=0.2 * (a + BLEED * b), b=0.2 * (b + BLEED * a))
    assert len(own["a"]) == 2


def test_late_bleed_does_not_push_out_the_quiet_toms_own_soft_hits(song):
    length = 5 * RATE
    tom1 = strikes([0.5, 1.5, 2.5, 3.5], [0.5] * 4, [(80, 0.5, 1)], length)
    delay = round(0.025 * RATE)
    tom3 = BLEED * np.concatenate([np.zeros(delay), tom1[:-delay]]) + strikes([0.1, 0.2, 0.3], [0.001] * 3, [(120, 0.02, 1)], length)
    assert np.allclose(own_hits(song, tom1=tom1, tom3=tom3)["tom3"], [0.1, 0.2, 0.3], atol=0.01)


@pytest.mark.parametrize("count", [3, 8])
def test_a_roll_on_one_tom_is_not_the_other(song, count):
    length = 2 * RATE
    times, amps = [0.5 + 0.02 * i for i in range(count)], [1] + [0.3] * (count - 1)
    played = strikes(times, amps, [(80, 0.5, 1), (1000, 0.003, 1)], length)
    unplayed = strikes(times, amps, [(80, 0.5, 0.001), (1000, 0.003, BLEED)], length)
    assert len(own_hits(song, played=played, unplayed=unplayed)["unplayed"]) == 0


def test_a_louder_toms_beating_tail_does_not_take_a_fill_hit(song):
    length = 4 * RATE
    a = strikes([0.675, 1.675, 2.675], [0.2] * 3, [(120, 0.15, 1), (1000, 0.01, 0.5)], length)
    b = strikes([0.5, 1.5, 2.5], [0.5] * 3, [(80, 0.5, 1), (90, 0.5, 1), (1000, 0.01, 0.5)], length)
    noise = np.random.default_rng(417).normal(size=(2, length)) * 0.001
    own = own_hits(song, a=a + BLEED * b + noise[0], b=b + BLEED * a + noise[1])
    assert np.allclose(own["a"], [0.675, 1.675, 2.675], atol=0.01)


@pytest.mark.parametrize("rate", [8000, 11025])
def test_a_low_sample_rate_reads_tom_hits(song, rate):
    n = np.arange(rate) / rate
    x = np.where(n >= 0.3, 0.5 * np.sin(2 * np.pi * 40 * (n - 0.3)) * np.exp(-(n - 0.3) / 0.5), 0)
    for name, audio in (("played", x), ("unplayed", 0.028 * x)):
        soundfile.write(song / "stems" / f"{name}.wav", audio, rate, subtype="FLOAT")
    sources = {name: {"chain": "toms", "stems": [f"{name}.wav"]} for name in ("played", "unplayed")}
    recipe = {"stems": {f"{name}.wav": {} for name in sources}, "sources": sources}
    rate, length = render.timeline(recipe, song)
    raw = {name: evidence.timing(recipe, song, source, rate, length)[1] for name, source in sources.items()}
    own = {name: evidence.own_tom_hits(recipe, song, sources, raw, f"{name}.wav", raw[name], rate, length) for name in sources}
    assert (len(own["played"]), len(own["unplayed"])) == (1, 0)
