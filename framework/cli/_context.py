"""Shared CLI context.

A ``CliContext`` bundles the ``BackendClient`` (for state-mutating calls)
and the ``StatePaths`` (for direct file access where appropriate). Tests
construct one bound to a FastAPI ``TestClient``; production builds one
bound to a real httpx connection.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, TextIO

from framework.pod.backend_client import BackendClient
from framework.state import StatePaths


@dataclass
class CliContext:
    backend: BackendClient
    paths: StatePaths
    stdout: TextIO = sys.stdout
    stderr: TextIO = sys.stderr

    def log_action(
        self, tool: str, args: dict[str, Any] | None = None, *,
        result: str = "ok",
    ) -> None:
        """Record one parent action. Best-effort: never fail the user's
        command because logging failed."""
        try:
            self.backend.record_parent_action(
                tool=tool, args=args or {}, result=result, caller="parent",
            )
        except Exception as e:  # pragma: no cover — defensive
            self.stderr.write(f"warning: parent_action log failed: {e}\n")
