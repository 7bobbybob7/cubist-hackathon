"""The single source of truth for task claiming.

Architectural Invariant #7: all claim logic goes through one function.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from framework.db import Database, utcnow_iso
from framework.events import emit_event


def _utc_day(ts: str) -> str:
    """Extract YYYY-MM-DD from an ISO timestamp."""
    return ts[:10]


def _today_spend_usd(db: Database) -> float:
    today = _utc_day(utcnow_iso())
    row = db.query_one(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total "
        "FROM budget_ledger WHERE substr(ts, 1, 10) = ?",
        (today,),
    )
    return float(row["total"]) if row else 0.0


def budget_cap_hit_today(db: Database) -> bool:
    """Has a ``budget_cap_hit`` event already fired in the current UTC day?"""
    today = _utc_day(utcnow_iso())
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM events "
        "WHERE type = 'budget_cap_hit' AND substr(ts, 1, 10) = ?",
        (today,),
    )
    return bool(row and row["n"] > 0)


def claim_next_task(
    db: Database,
    pod_id: str,
    events_jsonl_path: str | Path,
    *,
    daily_cap_usd: float | None = None,
) -> dict[str, Any] | None:
    """Atomically claim the highest-priority ready task for ``pod_id``.

    If ``daily_cap_usd`` is set and today's cumulative spend has reached
    or exceeded it, no task is claimed and a ``budget_cap_hit`` event is
    emitted exactly once per UTC day. The next call within the same day
    silently returns ``None`` so the loop doesn't spam the event stream.

    Uses ``BEGIN IMMEDIATE`` so racing pods see one another's writes.
    """
    if daily_cap_usd is not None and daily_cap_usd > 0:
        spent = _today_spend_usd(db)
        if spent >= daily_cap_usd:
            if not budget_cap_hit_today(db):
                emit_event(
                    db, events_jsonl_path, "budget_cap_hit",
                    payload={
                        "spent_usd": round(spent, 6),
                        "cap_usd": daily_cap_usd,
                        "utc_day": _utc_day(utcnow_iso()),
                    },
                )
            return None

    with db.transaction(mode="IMMEDIATE") as conn:
        row = conn.execute(
            """
            SELECT * FROM tasks
            WHERE status = 'ready'
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None

        task_id = row["task_id"]
        now = utcnow_iso()
        conn.execute(
            "UPDATE tasks SET status = 'claimed', pod_id = ?, claimed_at = ? "
            "WHERE task_id = ?",
            (pod_id, now, task_id),
        )
        conn.execute(
            "UPDATE pods SET status = 'working', current_task_id = ?, last_seen = ? "
            "WHERE pod_id = ?",
            (task_id, now, pod_id),
        )

        task = dict(row)
        task["status"] = "claimed"
        task["pod_id"] = pod_id
        task["claimed_at"] = now

    emit_event(
        db, events_jsonl_path, "task_claimed",
        task_id=task_id,
        payload={"pod_id": pod_id, "claimed_at": now},
    )
    return task
