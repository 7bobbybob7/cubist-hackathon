"""Event emission: writes to SQLite events table AND appends to events.jsonl.

The SQLite row is the queryable mirror; the JSONL file is the durable
append-only log that gets committed alongside other framework state.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from framework.db import Database, utcnow_iso

# Allowed event types per Section 9.4 of the methodology.
EVENT_TYPES = frozenset({
    "task_created",
    "task_before_gate",
    "task_approved_before",
    "task_rejected_before",
    "task_claimed",
    "task_completed",
    "task_after_gate",
    "task_approved_after",
    "task_rejected_after",
    "artifact_submitted",
    "budget_updated",
    "budget_cap_hit",
    "summary_updated",
    "plan_revised",
    "pod_registered",
    "pod_heartbeat",
    # v2 — git worktree lifecycle
    "worktree_created",
    "worktree_shared",
    "worktree_removed",
})

_jsonl_lock = threading.Lock()


def _new_event_id() -> str:
    return f"e_{uuid.uuid4().hex[:12]}"


def emit_event(
    db: Database,
    jsonl_path: str | Path,
    type_: str,
    *,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert an event row and append a line to events.jsonl.

    Returns the event record as a dict.
    """
    if type_ not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {type_}")

    event = {
        "event_id": _new_event_id(),
        "ts": utcnow_iso(),
        "type": type_,
        "task_id": task_id,
        "payload": payload or {},
    }

    db.execute(
        "INSERT INTO events (event_id, ts, type, task_id, payload) "
        "VALUES (:event_id, :ts, :type, :task_id, :payload)",
        {
            "event_id": event["event_id"],
            "ts": event["ts"],
            "type": event["type"],
            "task_id": event["task_id"],
            "payload": json.dumps(event["payload"], sort_keys=True),
        },
    )

    jsonl_path = Path(jsonl_path)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, sort_keys=True)
    with _jsonl_lock:
        with jsonl_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    return event


def record_parent_action(
    db: Database,
    jsonl_path: str | Path,
    *,
    tool: str,
    args: dict[str, Any],
    result: str = "ok",
    caller: str = "parent",
) -> None:
    """Record one framework-tool invocation in SQLite and parent_actions.jsonl."""
    ts = utcnow_iso()
    db.execute(
        "INSERT INTO parent_actions (ts, tool, args, result, caller) "
        "VALUES (?, ?, ?, ?, ?)",
        (ts, tool, json.dumps(args, sort_keys=True), result, caller),
    )

    jsonl_path = Path(jsonl_path)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"ts": ts, "tool": tool, "args": args, "result": result, "caller": caller},
        sort_keys=True,
    )
    with _jsonl_lock:
        with jsonl_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
