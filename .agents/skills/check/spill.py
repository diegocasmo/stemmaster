"""Tapped signals kept in an unlinked temp file and read back on each use, so a render's taps don't all sit in memory."""
import collections
import itertools
import os
import tempfile
import time
import weakref

import numpy as np

# Taps stay in memory while their bytes fit the budget, or while there are no more than RESIDENT of them; past both,
# the least recently used go to disk. The check sets the budget from free memory; 0 keeps only RESIDENT taps.
RESIDENT = 4
BUDGET = 0
# The safety net, set by the check: free memory as a fraction (None if unknown), read at most once a second. Under
# FLOOR, or unknown, taps go to disk whatever the budget, so a run that free memory shrinks under keeps going.
FREE, FLOOR = None, 0.0
_FREE_READ = [-1.0, False]
_LRU = collections.OrderedDict()
_HELD = [0]
_SERIAL = itertools.count()


def _touch(store, name, nbytes):
    key = (store.serial, name)
    if key in _LRU:
        _HELD[0] -= _LRU[key][1]
    _LRU[key] = weakref.ref(store), nbytes
    _HELD[0] += nbytes
    _LRU.move_to_end(key)
    limit = 0 if _short() else BUDGET
    while len(_LRU) > RESIDENT and _HELD[0] > limit:
        (_, old), (ref, size) = _LRU.popitem(last=False)
        _HELD[0] -= size
        owner = ref()
        if owner is not None:
            owner._spill(old)


def _short():
    if FREE is None:
        return False
    if time.monotonic() - _FREE_READ[0] >= 1:
        free = FREE()
        _FREE_READ[:] = [time.monotonic(), free is None or free < FLOOR]
    return _FREE_READ[1]


def _forget(serial):
    for key in [key for key in _LRU if key[0] == serial]:
        _HELD[0] -= _LRU.pop(key)[1]


class Taps:
    """A render's taps by name, as the dict it replaces. Each read returns the same bytes, shape, dtype and memory order
    as the array stored: a sum's last bits can depend on the order, so only C or Fortran contiguous arrays go to disk."""

    def __init__(self):
        self.serial = next(_SERIAL)
        # A dropped render's taps leave the count at once, so they never stand in for a live render's.
        weakref.finalize(self, _forget, self.serial)
        self.resident, self.spilled, self.names = {}, {}, {}
        self.file = None
        self.end = 0

    def __setitem__(self, name, audio):
        self.spilled.pop(name, None)
        self.names[name] = None
        self.resident[name] = audio
        if audio.flags.c_contiguous or audio.flags.f_contiguous:
            _touch(self, name, audio.nbytes)

    def _spill(self, name):
        audio = self.resident.get(name)
        if audio is None:
            return
        if name in self.spilled:
            del self.resident[name]
            return
        order = "C" if audio.flags.c_contiguous else "F"
        raw = memoryview(audio if order == "C" else audio.T).cast("B")
        try:
            if self.file is None:
                # Unlinked at once, so a killed worker leaves nothing behind: the space goes with the descriptor.
                self.file = tempfile.TemporaryFile(prefix="stemmaster-taps-", buffering=0)
            written = 0
            while written < len(raw):
                written += os.pwrite(self.file.fileno(), raw[written:], self.end + written)
        except OSError:
            # A full or unwritable temp folder costs memory, never the check.
            return
        self.spilled[name] = (self.end, audio.shape, audio.dtype, order)
        self.end += len(raw)
        del self.resident[name]

    def _load(self, name):
        offset, shape, dtype, order = self.spilled[name]
        audio = np.empty(shape, dtype, order=order)
        raw = memoryview(audio if order == "C" else audio.T).cast("B")
        read = 0
        while read < len(raw):
            got = os.preadv(self.file.fileno(), [raw[read:]], offset + read)
            if got <= 0:
                raise OSError(f"tap {name} cut short in its spill file")
            read += got
        return audio

    def __getitem__(self, name):
        if name in self.resident:
            audio = self.resident[name]
            if (self.serial, name) in _LRU:
                _LRU.move_to_end((self.serial, name))
            return audio
        if name not in self.spilled:
            raise KeyError(name)
        # Held again until it is the least recently used, so a run of reads of one tap reads the file once.
        audio = self._load(name)
        self.resident[name] = audio
        _touch(self, name, audio.nbytes)
        return audio

    def get(self, name, default=None):
        return self[name] if name in self else default

    def __contains__(self, name):
        return name in self.names

    def __iter__(self):
        return iter(list(self.names))

    def __len__(self):
        return len(self.names)

    def keys(self):
        return list(self)

    def values(self):
        return [self[name] for name in self]

    def items(self):
        return [(name, self[name]) for name in self]
