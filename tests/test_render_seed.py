import render


class Plugin:
    parameters = {}


def test_every_plugin_load_starts_from_the_same_rand_state(monkeypatch, tmp_path):
    (tmp_path / "Fake.vst3").mkdir()
    monkeypatch.setattr(render, "PLUGINS", tmp_path)
    draws = []

    def load_plugin(path):
        draws.append(render.LIBC.rand())
        return Plugin()

    monkeypatch.setattr(render.pedalboard, "load_plugin", load_plugin)
    step = {"id": "fake", "plugin": "Fake", "settings": {}}
    render.load(step)
    render.LIBC.rand()
    render.load(step)
    assert draws[0] == draws[1]
