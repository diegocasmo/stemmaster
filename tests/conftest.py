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
