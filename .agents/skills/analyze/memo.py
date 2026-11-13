import functools
import hashlib
import inspect
import os
import pickle
import platform
import sys
from collections import OrderedDict
from contextvars import ContextVar
from importlib import metadata
from pathlib import Path

import numpy as np

_ACTIVE = ContextVar("measurement_memo_active", default=False)


def _hash(digest, value):
    if isinstance(value, np.ndarray):
        digest.update(pickle.dumps(("array", value.shape, value.dtype, value.strides)))
        if value.size:
            digest.update(memoryview(np.ascontiguousarray(value)).cast("B"))
    elif isinstance(value, dict):
        digest.update(pickle.dumps(("dict", len(value))))
        for key, item in value.items():
            _hash(digest, key)
            _hash(digest, item)
    elif isinstance(value, (list, tuple)):
        digest.update(pickle.dumps((type(value).__name__, len(value))))
        for item in value:
            _hash(digest, item)
    else:
        digest.update(pickle.dumps(value))


SKILLS = Path(__file__).resolve().parents[1]
DISK_MB = 512


def code_tag():
    """Every module a reading can run, and the libraries and system under them: a stored result outlives the process."""
    digest = hashlib.sha256()
    for path in sorted(p for skill in ("analyze", "check", "render", "align") for p in (SKILLS / skill).glob("*.py")):
        digest.update(f"{path.parent.name}/{path.name}".encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    versions = [sys.version, platform.platform(), os.uname().version,
                *(metadata.version(name) for name in ("numpy", "scipy", "pyloudnorm", "pedalboard"))]
    digest.update(pickle.dumps(versions))
    return digest.digest()


class Disk:
    """Readings kept on disk across runs, least recently used dropped past the byte cap. Any read error is a miss."""

    def __init__(self, folder, cap_bytes):
        self.folder, self.cap = Path(folder), cap_bytes
        self.folder.mkdir(parents=True, exist_ok=True)
        self.tag = code_tag()
        self.written = 0

    @classmethod
    def from_env(cls):
        try:
            megabytes = int(os.environ.get("STEMMASTER_MEASURE_MEMO_MB", DISK_MB))
            if megabytes <= 0:
                return None
            folder = os.environ.get("STEMMASTER_MEASURE_MEMO_DIR") or \
                Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache") / "stemmaster" / "measurements"
            return cls(folder, megabytes << 20)
        except Exception as error:
            print(f"warning: measurement memo on disk off: {error}", file=sys.stderr)
            return None

    def path(self, key):
        return self.folder / f"{hashlib.sha256(self.tag + key).hexdigest()}.pkl"

    def get(self, key):
        try:
            path = self.path(key)
            data = path.read_bytes()
            payload = data[32:]
            # A torn or damaged file is a miss, never a wrong reading.
            if len(data) < 32 or hashlib.sha256(payload).digest() != data[:32]:
                return None
            os.utime(path)
            return payload
        except Exception:
            return None

    def put(self, key, payload):
        try:
            path = self.path(key)
            temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            temp.write_bytes(hashlib.sha256(payload).digest() + payload)
            os.replace(temp, path)
            self.written += len(payload) + 32
            if self.written > self.cap // 8:
                self.evict()
        except Exception:
            pass

    def evict(self):
        self.written = 0
        files = []
        for entry in os.scandir(self.folder):
            try:
                if entry.name.endswith(".pkl") and entry.is_file(follow_symlinks=False):
                    stat = entry.stat()
                    files.append((stat.st_mtime_ns, stat.st_size, entry.path))
            except OSError:
                pass
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):
            if total <= self.cap:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                pass


_FROM_ENV = object()


class MeasurementMemo:

    def __init__(self, max_bytes=32 << 20, max_entries=1024, max_result_bytes=1 << 20, disk=None):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.max_result_bytes = max_result_bytes
        self._disk = disk
        self.clear()

    @property
    def disk(self):
        # Made on first use, so the folder and cap come from the environment the check runs in.
        if self._disk is _FROM_ENV:
            self._disk = Disk.from_env()
        return self._disk

    def clear(self):
        self.entries = OrderedDict()
        self.size = 0

    def get(self, key, compute):
        if self.max_bytes <= 0 or self.max_entries <= 0:
            return compute()
        if key in self.entries:
            payload = self.entries.pop(key)
        elif (payload := self.disk and self.disk.get(key)) is not None:
            self.size += len(key) + len(payload)
        else:
            result = compute()
            payload = pickle.dumps(result, protocol=5)
            if len(payload) > self.max_result_bytes or len(key) + len(payload) > self.max_bytes:
                return result
            if self.disk:
                self.disk.put(key, payload)
            self.size += len(key) + len(payload)
        while self.entries and (self.size > self.max_bytes or len(self.entries) >= self.max_entries):
            old_key, old_payload = self.entries.popitem(last=False)
            self.size -= len(old_key) + len(old_payload)
        # Serialized results own their bytes and cannot retain a view of the input audio.
        self.entries[key] = payload
        return pickle.loads(payload)


CACHE = MeasurementMemo(disk=_FROM_ENV)


def memoized(function=None, *, context=None):
    if function is None:
        return lambda function: memoized(function, context=context)
    signature = inspect.signature(function)
    identity = (function.__module__, function.__qualname__)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        if _ACTIVE.get() or CACHE.max_bytes <= 0 or CACHE.max_entries <= 0:
            return function(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        digest = hashlib.sha256()
        _hash(digest, identity)
        _hash(digest, bound.arguments)
        if context is not None:
            _hash(digest, context(bound.arguments))

        def compute():
            # The outer reading owns reuse; its helpers must not hash the same signal again.
            token = _ACTIVE.set(True)
            try:
                return function(*args, **kwargs)
            finally:
                _ACTIVE.reset(token)

        return CACHE.get(digest.digest(), compute)

    return wrapped
