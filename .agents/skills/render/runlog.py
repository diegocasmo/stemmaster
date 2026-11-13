"""One line per render or check run in the song's renders/timings.jsonl, so the next speed-up can target the largest share."""
import contextlib
import datetime
import json
import resource
import signal
import sys
import time
from pathlib import Path


def peak_mb(who):
    peak = resource.getrusage(who).ru_maxrss
    return round((peak if sys.platform == "darwin" else peak * 1024) / 2**20, 1)


def log_path(recipe_path):
    """The song's log; a cut made by check/cut.py logs into the song it was cut from."""
    # The supplied path's folder, not a symlinked recipe's: a cut may link the song's recipe yet render its own stems.
    song = Path(recipe_path).parent.resolve()
    if song.parent.name == "cuts" and song.parent.parent.name == "renders":
        return song.parent.parent / "timings.jsonl", song.name
    return song / "renders" / "timings.jsonl", None


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def terminate(signum, frame):
    # SIGTERM would end the process without unwinding; as an exit, the run still gets its line and the check's
    # cleanup (stopping its workers) still runs.
    sys.exit(128 + signum)


@contextlib.contextmanager
def logged(tool, recipe_path, details):
    """Logs the run after it ends; `details()` gives its scope and cache counts. Bookkeeping only: it never fails the run."""
    signal.signal(signal.SIGTERM, terminate)
    start, clock, code = now(), time.monotonic(), 0
    try:
        yield
    except SystemExit as exit:
        code = exit.code if isinstance(exit.code, int) else 0 if exit.code is None else 1
        raise
    except KeyboardInterrupt:
        code = 130
        raise
    except BaseException:
        code = 1
        raise
    finally:
        try:
            if recipe_path and (Path(recipe_path).parent / "stems").is_dir():
                path, cut = log_path(recipe_path)
                line = {"tool": tool, "start": start, "end": now(), "wall_s": round(time.monotonic() - clock, 1),
                        "peak_rss_mb": peak_mb(resource.RUSAGE_SELF), "peak_child_rss_mb": peak_mb(resource.RUSAGE_CHILDREN),
                        **({"cut": cut} if cut else {}), **details(), "exit": code}
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a") as file:
                    file.write(json.dumps(line) + "\n")
        except Exception as error:
            print(f"warning: run not logged: {error}", file=sys.stderr)
