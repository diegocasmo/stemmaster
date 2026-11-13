import hashlib
import json
import platform
import sys
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

import numpy as np
import pedalboard
import pedalboard._pedalboard
import pedalboard_native

WHITELIST = {
    "TDR Kotelnikov": "1adbbd38e40c0e6a5c68fe252b3d93415be26f4b5d34f8e61318408fbe80dadd",
    "ZL Equalizer 2": "76ef9726b7f4fc984f4f37a54fd5518136e2b7bec0f64562387b1ff07d1fcb06",
    "ZL Limiter": "ba4bcfd2fc2add1babc505c0f7394046f3b65b5b217247a7e933bc1dd3eb8eb8",
    "ZaMultiCompX2": "d5d5f1c4b03944b4c0450fa56de357792246cd1d8d2a9182c3b8c202c9a33d3d",
    "ZL Compressor": "77c6507314372908181a64f10a232cf2714870b0437acbf669373c0473838946",
    "TDR Nova": "348b789f96792456e120b8885a6a5df27011d64a6c66feeb0b29cac947469dc8",
    "TDR Molotok": "0054b5fecb3351f141c50cf635591ae99233a1f1ad332590d9ac22fc8c195658",
    "TDR VOS SlickEQ": "34f622dd13c31c127e671f636bba42140705bf1d9472d1210c3117532c352638",
}
NORMAL_SETUP = pedalboard.VST3Plugin.__set_initial_parameter_values__
MAX_ENTRIES = 128
_CACHE = OrderedDict()
_REJECTED = OrderedDict()


class _SkipDiscovery:
    pass


def _setup(plugin, values):
    # The native constructor casts to the base class before calling this hook, so a subclass cannot bypass discovery.
    if type(values) is not _SkipDiscovery:
        NORMAL_SETUP(plugin, values)


@lru_cache(maxsize=32)
def _file_hash(path, stat):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _stat(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _dependency_hash(path):
    path = Path(path)
    return _file_hash(path, _stat(path))


def runtime():
    return {
        "python": platform.python_version(),
        "pedalboard": pedalboard.__version__,
        "numpy": np.__version__,
        "platform": sys.platform,
        "machine": platform.machine(),
        "macos": platform.mac_ver()[0],
        "wrapper_sha256": _dependency_hash(pedalboard._pedalboard.__file__),
        "native_sha256": _dependency_hash(pedalboard_native.__file__),
    }


@lru_cache(maxsize=16)
def _bundle_hash(bundle, files):
    digest = hashlib.sha256()
    for relative, stat in files:
        encoded = relative.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(stat[2].to_bytes(8, "big"))
        with (bundle / relative).open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
    return digest.hexdigest()


def bundle_hash(bundle):
    files = tuple((path.relative_to(bundle).as_posix(), _stat(path))
                  for path in sorted(bundle.rglob("*")) if path.is_file())
    return _bundle_hash(bundle, files)


def _settings_key(settings):
    return json.dumps(list(settings.items()), separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _values(plugin):
    return tuple((parameter.name, parameter.raw_value) for parameter in plugin._parameters)


def _preset_selector(name):
    return any(word in name.casefold() for word in ("program", "preset", "bank"))


def key(bundle, settings, rate, channels):
    try:
        if bundle.stem not in WHITELIST or type(rate) is not int or rate <= 0 or channels != 2:
            return None
        if pedalboard.__version__ != "0.9.26":
            return None
        # Preset selection carries history that native control values cannot reproduce.
        if any(_preset_selector(name) for name in settings):
            return None
        identity = bundle_hash(bundle)
        if identity != WHITELIST[bundle.stem]:
            return None
        return (identity, json.dumps(runtime(), sort_keys=True), _settings_key(settings), rate, channels)
    except Exception:
        return None


def load(bundle, key, factory):
    if key is None:
        return None
    try:
        hook = pedalboard.VST3Plugin.__set_initial_parameter_values__
        if hook not in (NORMAL_SETUP, _setup):
            raise ValueError("plugin setup hook changed")
        values = _CACHE.get(key)
        if values is None:
            return None
        if hook is NORMAL_SETUP:
            pedalboard.VST3Plugin.__set_initial_parameter_values__ = _setup
        _CACHE.move_to_end(key)
        plugin = factory(bundle, parameter_values=_SkipDiscovery())
        parameters = plugin._parameters
        if tuple(parameter.name for parameter in parameters) != tuple(name for name, _ in values):
            raise ValueError("prepared parameter layout changed")
        for parameter, (_, value) in zip(parameters, values):
            if parameter.raw_value != value:
                if _preset_selector(parameter.name):
                    raise ValueError("prepared load would select a preset")
                parameter.raw_value = value
        if _values(plugin) != values:
            raise ValueError("prepared state changed parameter values")
        return plugin
    except Exception:
        _CACHE.pop(key, None)
        _REJECTED[key] = None
        while len(_REJECTED) > MAX_ENTRIES:
            _REJECTED.popitem(last=False)
        return None


def remember(plugin, key):
    try:
        if key is None or key in _REJECTED:
            return
        _CACHE[key] = _values(plugin)
        _CACHE.move_to_end(key)
        while len(_CACHE) > MAX_ENTRIES:
            _CACHE.popitem(last=False)
    except Exception:
        pass
