"""Phase 6 polish: budget cap, session reset, task requeue.

Resilience matrix per Section 19:
- kill pod mid-task → task stuck in 'claimed' → requeue restores 'ready'
- hit budget cap → claim returns None + budget_cap_hit event (idempotent)
- reject at both gates → already covered in test_lifecycle / test_pod_worker
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass

import pytest
import yaml
from fastapi.testclient import TestClient

from framework import services as svc
from framework.api.app import create_app
from framework.cli import commands as C
from framework.cli._context import CliContext
from framework.config import load_config
from framework.db import Database, init_db
from framework.models import (
    ArtifactCreate, SubmitResultIn, TaskCreate,
)
from framework.pod.backend_client import BackendClient
from framework.scheduler import (
    budget_cap_hit_today, claim_next_task,
)
from framework.state import StatePaths


# ---------------- migration smoke ------------------------------------

def test_init_db_adds_archived_at_to_old_db(tmp_path):
    """An older DB without the archived_at column should still upgrade
    cleanly when init_db runs again."""
    import sqlite3
    db_path = tmp_path / "old.db"
    # Simulate a Phase 5 schema without the column.
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            agent_role TEXT NOT NULL,
            goal_text TEXT NOT NULL,
            input_artifact_ids TEXT NOT NULL DEFAULT '[]',
            output_artifact_types TEXT NOT NULL DEFAULT '[]',
            recommended_model TEXT,
            priority INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            depends_on TEXT NOT NULL DEFAULT '[]',
            working_dir TEXT,
            status TEXT NOT NULL,
            pod_id TEXT,
            claimed_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            rejection_reason TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            parent_task_id TEXT
        )
    """)
    conn.commit()
    conn.close()

    init_db(db_path)  # should run the migration
    db = Database(db_path)
    cols = {r["name"] for r in db.query_all("PRAGMA table_info(tasks)")}
    assert "archived_at" in cols


# ---------------- budget cap -----------------------------------------

@pytest.fixture
def env(tmp_path):
    """Bare backend (no template copy needed for these tests)."""
    state_root = tmp_path / "fw"
    app = create_app(state_root)
    test_client = TestClient(app)
    backend = BackendClient(http_client=test_client)
    paths = app.state.paths
    db = app.state.db
    out = io.StringIO()
    err = io.StringIO()
    ctx = CliContext(backend=backend, paths=paths, stdout=out, stderr=err)
    yield ctx, db, paths, out, err, app
    test_client.close()


def _seed_ready(db, paths) -> str:
    spec = TaskCreate(agent_role="development", goal_text="x")
    t = svc.create_task(db, paths.events_jsonl, spec)
    svc.approve_before(db, paths.events_jsonl, t.task_id)
    return t.task_id


def test_budget_cap_blocks_claim_and_emits_event_once(env):
    _, db, paths, _, _, _ = env
    # Seed today's spend over the cap.
    from framework.db import utcnow_iso
    today = utcnow_iso()
    db.execute(
        "INSERT INTO budget_ledger ("
        "ts, pod_id, task_id, agent_role, model, "
        "input_tokens, output_tokens, cost_usd, duration_seconds) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (today, "pod_a", "t_x", "development", "claude-sonnet-4-6",
         0, 0, 99.99, 0.0),
    )
    _seed_ready(db, paths)
    svc.register_pod(db, paths.events_jsonl, "pod_a")

    # Cap at $50 — already $99.99 spent today.
    first = claim_next_task(
        db, "pod_a", paths.events_jsonl, daily_cap_usd=50.00,
    )
    assert first is None
    assert budget_cap_hit_today(db) is True

    # Second call returns None too, but does NOT emit a second event.
    second = claim_next_task(
        db, "pod_a", paths.events_jsonl, daily_cap_usd=50.00,
    )
    assert second is None
    cap_events = db.query_all(
        "SELECT * FROM events WHERE type = 'budget_cap_hit'"
    )
    assert len(cap_events) == 1, "budget_cap_hit must be emitted exactly once per day"


