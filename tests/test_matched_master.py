from types import SimpleNamespace

import numpy as np

import evidence


class Result:
    rate, mix, master_steps = 48000, np.ones(4), [{"id": "limiter"}]

    def __init__(self):
        self.mixer = SimpleNamespace(probed={})

    def master(self, mix):
        self.mixer.probed["limiter"] = round(float(20 * np.log10(mix[0])), 6)
        return mix, mix


def test_the_master_probes_are_the_best_drives_not_the_last_tried(monkeypatch):
    # Loudness that isn't monotonic in drive: the second try lands further from the target than the first.
    errors = {0.0: 2.0, -2.0: 5.0}
    monkeypatch.setattr(evidence, "loudness", lambda out, rate: -14 + errors[round(float(20 * np.log10(out[0])), 6)])
    monkeypatch.setattr(evidence, "MATCH_ROUNDS", 1)
    result = Result()
    numbers, _, _ = evidence.matched_master(result, -14)
    assert numbers["drive_change_db"] == 0.0
    assert result.mixer.probed["limiter"] == 0.0
