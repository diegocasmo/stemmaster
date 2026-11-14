import sys
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[1] / ".agents" / "skills"
for skill in ("check", "align", "analyze", "render"):
    sys.path.insert(0, str(SKILLS / skill))


@pytest.fixture
def song(tmp_path):
    """A song folder whose stems the test writes from arrays."""
    (tmp_path / "stems").mkdir()
    return tmp_path


@pytest.fixture(autouse=True)
def one_process(monkeypatch):
    """Tests count renders in this process, so the check runs here unless a test asks for workers."""
    monkeypatch.setenv("STEMMASTER_CHECK_WORKERS", "1")


@pytest.fixture(autouse=True)
def measurement_disk(tmp_path_factory, monkeypatch):
    """Each test keeps its readings in a folder of its own, never in the user's cache."""
    import memo

    monkeypatch.setenv("STEMMASTER_MEASURE_MEMO_DIR", str(tmp_path_factory.mktemp("measurements")))
    monkeypatch.setattr(memo.CACHE, "_disk", memo._FROM_ENV)


@pytest.fixture(autouse=True)
def fresh_spill(monkeypatch):
    """A check sets the spill budget and free memory reader for its process, so each test starts from the defaults."""
    import collections

    import spill

    for name, value in {"BUDGET": 0, "FREE": None, "FLOOR": 0.0, "_FREE_READ": [-1.0, False],
                        "_LRU": collections.OrderedDict(), "_HELD": [0], "_PEAK": [0, 0]}.items():
        monkeypatch.setattr(spill, name, value)
