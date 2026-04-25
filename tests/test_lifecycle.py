"""Phase 1 end-to-end: walk a task through every state."""
import json

import pytest

from framework import services as svc
from framework.models import (
    ArtifactCreate, FailureIn, GateRejectIn, SubmitResultIn,
    TaskCreate, TaskEdit,
)


def _make_task(db, paths, **overrides) -> str:
    spec = TaskCreate(
        agent_role=overrides.pop("agent_role", "development"),
        goal_text=overrides.pop("goal_text", "do a thing"),
        recommended_model=overrides.pop("recommended_model", "claude-haiku-4-5-20251001"),
        priority=overrides.pop("priority", 0),
    )
    t = svc.create_task(db, paths.events_jsonl, spec, **overrides)
    return t.task_id


def test_full_happy_path(db, state_dir):
    paths = state_dir
    tid = _make_task(db, paths)

    # created → before_gate (entered automatically)
    assert svc.get_task(db, tid).status == "before_gate"

    # edits at before_gate are allowed
    edited = svc.edit_task(db, paths.events_jsonl, tid,
                           TaskEdit(goal_text="updated goal", priority=5))
    assert edited.goal_text == "updated goal"
    assert edited.priority == 5

    # before_gate → ready
    assert svc.approve_before(db, paths.events_jsonl, tid).status == "ready"

    # register a pod, then claim
    svc.register_pod(db, paths.events_jsonl, "pod_a")
    claimed = svc.claim(db, paths.events_jsonl, "pod_a")
    assert claimed is not None
    assert claimed.task_id == tid
    assert claimed.status == "claimed"
    assert claimed.pod_id == "pod_a"

    # claimed → running
    assert svc.mark_running(db, paths.events_jsonl, tid).status == "running"

    # running → after_gate via submit_result
    body = SubmitResultIn(
        artifacts=[ArtifactCreate(
            artifact_type="PatchSummary",
            produced_by_task=tid,
            produced_by_agent="development",
            content={"files_changed": ["a.py"], "rationale": "x"},
        )],
        input_tokens=100, output_tokens=50, cost_usd=0.001,
        duration_seconds=2.0, model="claude-haiku-4-5-20251001",
    )
    task, arts = svc.submit_result(
        db, paths.events_jsonl, paths.budget_ledger_jsonl, tid, body,
    )
    assert task.status == "after_gate"
    assert len(arts) == 1
    assert arts[0].artifact_type == "PatchSummary"

    # after_gate → done
    done = svc.approve_after(db, paths.events_jsonl, tid)
    assert done.status == "done"

    # budget ledger picked up the row (SQLite + JSONL)
    rows = db.query_all("SELECT * FROM budget_ledger WHERE task_id = ?", (tid,))
    assert len(rows) == 1
    assert rows[0]["cost_usd"] == pytest.approx(0.001)
    lines = paths.budget_ledger_jsonl.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["task_id"] == tid

    # events.jsonl mirrors SQLite events
    ev_lines = paths.events_jsonl.read_text().strip().splitlines()
    types_jsonl = [json.loads(l)["type"] for l in ev_lines]
    types_db = [r["type"] for r in db.query_all("SELECT type FROM events ORDER BY ts ASC")]
    assert types_jsonl == types_db
    assert "task_approved_after" in types_db


def test_rejection_at_after_gate_returns_to_before_gate(db, state_dir):
    paths = state_dir
    tid = _make_task(db, paths)
    svc.approve_before(db, paths.events_jsonl, tid)
    svc.register_pod(db, paths.events_jsonl, "pod_a")
    svc.claim(db, paths.events_jsonl, "pod_a")
    svc.mark_running(db, paths.events_jsonl, tid)
    svc.submit_result(
        db, paths.events_jsonl, paths.budget_ledger_jsonl, tid,
        SubmitResultIn(
            artifacts=[ArtifactCreate(
                artifact_type="PatchSummary",
                produced_by_task=tid,
                produced_by_agent="development",
                content={"x": 1},
            )],
        ),
    )

    after_reject = svc.reject_after(db, paths.events_jsonl, tid, "wrong approach")
    assert after_reject.status == "before_gate"
    assert after_reject.retry_count == 1
    assert after_reject.rejection_reason == "wrong approach"
    assert after_reject.pod_id is None  # cleared so it can be reclaimed

    # After edits, it can flow through again.
    svc.edit_task(db, paths.events_jsonl, tid, TaskEdit(goal_text="try again"))
    svc.approve_before(db, paths.events_jsonl, tid)
    assert svc.get_task(db, tid).status == "ready"


def test_rejection_at_before_gate(db, state_dir):
    paths = state_dir
    tid = _make_task(db, paths)
    svc.reject_before(db, paths.events_jsonl, tid, "not now")
    t = svc.get_task(db, tid)
    assert t.status == "rejected"
    assert t.rejection_reason == "not now"


def test_edit_only_allowed_at_before_gate(db, state_dir):
    paths = state_dir
    tid = _make_task(db, paths)
    svc.approve_before(db, paths.events_jsonl, tid)
    with pytest.raises(svc.IllegalTransition):
        svc.edit_task(db, paths.events_jsonl, tid, TaskEdit(goal_text="nope"))


def test_approve_before_requires_before_gate(db, state_dir):
    paths = state_dir
    tid = _make_task(db, paths)
    svc.approve_before(db, paths.events_jsonl, tid)  # → ready
    with pytest.raises(svc.IllegalTransition):
        svc.approve_before(db, paths.events_jsonl, tid)


def test_claim_returns_none_when_no_ready_tasks(db, state_dir):
    svc.register_pod(db, state_dir.events_jsonl, "pod_a")
    assert svc.claim(db, state_dir.events_jsonl, "pod_a") is None


def test_failure_report_routes_through_after_gate(db, state_dir):
    paths = state_dir
    tid = _make_task(db, paths)
    svc.approve_before(db, paths.events_jsonl, tid)
    svc.register_pod(db, paths.events_jsonl, "pod_a")
    svc.claim(db, paths.events_jsonl, "pod_a")
    svc.mark_running(db, paths.events_jsonl, tid)

    t = svc.report_failure(
        db, paths.events_jsonl, tid,
        FailureIn(error_message="api 500", failure_mode="api_error"),
    )
    assert t.status == "after_gate"
    arts = svc.list_artifacts(db, task_id=tid)
    assert len(arts) == 1
    assert arts[0].artifact_type == "FailureReport"
