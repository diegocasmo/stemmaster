"""Worker processes that fill the check's caches before it runs.

Each worker evaluates a disjoint part of the contracts and published batteries with the check's own code, and writes
the same contract and battery files a cold check writes. The check then runs as one process and reads them, so its
output is the output of a warm check, which is byte-identical to a cold one. A part a worker didn't finish is a cache
miss the check computes itself.
"""
import concurrent.futures as futures
import json
import math
import multiprocessing
import os
import re
import resource
import subprocess
import sys
import time

ENV = "STEMMASTER_CHECK_WORKERS"
# Kept free for the rest of the machine while workers run.
FREE_FLOOR = 0.30
POLL_S = 1.0
# A worker's peak is its interpreter and plugins plus the render it is making: a render's taps, mix and trial master
# wait on disk, a few at a time in memory. Synthetic songs measured 0.76 tapped signals per worker (2.84 when every tap
# was held, against song 000's 2.28); the default keeps a margin over that.
BASE_BYTES = 600 << 20
TAP_SIGNALS = 1.0
PEAKS = "workers.json"
# Named for the code that measured it: a peak measured while every tap was held says nothing about this code's.
FACTOR = "per_tapped_signal_spilled"
_STATE = {}


def total_bytes():
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")


def free_fraction():
    """The system's free memory as a fraction of all of it, as macOS memory_pressure reports it; None if unknown."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["/usr/bin/memory_pressure", "-Q"], capture_output=True, text=True, timeout=2).stdout
            return int(re.search(r"free percentage: (\d+)%", out)[1]) / 100
        with open("/proc/meminfo") as file:
            info = dict(line.split(":", 1) for line in file)
        return int(info["MemAvailable"].split()[0]) * 1024 / total_bytes()
    except Exception:
        return None


def peak_bytes():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def recorded(cache):
    try:
        return float(json.loads((cache / PEAKS).read_text())[FACTOR])
    except Exception:
        return 0.0


def estimate(cache, signal_bytes, taps):
    """A worker's peak memory. A measured peak only raises the default: a small song's peak says little about a long one."""
    return BASE_BYTES + max(TAP_SIGNALS, recorded(cache)) * signal_bytes * taps


def record(cache, signal_bytes, taps, peaks):
    """Keeps this song's largest worker peak per tapped signal. Bookkeeping only: it never fails the check."""
    import evidence

    try:
        if peaks and signal_bytes * taps:
            per_signal = max(recorded(cache), (max(peaks) - BASE_BYTES) / (signal_bytes * taps))
            evidence.write_atomic(cache / PEAKS, json.dumps({FACTOR: per_signal, "peak_bytes": max(peaks)}))
    except Exception as error:
        evidence.progress(f"workers: peak not recorded: {error}")


def count(parts, need, free=None, total=None, cpus=None):
    """How many workers: the env override, else as many as cores, parts and memory above the free floor allow."""
    override = os.environ.get(ENV)
    if override is not None:
        try:
            wanted = int(override)
        except ValueError:
            sys.exit(f"{ENV} must be a whole number of worker processes (1 runs the check in one process), not {override!r}")
        return max(1, min(wanted, parts))
    free = free_fraction() if free is None else free
    if free is None:
        return 1
    spare = (free - FREE_FLOOR) * (total or total_bytes())
    return max(1, min(cpus or os.process_cpu_count() or 1, parts, math.floor(spare / need)))


def parts(recipe, variants, ctx, master_mode):
    """Uncached contracts and published batteries, grouped so each variant they render is in one part."""
    import evidence

    records, entries = evidence.contract_entries(recipe, variants, ctx, master_mode)
    groups = {}
    for entry in entries:
        if evidence.contract_path(recipe, variants, ctx, master_mode, records, entry).exists():
            continue
        record, keys = entry["record"], entry["keys"]
        # A kept record's off render, or a dropped one's trial; every part renders the current recipe anyway.
        group = next((k for k in (keys.get("without"), keys.get("with")) if k not in (None, "current")),
                     (record["move"], record["source"]))
        groups.setdefault(group, (set(), set()))[0].add((record["move"], record["source"]))
    for key in sorted(variants.published):
        if key in variants.recipes and not variants.battery_path(key).exists():
            groups.setdefault(key, (set(), set()))[1].add(key)
    # Largest first, so the last parts to finish are short ones.
    return sorted(((sorted(c), sorted(p)) for c, p in groups.values()), key=lambda part: -len(part[0]) - len(part[1]))