def test_state_endpoint_surfaces_cap_hit(env):
    """The parent reads `framework state` at the start of each turn,
    so the cap-hit signal must show up there."""
    ctx, db, paths, out, _, _ = env
    from framework.db import utcnow_iso
    db.execute(
        "INSERT INTO budget_ledger (ts, pod_id, task_id, agent_role, model, "
        "input_tokens, output_tokens, cost_usd, duration_seconds) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (utcnow_iso(), "pod_a", "t_x", "development", "claude-sonnet-4-6",
         0, 0, 60.0, 0.0),
    )
    # Trigger the cap event
    _seed_ready(db, paths)
    svc.register_pod(db, paths.events_jsonl, "pod_a")
    claim_next_task(db, "pod_a", paths.events_jsonl, daily_cap_usd=50.0)

    state = ctx.backend.get_state()
    assert state["budget_today"]["spent_usd"] >= 60.0
    assert state["budget_today"]["cap_usd"] == 50.0
    assert state["budget_today"]["cap_hit_today"] is True


def test_pod_claim_endpoint_uses_config_cap(env, tmp_path):
    """End-to-end: the FastAPI claim route should refuse to claim once
    the cap (loaded from config.yaml) is exceeded."""
    ctx, db, paths, _, _, app = env
    from framework.db import utcnow_iso
    # Write a tight config.yaml (1 cent cap)
    paths.config_yaml.write_text(yaml.safe_dump({
        "budget": {"daily_cap_usd": 0.01},
    }))

    db.execute(
        "INSERT INTO budget_ledger (ts, pod_id, task_id, agent_role, model, "
        "input_tokens, output_tokens, cost_usd, duration_seconds) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (utcnow_iso(), "pod_a", "t_x", "development", "claude-sonnet-4-6",
         0, 0, 1.0, 0.0),
    )
    _seed_ready(db, paths)
    svc.register_pod(db, paths.events_jsonl, "pod_a")

    # The HTTP claim should return 204 (nothing to claim).
    assert ctx.backend.claim("pod_a") is None
    # And the cap-hit signal is now in /state.
    state = ctx.backend.get_state()
    assert state["budget_today"]["cap_hit_today"] is True


# ---------------- session reset --------------------------------------

def test_session_reset_archives_done_and_rejected(env):
    ctx, db, paths, out, _, _ = env

    # Seed: one done, one rejected, one before_gate.
    tid_done = _seed_ready(db, paths)
    svc.register_pod(db, paths.events_jsonl, "pod_a")
    svc.claim(db, paths.events_jsonl, "pod_a")
    svc.submit_result(
        db, paths.events_jsonl, paths.budget_ledger_jsonl, tid_done,
        SubmitResultIn(artifacts=[ArtifactCreate(
            artifact_type="PatchSummary",
            produced_by_task=tid_done, produced_by_agent="development",
            content={"x": 1},
        )]),
    )
    svc.approve_after(db, paths.events_jsonl, tid_done)

    # before_gate task that gets rejected (don't auto-approve)
    tid_rej = svc.create_task(
        db, paths.events_jsonl,
        TaskCreate(agent_role="development", goal_text="to reject"),
    ).task_id
    svc.reject_before(db, paths.events_jsonl, tid_rej, "skip")

    tid_pending = svc.create_task(
        db, paths.events_jsonl,
        TaskCreate(agent_role="development", goal_text="pending"),
    ).task_id

    # Reset
    rc = C.cmd_session_reset(ctx)
    assert rc == 0
    assert "archived 2" in out.getvalue()

    # `plan show` defaults to filtering archived.
    out.truncate(0); out.seek(0)
    C.cmd_plan_show(ctx)
    visible = yaml.safe_load(out.getvalue())
    visible_ids = {t["task_id"] for t in visible}
    assert tid_pending in visible_ids
    assert tid_done not in visible_ids
    assert tid_rej not in visible_ids

    # --include-archived restores the full picture
    out.truncate(0); out.seek(0)
    C.cmd_plan_show(ctx, include_archived=True)
    everything = yaml.safe_load(out.getvalue())
    everything_ids = {t["task_id"] for t in everything}
    assert {tid_done, tid_rej, tid_pending}.issubset(everything_ids)

    # parent_actions records the reset
    rows = db.query_all(
        "SELECT * FROM parent_actions WHERE tool = 'framework_session_reset'"
    )
    assert len(rows) == 1
    args = json.loads(rows[0]["args"])
    assert args["archived_tasks"] == 2


