import functools
import hashlib
import inspect
import pickle
from collections import OrderedDict
from contextvars import ContextVar

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


class MeasurementMemo:

    def __init__(self, max_bytes=32 << 20, max_entries=1024, max_result_bytes=1 << 20):
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.max_result_bytes = max_result_bytes
        self.clear()

    def clear(self):
        self.entries = OrderedDict()
        self.size = 0

    def get(self, key, compute):
        if self.max_bytes <= 0 or self.max_entries <= 0:
            return compute()
        if key in self.entries:
            payload = self.entries.pop(key)
        else:
            result = compute()
            payload = pickle.dumps(result, protocol=5)
            if len(payload) > self.max_result_bytes or len(key) + len(payload) > self.max_bytes:
                return result
            while self.entries and (self.size + len(key) + len(payload) > self.max_bytes
                                    or len(self.entries) >= self.max_entries):
                old_key, old_payload = self.entries.popitem(last=False)
                self.size -= len(old_key) + len(old_payload)
            self.size += len(key) + len(payload)
        # Serialized results own their bytes and cannot retain a view of the input audio.
        self.entries[key] = payload
        return pickle.loads(payload)


CACHE = MeasurementMemo()


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