def prewarm(recipe, song, nodes, master_mode, planned, outside, variants, ctx):
    import evidence
    import render

    todo = parts(recipe, variants, ctx, master_mode)
    if len(todo) < 2:
        return
    taps = len(({tap for tap, _ in variants.layout[0].values()} | variants.taps) & set(render.sources(recipe)))
    signal_bytes = ctx.length * 2 * 8
    need = estimate(variants.cache, signal_bytes, taps)
    n = count(len(todo), need)
    if n < 2:
        return
    evidence.progress(f"up to {n} workers for {len(todo)} parts, each estimated at {int(need) >> 20} MB")
    peaks, pool = [], None
    # The machinery is best effort: whatever goes wrong here, the check computes every missing part itself.
    try:
        pool = futures.ProcessPoolExecutor(n, mp_context=multiprocessing.get_context("spawn"), initializer=_start,
                                           initargs=(recipe, song, nodes, master_mode, planned, outside))
        waiting, running, names, done = list(todo), set(), {}, 0
        while waiting or running:
            # One more worker per poll, and only while free memory has room for it, so starting them is no burst.
            free = free_fraction()
            if free is None or free < FREE_FLOOR:
                evidence.progress("workers stopped: free memory " + ("unknown" if free is None else f"{free:.0%} is under {FREE_FLOOR:.0%}"))
                return
            if waiting and len(running) < n and (not running or (free - FREE_FLOOR) * total_bytes() >= need):
                contracts, published = waiting.pop(0)
                future = pool.submit(_run, contracts, published)
                running.add(future)
                names[future] = (contracts or published)[0]
            finished, running = futures.wait(running, timeout=POLL_S, return_when=futures.FIRST_COMPLETED)
            for future in finished:
                if future.exception() is not None:
                    # The check meets the same error itself, in its own order, when it computes this part.
                    evidence.progress(f"workers stopped: {type(future.exception()).__name__}: {future.exception()}")
                    return
                peak, seconds = future.result()
                peaks.append(peak)
                done += 1
                evidence.progress(f"workers: {done}/{len(todo)} parts ({names[future]}... {seconds:.0f} s), worker peak {peak >> 20} MB")
                if waiting:
                    # A finished part frees its worker for the next one at once.
                    contracts, published = waiting.pop(0)
                    future = pool.submit(_run, contracts, published)
                    running.add(future)
                    names[future] = (contracts or published)[0]
    except Exception as error:
        evidence.progress(f"workers stopped: {type(error).__name__}: {error}")
    finally:
        if pool is not None:
            try:
                pool.kill_workers()
                pool.shutdown(wait=True, cancel_futures=True)
            except Exception as error:
                evidence.progress(f"workers: shutdown failed: {error}")
        record(variants.cache, signal_bytes, taps, peaks)


def _start(recipe, song, nodes, master_mode, planned, outside):
    import evidence

    # Only the check prints its report, fails and progress; a worker's error reaches it through its part's result.
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    evidence.progress = lambda message: None
    # A stem, plugin, doc or code file changed since the check hashed them would be cached under the wrong key, so
    # such a worker refuses every part, and the refusal reaches the check as the part's error.
    if evidence.inputs(song, recipe) != outside:
        _STATE["refused"] = "the song's stems, plugins, docs or check code changed after the check hashed them"
        return
    variants, ctx, _ = evidence.prepare(recipe, song, nodes, planned, outside)
    evidence.contract_entries(recipe, variants, ctx, master_mode)
    _STATE.update(recipe=recipe, song=song, nodes=nodes, master_mode=master_mode, variants=variants, ctx=ctx)


def _run(contracts, published):
    import evidence

    state, started = _STATE, time.monotonic()
    if "refused" in state:
        raise RuntimeError(state["refused"])
    variants = state["variants"]
    evidence.contract_report(state["recipe"], state["song"], state["nodes"], variants, state["ctx"], state["master_mode"],
                             only={tuple(c) for c in contracts})
    for key in published:
        variants.battery(key)
    # Only the current render is shared between parts; the rest would hold memory the next part needs.
    for key in [k for k in variants.views if k != "current"]:
        variants.drop(key)
    return peak_bytes(), time.monotonic() - started
