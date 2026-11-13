import numpy as np
import pytest

import native


def reference(level, attack, release):
    envelope, state = np.empty(len(level)), 0.0
    for index, value in enumerate(level.tolist()):
        coefficient = attack if value > state else release
        state = coefficient * state + (1 - coefficient) * value
        envelope[index] = state
    return envelope


def levels():
    rng = np.random.default_rng(7)
    tiny = np.finfo(float).tiny
    impulses = np.zeros(20000)
    impulses[::997] = 1.0
    bursts = np.abs(rng.standard_normal(30000)) * np.repeat(rng.integers(0, 2, 30), 1000)
    return {
        "empty": np.zeros(0),
        "one": np.array([0.5]),
        "silence": np.zeros(5000),
        "noise": np.abs(rng.standard_normal(200000)) * 0.3,
        "loud": np.abs(rng.standard_normal(5000)) * 1e3,
        "denormal": np.abs(rng.standard_normal(5000)) * tiny * 1e-3,
        "around tiny": tiny * np.array([0.5, 1, 2, 1, 0.5, 0, 1e-5, 3, 3, 3]),
        "impulses": impulses,
        "bursts": bursts,
        "constant": np.full(5000, 0.25),
        "steps": np.repeat([0.0, 0.5, 0.5, 0.1, 0.9, 0.0], 3000),
        "float32": (np.abs(rng.standard_normal(20000)) * 0.3).astype(np.float32),
    }


@pytest.mark.parametrize("name", levels())
@pytest.mark.parametrize("attack_ms, release_ms", [(1, 60), (0, 60), (1, 0), (0, 0), (0.01, 5000), (50, 0.5)])
def test_follow_matches_python_loop_bit_for_bit(name, attack_ms, release_ms):
    level = levels()[name]
    attack, release = native.smoothing(attack_ms, 48000), native.smoothing(release_ms, 48000)
    assert native.follow(level, attack, release).tobytes() == reference(level, attack, release).tobytes()


def test_follow_holds_a_level_equal_to_its_state():
    # Once the state equals the input, neither branch may move it.
    level = np.full(1000, 0.5)
    assert native.follow(level, 0.0, 0.99).tobytes() == reference(level, 0.0, 0.99).tobytes()
    assert (native.follow(level, 0.0, 0.99) == 0.5).all()