def test_session_reset_does_not_archive_pending(env):
    ctx, db, paths, _, _, _ = env
    _seed_ready(db, paths)  # ready, not done
    res = ctx.backend.session_reset()
    assert res["archived_tasks"] == 0


# ---------------- requeue (kill pod mid-task) ------------------------

def test_requeue_resets_stuck_claimed_task_to_ready(env):
    """Simulate a pod killed after claim but before submit."""
    ctx, db, paths, out, _, _ = env
    tid = _seed_ready(db, paths)
    svc.register_pod(db, paths.events_jsonl, "pod_a")
    claimed = svc.claim(db, paths.events_jsonl, "pod_a")
    assert claimed.status == "claimed"
    assert claimed.pod_id == "pod_a"

    # Pod dies. Parent investigates, sees a stuck task, requeues.
    rc = C.cmd_task_requeue(ctx, tid)
    assert rc == 0

    t = svc.get_task(db, tid)
    assert t.status == "ready"
    assert t.pod_id is None
    assert t.claimed_at is None
    assert t.retry_count == 1

    # Pod state cleared so it reports idle.
    pod = db.query_one("SELECT * FROM pods WHERE pod_id = ?", ("pod_a",))
    assert pod["status"] == "idle"
    assert pod["current_task_id"] is None

    # Another pod can now claim it cleanly.
    svc.register_pod(db, paths.events_jsonl, "pod_b")
    re_claimed = svc.claim(db, paths.events_jsonl, "pod_b")
    assert re_claimed is not None
    assert re_claimed.task_id == tid
    assert re_claimed.pod_id == "pod_b"


def test_requeue_refuses_for_non_claimed_task(env):
    ctx, db, paths, _, _, _ = env
    tid = _seed_ready(db, paths)  # status == 'ready'
    import httpx
    with pytest.raises(httpx.HTTPStatusError):
        C.cmd_task_requeue(ctx, tid)


def test_after_gate_reject_at_both_gates_audit_trail(env):
    """The reject-at-both-gates path is already covered functionally
    in test_pod_worker / test_lifecycle. This test confirms the audit
    trail (parent_actions + events) is intact through a rejection-loop."""
    ctx, db, paths, _, _, _ = env
    tid = _seed_ready(db, paths)
    svc.register_pod(db, paths.events_jsonl, "pod_a")
    svc.claim(db, paths.events_jsonl, "pod_a")
    svc.submit_result(
        db, paths.events_jsonl, paths.budget_ledger_jsonl, tid,
        SubmitResultIn(artifacts=[ArtifactCreate(
            artifact_type="PatchSummary",
            produced_by_task=tid, produced_by_agent="development",
            content={"x": 1},
        )]),
    )

    # Reject at after gate (1st time)
    C.cmd_gate_after_reject(ctx, tid, reason="redo")
    assert svc.get_task(db, tid).status == "before_gate"
    # Reject at before gate (now permanently rejected)
    C.cmd_gate_before_reject(ctx, tid, reason="give up")
    assert svc.get_task(db, tid).status == "rejected"

    # Audit trail: both rejection events + both parent_actions
    types = [r["type"] for r in db.query_all(
        "SELECT type FROM events WHERE task_id = ? ORDER BY ts ASC",
        (tid,),
    )]
    assert "task_rejected_after" in types
    assert "task_rejected_before" in types
    tools = [r["tool"] for r in db.query_all(
        "SELECT tool FROM parent_actions ORDER BY id ASC"
    )]
    assert "framework_gate_after_reject" in tools
    assert "framework_gate_before_reject" in tools
