"""Phase 1: claim_next_task must never double-claim under concurrency.

We launch many threads, each with its own ``Database`` (fresh per-thread
sqlite connection), all hammering ``claim_next_task`` against the same
DB file. Every ready task should be claimed exactly once.
"""
from __future__ import annotations

import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from framework import services as svc
from framework.db import Database
from framework.models import TaskCreate
from framework.scheduler import claim_next_task


def _seed_ready_tasks(db: Database, events_jsonl: Path, n: int) -> list[str]:
    ids = []
    for i in range(n):
        spec = TaskCreate(
            agent_role="development",
            goal_text=f"task {i}",
            priority=(i % 3),
        )
        t = svc.create_task(db, events_jsonl, spec)
        svc.approve_before(db, events_jsonl, t.task_id)
        ids.append(t.task_id)
    return ids


def _claim_loop(db_path: Path, events_jsonl: Path, pod_id: str,
                stop_after: int, results: list, lock: threading.Lock):
    db = Database(db_path)
    svc.register_pod(db, events_jsonl, pod_id)
    while True:
        with lock:
            if len(results) >= stop_after:
                return
        task = claim_next_task(db, pod_id, events_jsonl)
        if task is None:
            with lock:
                if len(results) >= stop_after:
                    return
            continue
        with lock:
            results.append((pod_id, task["task_id"]))


def test_no_double_claim_under_concurrency(state_dir):
    db = Database(state_dir.db)
    n_tasks = 50
    ids = _seed_ready_tasks(db, state_dir.events_jsonl, n_tasks)

    results: list[tuple[str, str]] = []
    lock = threading.Lock()
    n_workers = 8

    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futures = [
            ex.submit(_claim_loop, state_dir.db, state_dir.events_jsonl,
                      f"pod_{i}", n_tasks, results, lock)
            for i in range(n_workers)
        ]
        for f in as_completed(futures):
            f.result()  # raise any exceptions

    claimed_task_ids = [tid for _, tid in results]
    counts = Counter(claimed_task_ids)
    duplicates = {tid: c for tid, c in counts.items() if c > 1}
    assert not duplicates, f"double-claimed tasks: {duplicates}"
    assert set(claimed_task_ids) == set(ids), \
        f"missing or extra claims: claimed={set(claimed_task_ids)}, expected={set(ids)}"

    # Every task in the DB should now be in 'claimed' status with a pod_id.
    rows = db.query_all("SELECT task_id, status, pod_id FROM tasks")
    for r in rows:
        assert r["status"] == "claimed", r["task_id"]
        assert r["pod_id"] is not None, r["task_id"]

    # task_claimed events should equal the number of tasks.
    ev_rows = db.query_all(
        "SELECT COUNT(*) AS n FROM events WHERE type = 'task_claimed'"
    )
    assert ev_rows[0]["n"] == n_tasks


def test_claim_priority_order(state_dir):
    """Higher priority claimed first; tie-break by created_at ASC."""
    db = Database(state_dir.db)

    # Seed with mixed priorities: low(1), high(10), mid(5), high-later(10)
    specs = [
        ("low",   1),
        ("high1", 10),
        ("mid",   5),
        ("high2", 10),
    ]
    for goal, prio in specs:
        t = svc.create_task(
            db, state_dir.events_jsonl,
            TaskCreate(agent_role="development",
                       goal_text=goal, priority=prio),
        )
        svc.approve_before(db, state_dir.events_jsonl, t.task_id)

    svc.register_pod(db, state_dir.events_jsonl, "pod_a")
    order = []
    for _ in range(4):
        c = claim_next_task(db, "pod_a", state_dir.events_jsonl)
        assert c is not None
        order.append(c["goal_text"])

    assert order == ["high1", "high2", "mid", "low"], order


def test_claim_returns_none_when_empty(state_dir):
    db = Database(state_dir.db)
    svc.register_pod(db, state_dir.events_jsonl, "pod_a")
    assert claim_next_task(db, "pod_a", state_dir.events_jsonl) is None


def test_claim_skips_unapproved_tasks(state_dir):
    """A task in 'before_gate' must not be claimed."""
    db = Database(state_dir.db)
    svc.create_task(
        db, state_dir.events_jsonl,
        TaskCreate(agent_role="development", goal_text="pending"),
    )
    svc.register_pod(db, state_dir.events_jsonl, "pod_a")
    assert claim_next_task(db, "pod_a", state_dir.events_jsonl) is None
