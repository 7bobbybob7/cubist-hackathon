import sys
from importlib import resources
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from framework.db import Database, init_db  # noqa: E402
from framework.state import StatePaths  # noqa: E402


def _copy_agent_templates(paths: StatePaths) -> None:
    """Copy the bundled agent .md templates into the state dir so tests
    that exercise the Phase 5 pod path don't have to bootstrap a full
    framework-state by hand."""
    paths.agents_dir.mkdir(parents=True, exist_ok=True)
    for role in ("methodology", "development", "testing"):
        src = resources.files("framework").joinpath(f"templates/agents/{role}.md")
        dest = paths.agents_dir / f"{role}.md"
        if not dest.exists():
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


@pytest.fixture
def state_dir(tmp_path) -> StatePaths:
    sp = StatePaths(tmp_path / "fw")
    sp.ensure()
    init_db(sp.db)
    _copy_agent_templates(sp)
    return sp


@pytest.fixture
def db(state_dir) -> Database:
    return Database(state_dir.db)
