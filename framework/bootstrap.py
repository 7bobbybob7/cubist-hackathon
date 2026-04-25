"""`framework run start` bootstrap.

Per Section 11: create framework-state/, init SQLite, write config.yaml,
copy parent CLAUDE.md and default agent .md files, validate target repo
path. (Cloning a remote target repo is left to the user for v1.)
"""
from __future__ import annotations

import shutil
import time
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from framework.config import write_default_config
from framework.db import init_db
from framework.state import StatePaths
from framework.worktree import WorktreeError, ensure_branch, is_git_repo


def _new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _copy_template(resource_path: str, dest: Path) -> None:
    src = resources.files("framework").joinpath(resource_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def bootstrap_run(
    state_dir: str | Path,
    *,
    goal: str,
    target_repo: str,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Bootstrap a fresh framework-state/ directory.

    Returns a dict describing what was created so the parent can surface
    it back to the user.
    """
    target = Path(target_repo).expanduser().resolve()
    if not target.exists():
        raise FileNotFoundError(
            f"target_repo {target!s} does not exist. v1 does not clone remote "
            "repos — supply a path to a local clone."
        )
    if not target.is_dir():
        raise NotADirectoryError(f"target_repo {target!s} is not a directory")

    paths = StatePaths(state_dir)
    if paths.root.exists() and any(paths.root.iterdir()):
        if not overwrite:
            raise FileExistsError(
                f"{paths.root} is not empty; pass overwrite=True to wipe it"
            )
        shutil.rmtree(paths.root)

    paths.ensure()
    init_db(paths.db)
    write_default_config(paths.config_yaml)

    # Copy templates
    _copy_template("templates/CLAUDE.md", paths.parent_claude_md)
    for role in ("methodology", "development", "testing"):
        _copy_template(
            f"templates/agents/{role}.md",
            paths.agents_dir / f"{role}.md",
        )

    # Initialize the rolling summary with the goal
    run_id = _new_run_id()
    summary = (
        "## Goal\n"
        f"{goal}\n\n"
        "## Completed milestones\n"
        "- (run started; no milestones yet)\n\n"
        "## Open threads\n"
        "- (none yet)\n\n"
        "## Key decisions\n"
        f"- {time.strftime('%Y-%m-%d')}: framework run started against `{target}`\n\n"
        "## Referenceable artifact IDs\n"
        "- (none yet)\n"
    )
    paths.rolling_summary.write_text(summary, encoding="utf-8")

    branch_name = f"framework/{run_id}"

    # Per-task worktrees (v2) need a real branch in the target repo to
    # fork from. Create it now if the target is a git repo. Non-git
    # targets are still allowed — pods will just edit files directly,
    # without worktree isolation. (Methodology agent decides.)
    target_is_git = is_git_repo(target)
    if target_is_git:
        try:
            ensure_branch(target, branch_name)
        except WorktreeError as e:
            # Don't abort bootstrap on a git failure — surface the issue
            # and let the user retry with --overwrite or a clean repo.
            # Pods that try to use worktrees will fail with a clear
            # error at approve_before time.
            raise WorktreeError(
                f"failed to create framework branch in {target}: {e}\n"
                "Either commit/stash uncommitted state, switch to a "
                "clean HEAD, or pass a different target_repo."
            ) from e

    # Write a run-meta YAML so the parent can read goal + target_repo +
    # run_id without parsing config.yaml or guessing.
    meta = {
        "run_id": run_id,
        "goal": goal,
        "target_repo": str(target),
        "branch_name": branch_name,
        "target_is_git": target_is_git,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (paths.root / "run.yaml").write_text(
        yaml.safe_dump(meta, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )

    # Empty progress.md
    paths.progress_md.write_text(
        f"# Progress log — run {run_id}\n\n"
        f"_Run goal_: {goal}\n"
        f"_Target repo_: {target}\n\n"
        "(append milestones below as the run proceeds)\n",
        encoding="utf-8",
    )

    return {
        "run_id": run_id,
        "state_dir": str(paths.root),
        "db": str(paths.db),
        "target_repo": str(target),
        "branch_name": f"framework/{run_id}",
        "goal": goal,
        "files_created": [
            str(paths.parent_claude_md.relative_to(paths.root)),
            str(paths.config_yaml.relative_to(paths.root)),
            str(paths.rolling_summary.relative_to(paths.root)),
            str((paths.root / "run.yaml").relative_to(paths.root)),
            str(paths.progress_md.relative_to(paths.root)),
            *(f"agents/{r}.md" for r in ("methodology", "development", "testing")),
        ],
    }
