"""Framework-state directory layout helpers.

Centralizes paths so the rest of the code never builds path strings ad-hoc.
"""
from __future__ import annotations

from pathlib import Path


class StatePaths:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    @property
    def db(self) -> Path:
        return self.root / "framework.db"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def events_jsonl(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def parent_actions_jsonl(self) -> Path:
        return self.logs_dir / "parent_actions.jsonl"

    @property
    def budget_ledger_jsonl(self) -> Path:
        return self.logs_dir / "budget_ledger.jsonl"

    @property
    def rolling_summary(self) -> Path:
        return self.root / "rolling_summary.md"

    @property
    def progress_md(self) -> Path:
        return self.root / "progress.md"

    @property
    def parent_claude_md(self) -> Path:
        return self.root / "CLAUDE.md"

    @property
    def config_yaml(self) -> Path:
        return self.root / "config.yaml"

    @property
    def agents_dir(self) -> Path:
        return self.root / "agents"

    @property
    def plan_dir(self) -> Path:
        return self.root / "plan"

    def ensure(self) -> None:
        """Create the minimum directory layout needed by Phase 1."""
        self.root.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.root / "artifacts").mkdir(parents=True, exist_ok=True)
        (self.root / "agents").mkdir(parents=True, exist_ok=True)
        (self.root / "plan").mkdir(parents=True, exist_ok=True)
