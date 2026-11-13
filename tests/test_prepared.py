import json
import os
from collections import OrderedDict
from pathlib import Path
import subprocess
import sys

import pytest

import prepared
import render


@pytest.fixture
def host(monkeypatch, tmp_path):
    events = []

    class Parameter:
        def __init__(self, plugin, name):
            self.plugin, self.name = plugin, name

        @property
        def raw_value(self):
            return getattr(self.plugin, self.name)

        @raw_value.setter
        def raw_value(self, value):
            setattr(self.plugin, self.name, value)

    class Plugin:
        parameters = dict.fromkeys(("frequency", "gain", "program", "current_program", "factory_preset", "preset_bank"))
        frequency = gain = 0.0

        def __set_initial_parameter_values__(self, values):
            events.append(("normal_setup", values))

        def __setattr__(self, name, value):
            events.append(("set", name, value))
            super().__setattr__(name, value)

        @property
        def _parameters(self):
            return [Parameter(self, name) for name in ("frequency", "gain")]

    def load_plugin(path, parameter_values=None):
        events.append(("load", render.LIBC.rand(), type(parameter_values) is prepared._SkipDiscovery))
        plugin = Plugin()
        plugin.__set_initial_parameter_values__(parameter_values)
        return plugin

    bundle = tmp_path / "Proven.vst3"
    bundle.mkdir()
    (bundle / "binary").write_bytes(b"original")
    monkeypatch.setattr(render, "PLUGINS", tmp_path)
    monkeypatch.setattr(prepared, "WHITELIST", {"Proven": prepared.bundle_hash(bundle)})
    monkeypatch.setattr(prepared, "runtime", lambda: {"test": 1})
    monkeypatch.setattr(prepared, "NORMAL_SETUP", Plugin.__set_initial_parameter_values__)
    monkeypatch.setattr(prepared, "_CACHE", OrderedDict())
    monkeypatch.setattr(prepared, "_REJECTED", OrderedDict())
    monkeypatch.setattr(render.pedalboard, "VST3Plugin", Plugin)
    monkeypatch.setattr(render.pedalboard, "load_plugin", load_plugin)
    return {"events": events, "settings": {"frequency": 517.2, "gain": -2.3}, "bundle": bundle}


def load(host, rate=44100, channels=2, name="Proven"):
    return render.load({"plugin": name, "settings": host["settings"]}, rate=rate, channels=channels)


def markers(host):
    return [event[2] for event in host["events"] if event[0] == "load"]


def test_load_caches_validated_values_without_dsp_and_replays_into_fresh_instances(host):
    first = load(host)
    assert [event[1] for event in host["events"] if event[0] == "set"] == ["frequency", "gain"]
    render.LIBC.rand()
    second = load(host)
    assert first is not second
    assert prepared._values(first) == prepared._values(second)
    assert markers(host) == [False, True]
    draws = [event[1] for event in host["events"] if event[0] == "load"]
    assert draws[0] == draws[1]


@pytest.mark.parametrize("change", ["plugin", "bundle", "settings", "order", "value_type", "rate", "channels", "no_context", "dependency", "hook"])
def test_other_key_or_unproved_plugin_loads_normally(host, monkeypatch, change):
    if change == "value_type":
        host["settings"]["frequency"] = 517.0
    load(host)
    rate, channels, name = 44100, 2, "Proven"
    if change == "plugin":
        name = "Other"
        (render.PLUGINS / "Other.vst3").mkdir()
    elif change == "bundle":
        (host["bundle"] / "binary").write_bytes(b"replaced")
    elif change == "settings":
        host["settings"]["frequency"] = 612.8
    elif change == "order":
        host["settings"] = dict(reversed(list(host["settings"].items())))
    elif change == "value_type":
        host["settings"]["frequency"] = 517
    elif change == "rate":
        rate = 48000
    elif change == "channels":
        channels = 4
    elif change == "no_context":
        rate, channels = None, None
    elif change == "dependency":
        monkeypatch.setattr(prepared, "runtime", lambda: {"test": 2})
    elif change == "hook":
        monkeypatch.setattr(render.pedalboard.VST3Plugin, "__set_initial_parameter_values__", lambda *args: None)
    plugin = load(host, rate, channels, name)
    assert plugin.frequency == host["settings"]["frequency"]
    assert markers(host) == [False, False]


@pytest.mark.parametrize("selector", ["program", "current_program", "factory_preset", "preset_bank"])
def test_preset_selectors_always_load_normally(host, selector):
    host["settings"][selector] = "Preset A"
    load(host)
    load(host)
    assert markers(host) == [False, False]
    assert not prepared._CACHE


def test_readback_failure_reseeds_normal_fallback_and_rejects_key(host, monkeypatch):
    first = load(host)
    parameter_type = type(first._parameters[0])
    descriptor = parameter_type.raw_value
    monkeypatch.setattr(parameter_type, "raw_value", property(descriptor.fget, lambda *args: None))
    key = next(iter(prepared._CACHE))
    plugin = load(host)
    assert plugin.frequency == 517.2
    assert markers(host) == [False, True, False]
    assert key not in prepared._CACHE
    assert key in prepared._REJECTED
    assert len({event[1] for event in host["events"] if event[0] == "load"}) == 1
    load(host)
    assert markers(host) == [False, True, False, False]


