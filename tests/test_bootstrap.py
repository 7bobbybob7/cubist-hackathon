"""Phase 3: framework run start bootstrap."""
import yaml

import pytest

from framework.bootstrap import bootstrap_run
from framework.state import StatePaths


def test_bootstrap_creates_full_layout(tmp_path):
    target = tmp_path / "fake-repo"
    target.mkdir()
    state = tmp_path / "fw"

    info = bootstrap_run(state, goal="Implement UCI", target_repo=str(target))

    paths = StatePaths(state)
    assert paths.db.exists()
    assert paths.config_yaml.exists()
    assert paths.parent_claude_md.exists()
    assert (paths.agents_dir / "methodology.md").exists()
    assert (paths.agents_dir / "development.md").exists()
    assert (paths.agents_dir / "testing.md").exists()
    assert paths.rolling_summary.exists()
    assert paths.progress_md.exists()
    assert (paths.root / "run.yaml").exists()

    # Parent CLAUDE.md mentions both gates
    md = paths.parent_claude_md.read_text()
    assert "before gate" in md.lower()
    assert "after gate" in md.lower()
    assert "rolling_summary" in md.lower()

    # config.yaml round-trips
    cfg = yaml.safe_load(paths.config_yaml.read_text())
    assert cfg["models"]["sonnet"] == "claude-sonnet-4-6"

    # run.yaml records the goal + target_repo + branch_name
    run = yaml.safe_load((paths.root / "run.yaml").read_text())
    assert run["goal"] == "Implement UCI"
    assert run["target_repo"] == str(target.resolve())
    assert run["branch_name"].startswith("framework/")

    # Rolling summary contains the goal
    assert "Implement UCI" in paths.rolling_summary.read_text()

    assert info["run_id"] == run["run_id"]
    assert "agents/methodology.md" in info["files_created"]


def test_bootstrap_refuses_existing_dir_without_overwrite(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    state = tmp_path / "fw"
    state.mkdir()
    (state / "stale_file").write_text("x")
    with pytest.raises(FileExistsError):
        bootstrap_run(state, goal="g", target_repo=str(target))


def test_bootstrap_overwrite_wipes_old(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    state = tmp_path / "fw"
    state.mkdir()
    (state / "stale").write_text("x")
    bootstrap_run(state, goal="g", target_repo=str(target), overwrite=True)
    assert not (state / "stale").exists()


def test_bootstrap_validates_target_repo(tmp_path):
    with pytest.raises(FileNotFoundError):
        bootstrap_run(tmp_path / "fw", goal="g", target_repo=str(tmp_path / "nope"))
