"""Tapped signals kept in an unlinked temp file and read back on each use, so a render's taps don't all sit in memory."""
import collections
import itertools
import os
import tempfile
import weakref

import numpy as np

# Taps held in memory at once across every store in the process, least recently used spilled first.
RESIDENT = 4
_LRU = collections.OrderedDict()
_SERIAL = itertools.count()


def _touch(store, name):
    key = (store.serial, name)
    _LRU[key] = weakref.ref(store)
    _LRU.move_to_end(key)
    while len(_LRU) > RESIDENT:
        (_, old), ref = _LRU.popitem(last=False)
        owner = ref()
        if owner is not None:
            owner._spill(old)


def _forget(serial):
    for key in [key for key in _LRU if key[0] == serial]:
        del _LRU[key]


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
            _touch(self, name)

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
        _touch(self, name)
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
