"""Phase 1: smoke-test the FastAPI surface end-to-end through HTTP."""
from fastapi.testclient import TestClient

from framework.api.app import create_app


def test_full_lifecycle_via_http(tmp_path):
    app = create_app(tmp_path / "fw")
    client = TestClient(app)

    r = client.post("/tasks", json={
        "agent_role": "development",
        "goal_text": "implement foo",
        "recommended_model": "claude-haiku-4-5-20251001",
        "priority": 1,
    })
    assert r.status_code == 200, r.text
    task = r.json()
    tid = task["task_id"]
    assert task["status"] == "before_gate"

    # Edit before approving
    r = client.patch(f"/tasks/{tid}", json={"goal_text": "implement foo v2"})
    assert r.status_code == 200
    assert r.json()["goal_text"] == "implement foo v2"

    # Before-gate approve
    assert client.post(f"/tasks/{tid}/gate/before/approve").json()["status"] == "ready"

    # Register pod + claim
    assert client.post("/pods", json={"pod_id": "pod_a"}).status_code == 200
    claim = client.post("/pods/pod_a/claim")
    assert claim.status_code == 200, claim.text
    assert claim.json()["task_id"] == tid

    # Mark running, then submit
    assert client.post(f"/tasks/{tid}/start").json()["status"] == "running"

    submit = client.post(f"/tasks/{tid}/submit", json={
        "artifacts": [{
            "artifact_type": "PatchSummary",
            "produced_by_task": tid,
            "produced_by_agent": "development",
            "content": {"files_changed": ["x.py"], "rationale": "x"},
        }],
        "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0001,
        "duration_seconds": 0.5, "model": "claude-haiku-4-5-20251001",
    })
    assert submit.status_code == 200, submit.text
    assert submit.json()["task"]["status"] == "after_gate"

    # After-gate approve
    final = client.post(f"/tasks/{tid}/gate/after/approve")
    assert final.status_code == 200
    assert final.json()["status"] == "done"

    # Events present
    evs = client.get("/events", params={"task_id": tid}).json()
    types = [e["type"] for e in evs]
    for t in (
        "task_created", "task_before_gate", "task_approved_before",
        "task_claimed", "artifact_submitted", "budget_updated",
        "task_completed", "task_after_gate", "task_approved_after",
    ):
        assert t in types, f"missing event {t}: {types}"

    # Budget total reflects the submission
    total = client.get("/budget/total").json()
    assert total["total_usd"] >= 0.0001


def test_claim_returns_204_when_empty(tmp_path):
    app = create_app(tmp_path / "fw")
    client = TestClient(app)
    client.post("/pods", json={"pod_id": "pod_a"})
    r = client.post("/pods/pod_a/claim")
    assert r.status_code == 204


def test_illegal_transition_returns_409(tmp_path):
    app = create_app(tmp_path / "fw")
    client = TestClient(app)
    r = client.post("/tasks", json={
        "agent_role": "development",
        "goal_text": "x",
    })
    tid = r.json()["task_id"]
    # Cannot approve_after a task that hasn't been submitted
    bad = client.post(f"/tasks/{tid}/gate/after/approve")
    assert bad.status_code == 409