def test_cache_evicts_old_entries(host, monkeypatch):
    monkeypatch.setattr(prepared, "MAX_ENTRIES", 1)
    load(host)
    host["settings"]["frequency"] = 612.8
    load(host)
    host["settings"]["frequency"] = 517.2
    load(host)
    assert markers(host) == [False, False, False]


def test_normal_validation_survives_installed_hook(host):
    load(host)
    load(host)
    host["settings"]["unknown"] = 1
    with pytest.raises(SystemExit, match="has no parameter unknown"):
        load(host)


def test_bundle_hash_reuses_unchanged_files_and_invalidates_changed_stat(monkeypatch, tmp_path):
    bundle = tmp_path / "Bundle.vst3"
    bundle.mkdir()
    binary = bundle / "binary"
    binary.write_bytes(b"original")
    original_open = Path.open
    reads = []

    def counted_open(path, mode="r", *args, **kwargs):
        if path == binary and mode == "rb":
            reads.append(path)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    original = prepared.bundle_hash(bundle)
    assert prepared.bundle_hash(bundle) == original
    assert len(reads) == 1
    stat = binary.stat()
    binary.write_bytes(b"replaced")
    os.utime(binary, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    replaced = prepared.bundle_hash(bundle)
    assert replaced != original
    assert len(reads) == 2
    (bundle / "added").write_bytes(b"new")
    assert prepared.bundle_hash(bundle) != replaced


PLUGIN_PROBE = """
import json, sys
import numpy as np
sys.path.insert(0, sys.argv[1])
import render, prepared
settings = json.loads(open(sys.argv[2]).read())[sys.argv[3]]
step = {'plugin': sys.argv[3], 'settings': settings, 'target': 'x'}
factory = render.pedalboard.load_plugin
markers = []
def traced_load(*args, **kwargs):
    markers.append(type(kwargs.get('parameter_values')) is prepared._SkipDiscovery)
    return factory(*args, **kwargs)
render.pedalboard.load_plugin = traced_load
audio = np.random.default_rng(400).normal(0, .1, (88200, 2))
normal = render.process(audio, 44100, [step], 'x', object())
restored = render.process(audio, 44100, [step], 'x', object())
assert normal.tobytes() == restored.tobytes(), step['plugin']
assert markers == [False, True], markers
"""


def test_installed_whitelist_in_one_fresh_process_per_plugin():
    if not all((render.PLUGINS / (name + ".vst3")).exists() for name in prepared.WHITELIST):
        pytest.skip("local VST3 bundles are not installed")
    env = dict(os.environ, OPENBLAS_NUM_THREADS="1", VECLIB_MAXIMUM_THREADS="1", OMP_NUM_THREADS="1")
    for name in prepared.WHITELIST:
        subprocess.run(
            [sys.executable, "-c", PLUGIN_PROBE, str(Path(render.__file__).parent),
             str(Path(__file__).with_name("prepared_settings.json")), name],
            check=True, capture_output=True, text=True, env=env, timeout=60,
        )


@pytest.mark.parametrize("name,preset", [
    ("TDR Kotelnikov", "Mastering - Warm"), ("TDR Nova", "1 Band Dyn"), ("TDR VOS SlickEQ", "HF Tilt"),
])
def test_installed_preset_selection_never_uses_prepared_loading(monkeypatch, name, preset):
    import numpy as np

    if not (render.PLUGINS / (name + ".vst3")).exists():
        pytest.skip("local VST3 bundle is not installed")
    monkeypatch.setattr(prepared, "_CACHE", OrderedDict())
    factory = render.pedalboard.load_plugin
    markers = []

    def traced_load(*args, **kwargs):
        markers.append(type(kwargs.get("parameter_values")) is prepared._SkipDiscovery)
        return factory(*args, **kwargs)

    monkeypatch.setattr(render.pedalboard, "load_plugin", traced_load)
    rng = np.random.default_rng(144)
    warm = rng.normal(0, .35, (32768, 2))
    target = rng.normal(0, .06, (17017, 2))
    step = {"plugin": name, "settings": {"program": preset}, "target": "x"}
    with monkeypatch.context() as normal_path:
        normal_path.setattr(prepared, "key", lambda *args: None)
        normal = render.process(target, 44100, [step], "x", object())
        normal2 = render.process(target, 44100, [step], "x", object())
    render.process(warm, 44100, [step], "x", object())
    repeated = render.process(target, 44100, [step], "x", object())
    repeated2 = render.process(target, 44100, [step], "x", object())
    assert normal.tobytes() == normal2.tobytes() == repeated.tobytes() == repeated2.tobytes()
    assert markers == [False] * 5
    assert not prepared._CACHE
