"""v3 — experimental loop with candidate variants.

A candidate set = N tasks attempting the same logical goal with one
varied dimension (model, prompt, etc.). All run in parallel through
the normal gate flow, all land at after_gate, then the user picks
ONE winner; that winner's worktree branch merges into base, the
losers are marked 'abandoned' and their worktrees+branches are pruned.

The set is represented by a "phantom parent" task row with
``task_id`` starting with ``c_`` and ``agent_role='candidate_set'``.
Children point at it via ``parent_task_id`` (the existing FK).
"""
from __future__ import annotations

import subprocess

import pytest

from framework import services as svc
from framework.db import Database
from framework.models import TaskCreate
from framework.services import create_candidate_set, is_candidate_set_id


# ----------------------- Step 1: phantom parent ----------------------

def test_phantom_parent_uses_c_prefix():
    """`c_<id>` is the convention; regular tasks use `t_<id>`."""
    assert is_candidate_set_id("c_abc")
    assert not is_candidate_set_id("t_abc")
    assert not is_candidate_set_id(None)
    assert not is_candidate_set_id("")


def test_create_candidate_set_inserts_phantom_plus_children(state_dir):
    db = Database(state_dir.db)
    out = create_candidate_set(
        db, state_dir.events_jsonl,
        goal_text="implement greet()",
        variants=[
            TaskCreate(agent_role="development", goal_text="",
                       output_artifact_types=["PatchSummary"],
                       recommended_model="claude-haiku-4-5-20251001"),
            TaskCreate(agent_role="development", goal_text="",
                       output_artifact_types=["PatchSummary"],
                       recommended_model="claude-sonnet-4-6"),
            TaskCreate(agent_role="development", goal_text="",
                       output_artifact_types=["PatchSummary"],
                       recommended_model="claude-opus-4-7"),
        ],
    )
    set_id = out["set_id"]
    assert set_id.startswith("c_")
    assert len(out["task_ids"]) == 3

    # Phantom row exists with the right shape.
    phantom = db.query_one(
        "SELECT * FROM tasks WHERE task_id = ?", (set_id,),
    )
    assert phantom is not None
    assert phantom["agent_role"] == "candidate_set"
    assert phantom["goal_text"] == "implement greet()"
    assert phantom["status"] == "done"  # never sits in any queue
    assert phantom["worktree_path"] is None

    # All children point at the phantom and inherited the goal.
    children = db.query_all(
        "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY created_at",
        (set_id,),
    )
    assert len(children) == 3
    assert all(c["parent_task_id"] == set_id for c in children)
    assert all(c["goal_text"] == "implement greet()" for c in children)
    assert all(c["status"] == "before_gate" for c in children)
    models = [c["recommended_model"] for c in children]
    assert models == [
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        "claude-opus-4-7",
    ]


def test_create_candidate_set_emits_event(state_dir):
    db = Database(state_dir.db)
    out = create_candidate_set(
        db, state_dir.events_jsonl,
        goal_text="g",
        variants=[
            TaskCreate(agent_role="development", goal_text="",
                       output_artifact_types=["PatchSummary"]),
        ],
    )
    rows = db.query_all(
        "SELECT * FROM events WHERE type = 'candidate_set_created'"
    )
    assert len(rows) == 1
    assert rows[0]["task_id"] == out["set_id"]


def test_create_candidate_set_rejects_empty(state_dir):
    db = Database(state_dir.db)
    with pytest.raises(ValueError, match="at least one"):
        create_candidate_set(
            db, state_dir.events_jsonl,
            goal_text="g", variants=[],
        )


def test_create_candidate_set_caps_at_16(state_dir):
    db = Database(state_dir.db)
    too_many = [
        TaskCreate(agent_role="development", goal_text="",
                   output_artifact_types=["PatchSummary"])
        for _ in range(17)
    ]
    with pytest.raises(ValueError, match="max 16"):
        create_candidate_set(
            db, state_dir.events_jsonl,
            goal_text="g", variants=too_many,
        )


def test_phantom_parent_satisfies_fk(state_dir):
    """Children's parent_task_id must reference a real row — and a
    bogus reference must fail the FK check."""
    db = Database(state_dir.db)
    # FK enabled per db.py:_configure (PRAGMA foreign_keys = ON).
    with pytest.raises(Exception):  # IntegrityError
        db.execute(
            "INSERT INTO tasks (task_id, parent_task_id, agent_role, "
            "goal_text, status, created_at) "
            "VALUES ('t_orphan', 'c_does_not_exist', 'development', 'x', "
            "'before_gate', '2026-01-01T00:00:00Z')",
        )


# ----------------------- Step 2: API surface -------------------------

@pytest.fixture
def api_env(tmp_path):
    """In-process FastAPI + BackendClient for round-trip tests."""
    from fastapi.testclient import TestClient
    from framework.api.app import create_app
    from framework.pod.backend_client import BackendClient
    state_root = tmp_path / "fw"
    app = create_app(state_root)
    client = TestClient(app)
    backend = BackendClient(http_client=client)
    yield backend, app.state.db, app.state.paths
    client.close()


def test_create_candidate_set_via_api(api_env):
    backend, db, paths = api_env
    out = backend.create_candidate_set(
        goal_text="implement greet()",
        variants=[
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"],
             "recommended_model": "claude-haiku-4-5-20251001",
             "variant_label": "haiku"},
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"],
             "recommended_model": "claude-sonnet-4-6",
             "variant_label": "sonnet"},
        ],
    )
    set_id = out["set_id"]
    assert set_id.startswith("c_")
    assert len(out["task_ids"]) == 2

    # Round-trip via GET — phantom + 2 children, labels preserved.
    fetched = backend.get_candidate_set(set_id)
    assert fetched["set_id"] == set_id
    assert fetched["goal_text"] == "implement greet()"
    assert len(fetched["children"]) == 2
    labels = sorted(c["variant_label"] for c in fetched["children"])
    assert labels == ["haiku", "sonnet"]


def test_get_candidate_set_404_for_unknown(api_env):
    """A bogus set ID surfaces as a 404 (TaskNotFound mapping)."""
    from httpx import HTTPStatusError
    backend, _, _ = api_env
    with pytest.raises(HTTPStatusError) as exc:
        backend.get_candidate_set("c_does_not_exist")
    assert exc.value.response.status_code == 404


def test_get_candidate_set_rejects_non_c_prefix(api_env):
    """Refuse to interpret a regular `t_*` task as a set — 400 not 500."""
    from httpx import HTTPStatusError
    backend, _, _ = api_env
    with pytest.raises(HTTPStatusError) as exc:
        backend.get_candidate_set("t_anything")
    assert exc.value.response.status_code == 400


# --------- Step 3: candidate children don't merge at after-gate -----

@pytest.fixture
def gitenv(tmp_path):
    """Bootstrap a real git target so worktree machinery actually fires."""
    from fastapi.testclient import TestClient
    from framework.api.app import create_app
    from framework.bootstrap import bootstrap_run
    from framework.pod.backend_client import BackendClient
    target = tmp_path / "repo"
    target.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=target, check=True)
    (target / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=target, check=True)
    state_root = tmp_path / "fw"
    info = bootstrap_run(state_root, goal="g", target_repo=str(target))
    app = create_app(state_root)
    test_client = TestClient(app)
    backend = BackendClient(http_client=test_client)
    yield {
        "backend": backend, "db": app.state.db, "paths": app.state.paths,
        "target": target, "branch": info["branch_name"],
    }
    test_client.close()


def _force_after_gate_with_diff(env, task_id: str, file_name: str):
    """Helper: simulate a pod completing a candidate child by writing
    a file in its worktree, then driving the task to after_gate."""
    from framework.db import utcnow_iso
    from framework.models import ArtifactCreate, SubmitResultIn
    db, backend = env["db"], env["backend"]
    # Write a file in the task's worktree (created at before-gate approve).
    wt = backend.get_task(task_id)["worktree_path"]
    assert wt is not None
    from pathlib import Path as _Path
    (_Path(wt) / file_name).write_text(f"VAL_{file_name} = 1\n")
    # Drive the task: claimed → submit_result → after_gate.
    now = utcnow_iso()
    db.execute(
        "UPDATE tasks SET status='claimed', pod_id=?, claimed_at=? WHERE task_id=?",
        ("pod_a", now, task_id),
    )
    art = ArtifactCreate(
        artifact_type="PatchSummary",
        produced_by_task=task_id, produced_by_agent="development",
        content={"files_changed": [file_name], "rationale": "ok",
                 "test_targets": [], "diff_stat": {}},
    )
    res = SubmitResultIn(
        artifacts=[art], input_tokens=10, output_tokens=2,
        cost_usd=0.001, duration_seconds=0.1,
        model="claude-haiku-4-5-20251001",
    )
    backend.submit_result(task_id, res.model_dump())


def test_candidate_child_after_gate_does_not_merge_into_base(gitenv):
    """Approving a candidate child at after-gate must NOT touch base.
    Merge is deferred to promote_candidate so only the winner lands."""
    env = gitenv
    backend, target, branch = env["backend"], env["target"], env["branch"]
    backend.register_pod("pod_a")

    out = backend.create_candidate_set(
        goal_text="add a value",
        variants=[
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"], "variant_label": "a"},
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"], "variant_label": "b"},
        ],
    )
    task_a, task_b = out["task_ids"]
    backend.approve_before(task_a)
    backend.approve_before(task_b)

    # Capture base HEAD before any after-gate approve.
    base_head_before = subprocess.run(
        ["git", "rev-parse", branch], cwd=target,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    _force_after_gate_with_diff(env, task_a, "a.py")
    _force_after_gate_with_diff(env, task_b, "b.py")

    # Base branch must be UNCHANGED — no candidate child merged just by
    # landing at after_gate (Step 3's whole point). Calling
    # `approve_after` on a candidate child is now an IllegalTransition
    # — see test_approve_after_refuses_candidate_children below.
    base_head_after = subprocess.run(
        ["git", "rev-parse", branch], cwd=target,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert base_head_before == base_head_after, (
        "candidate child should not have merged into base at after-gate"
    )

    # But each candidate's per-task branch DOES have its commit (the
    # auto-commit step still ran), so promote can find them later.
    for tid, fname in [(task_a, "a.py"), (task_b, "b.py")]:
        proc = subprocess.run(
            ["git", "cat-file", "-e", f"{branch}-{tid}:{fname}"],
            cwd=target, capture_output=True,
        )
        assert proc.returncode == 0, (
            f"candidate {tid}'s branch should track {fname}"
        )

    # Worktree paths should still be set on the children — the user
    # browses them via the review UI; they're cleaned up at promote/abandon.
    assert backend.get_task(task_a)["worktree_path"] is not None
    assert backend.get_task(task_b)["worktree_path"] is not None


# ----------------------- Step 4: promote -----------------------------

def test_promote_merges_winner_only(gitenv):
    """The winner's diff lands on base; losers' branches and worktrees
    are pruned. Phantom parent gets archived."""
    env = gitenv
    backend, target, branch = env["backend"], env["target"], env["branch"]
    backend.register_pod("pod_a")

    out = backend.create_candidate_set(
        goal_text="add a value",
        variants=[
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"], "variant_label": "a"},
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"], "variant_label": "b"},
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"], "variant_label": "c"},
        ],
    )
    set_id = out["set_id"]
    task_a, task_b, task_c = out["task_ids"]
    backend.approve_before(task_a)
    backend.approve_before(task_b)
    backend.approve_before(task_c)
    _force_after_gate_with_diff(env, task_a, "a.py")
    _force_after_gate_with_diff(env, task_b, "b.py")
    _force_after_gate_with_diff(env, task_c, "c.py")

    # Promote b — verify state transitions and git side effects.
    backend.candidate_promote(set_id, task_b)

    # Winner is done, no worktree; loser branches deleted from target.
    assert backend.get_task(task_b)["status"] == "done"
    assert backend.get_task(task_b)["worktree_path"] is None
    assert backend.get_task(task_a)["status"] == "abandoned"
    assert backend.get_task(task_c)["status"] == "abandoned"
    for tid in (task_a, task_c):
        assert backend.get_task(tid)["worktree_path"] is None

    # Base now has b.py (winner's file); a.py and c.py absent.
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", branch],
        cwd=target, capture_output=True, text=True, check=True,
    )
    files = set(proc.stdout.split())
    assert "b.py" in files
    assert "a.py" not in files
    assert "c.py" not in files

    # Loser branches deleted; winner branch also gone (merged + pruned).
    branches = subprocess.run(
        ["git", "branch", "--list"], cwd=target,
        capture_output=True, text=True, check=True,
    ).stdout
    for tid in (task_a, task_b, task_c):
        assert f"{branch}-{tid}" not in branches, (
            f"per-task branch for {tid} should be deleted"
        )

    # Phantom is archived.
    set_row = env["db"].query_one(
        "SELECT archived_at FROM tasks WHERE task_id = ?", (set_id,),
    )
    assert set_row["archived_at"] is not None

    # Event recorded.
    events = env["db"].query_all(
        "SELECT * FROM events WHERE type = 'candidate_promoted'"
    )
    assert len(events) == 1
    import json as _json
    payload = _json.loads(events[0]["payload"])
    assert payload["winner"] == task_b
    assert set(payload["losers"]) == {task_a, task_c}


def test_promote_rejects_unresolved_siblings(gitenv):
    """If a sibling is still mid-flight (claimed/running/etc.),
    promote refuses — the user must review or abandon first."""
    from httpx import HTTPStatusError
    env = gitenv
    backend = env["backend"]
    backend.register_pod("pod_a")
    out = backend.create_candidate_set(
        goal_text="g",
        variants=[
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"]},
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"]},
        ],
    )
    set_id = out["set_id"]
    task_a, task_b = out["task_ids"]
    backend.approve_before(task_a)
    backend.approve_before(task_b)
    _force_after_gate_with_diff(env, task_a, "a.py")
    # task_b is still in 'ready' (or whatever the fixture left it in),
    # not after_gate. Promoting task_a should refuse.
    with pytest.raises(HTTPStatusError) as exc:
        backend.candidate_promote(set_id, task_a)
    assert exc.value.response.status_code == 409  # IllegalTransition


def test_promote_rejects_unknown_winner(gitenv):
    from httpx import HTTPStatusError
    env = gitenv
    backend = env["backend"]
    out = backend.create_candidate_set(
        goal_text="g",
        variants=[{"agent_role": "development", "goal_text": "",
                   "output_artifact_types": ["PatchSummary"]}],
    )
    with pytest.raises(HTTPStatusError) as exc:
        backend.candidate_promote(out["set_id"], "t_not_a_candidate")
    assert exc.value.response.status_code == 400


def test_approve_after_refuses_candidate_children(gitenv):
    """The user must promote/abandon, not approve_after — otherwise N
    candidates could all flip to 'done' with no merge ever happening."""
    from httpx import HTTPStatusError
    env = gitenv
    backend = env["backend"]
    backend.register_pod("pod_a")
    out = backend.create_candidate_set(
        goal_text="g",
        variants=[
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"]},
        ],
    )
    task = out["task_ids"][0]
    backend.approve_before(task)
    _force_after_gate_with_diff(env, task, "x.py")
    with pytest.raises(HTTPStatusError) as exc:
        backend.approve_after(task)
    assert exc.value.response.status_code == 409
    assert "candidate" in exc.value.response.text


# ----------------------- Step 5: abandon -----------------------------

def test_abandon_drops_all_with_no_merge(gitenv):
    env = gitenv
    backend, target, branch = env["backend"], env["target"], env["branch"]
    backend.register_pod("pod_a")

    out = backend.create_candidate_set(
        goal_text="g",
        variants=[
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"]},
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"]},
        ],
    )
    set_id = out["set_id"]
    task_a, task_b = out["task_ids"]
    backend.approve_before(task_a)
    backend.approve_before(task_b)
    _force_after_gate_with_diff(env, task_a, "a.py")
    _force_after_gate_with_diff(env, task_b, "b.py")

    base_head_before = subprocess.run(
        ["git", "rev-parse", branch], cwd=target,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    backend.candidate_abandon(set_id, reason="all bad")

    # Both children abandoned, no worktrees.
    assert backend.get_task(task_a)["status"] == "abandoned"
    assert backend.get_task(task_b)["status"] == "abandoned"
    assert backend.get_task(task_a)["worktree_path"] is None
    assert backend.get_task(task_b)["worktree_path"] is None

    # Base unchanged (no merge).
    base_head_after = subprocess.run(
        ["git", "rev-parse", branch], cwd=target,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert base_head_before == base_head_after

    # Per-task branches deleted.
    branches = subprocess.run(
        ["git", "branch", "--list"], cwd=target,
        capture_output=True, text=True, check=True,
    ).stdout
    assert f"{branch}-{task_a}" not in branches
    assert f"{branch}-{task_b}" not in branches

    # Phantom archived.
    assert env["db"].query_one(
        "SELECT archived_at FROM tasks WHERE task_id = ?", (set_id,),
    )["archived_at"] is not None


def test_abandon_preserves_already_resolved_children(gitenv):
    """If the user already rejected a child individually before
    abandoning the set, that child's 'rejected' status is preserved —
    we don't squash it under 'abandoned'."""
    env = gitenv
    backend = env["backend"]
    backend.register_pod("pod_a")
    out = backend.create_candidate_set(
        goal_text="g",
        variants=[
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"]},
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"]},
        ],
    )
    set_id = out["set_id"]
    task_a, task_b = out["task_ids"]
    backend.reject_before(task_a, reason="clearly bad")
    # Now task_a is 'rejected'. task_b is still 'before_gate'.
    backend.candidate_abandon(set_id, reason="moving on")

    assert backend.get_task(task_a)["status"] == "rejected"
    assert backend.get_task(task_a)["rejection_reason"] == "clearly bad"
    assert backend.get_task(task_b)["status"] == "abandoned"


# ----------------------- Step 6: CLI surface -------------------------

def test_plan_candidates_cli_creates_set(gitenv, tmp_path):
    """`framework plan candidates <yaml>` creates the phantom + N children."""
    from framework.cli import commands as C
    from framework.cli._context import CliContext
    env = gitenv
    ctx = CliContext(backend=env["backend"], paths=env["paths"])

    spec_yaml = tmp_path / "cands.yaml"
    spec_yaml.write_text(
        "goal: implement greet()\n"
        "shared_role: development\n"
        "variants:\n"
        "  - variant_label: opus\n"
        "    recommended_model: claude-opus-4-7\n"
        "    output_artifact_types: [PatchSummary]\n"
        "    agent_role: development\n"
        "    goal_text: ''\n"
        "  - variant_label: haiku\n"
        "    recommended_model: claude-haiku-4-5-20251001\n"
        "    output_artifact_types: [PatchSummary]\n"
        "    agent_role: development\n"
        "    goal_text: ''\n"
    )
    rc = C.cmd_plan_candidates(ctx, spec_yaml)
    assert rc == 0

    sets = env["db"].query_all(
        "SELECT * FROM tasks WHERE agent_role = 'candidate_set'"
    )
    assert len(sets) == 1
    children = env["db"].query_all(
        "SELECT variant_label FROM tasks WHERE parent_task_id = ? "
        "ORDER BY created_at",
        (sets[0]["task_id"],),
    )
    assert [c["variant_label"] for c in children] == ["opus", "haiku"]


def test_plan_candidates_cli_validates_yaml(gitenv, tmp_path):
    """Missing 'goal' or 'variants' should fail loudly."""
    from framework.cli import commands as C
    from framework.cli._context import CliContext
    env = gitenv
    ctx = CliContext(backend=env["backend"], paths=env["paths"])
    bad = tmp_path / "bad.yaml"
    bad.write_text("variants: []\n")  # no goal
    with pytest.raises(ValueError, match="goal"):
        C.cmd_plan_candidates(ctx, bad)


def test_candidate_review_cli_lists_all_siblings(gitenv):
    """`candidate review <set_id>` surfaces every child's status,
    model, and primary artifact rationale."""
    import io
    from framework.cli import commands as C
    from framework.cli._context import CliContext
    env = gitenv
    out_buf = io.StringIO()
    ctx = CliContext(backend=env["backend"], paths=env["paths"], stdout=out_buf)
    env["backend"].register_pod("pod_a")

    out = env["backend"].create_candidate_set(
        goal_text="impl",
        variants=[
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"], "variant_label": "a"},
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"], "variant_label": "b"},
        ],
    )
    set_id = out["set_id"]
    task_a, task_b = out["task_ids"]
    env["backend"].approve_before(task_a)
    env["backend"].approve_before(task_b)
    _force_after_gate_with_diff(env, task_a, "a.py")
    _force_after_gate_with_diff(env, task_b, "b.py")

    rc = C.cmd_candidate_review(ctx, set_id)
    assert rc == 0
    rendered = out_buf.getvalue()
    assert set_id in rendered
    assert task_a in rendered and task_b in rendered
    assert "candidate promote" in rendered  # surfaces next-step hint


def test_candidate_promote_cli(gitenv):
    """`candidate promote <set_id> <winner>` end-to-end."""
    from framework.cli import commands as C
    from framework.cli._context import CliContext
    env = gitenv
    ctx = CliContext(backend=env["backend"], paths=env["paths"])
    env["backend"].register_pod("pod_a")
    out = env["backend"].create_candidate_set(
        goal_text="g",
        variants=[
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"]},
            {"agent_role": "development", "goal_text": "",
             "output_artifact_types": ["PatchSummary"]},
        ],
    )
    set_id = out["set_id"]
    a, b = out["task_ids"]
    env["backend"].approve_before(a)
    env["backend"].approve_before(b)
    _force_after_gate_with_diff(env, a, "a.py")
    _force_after_gate_with_diff(env, b, "b.py")

    rc = C.cmd_candidate_promote(ctx, set_id, a)
    assert rc == 0
    assert env["backend"].get_task(a)["status"] == "done"
    assert env["backend"].get_task(b)["status"] == "abandoned"


def test_candidate_argparse_routes():
    """Lock the parser contract for plan candidates + candidate verbs."""
    from framework.cli.parser import build_parser
    p = build_parser()
    args = p.parse_args(["plan", "candidates", "/tmp/x.yaml"])
    assert args.yaml_file == "/tmp/x.yaml"
    args = p.parse_args(["candidate", "review", "c_xyz"])
    assert args.set_id == "c_xyz"
    args = p.parse_args(["candidate", "promote", "c_xyz", "t_abc"])
    assert args.set_id == "c_xyz" and args.winner_task_id == "t_abc"
    args = p.parse_args(
        ["candidate", "abandon", "c_xyz", "--reason", "all bad"],
    )
    assert args.set_id == "c_xyz" and args.reason == "all bad"


def test_phantom_is_excluded_from_claim_queue(state_dir):
    """Phantoms have status='done' and shouldn't be claimable. Sanity
    check: claim_next_task returns None when only phantoms exist."""
    from framework.scheduler import claim_next_task
    db = Database(state_dir.db)
    create_candidate_set(
        db, state_dir.events_jsonl,
        goal_text="g",
        variants=[
            TaskCreate(agent_role="development", goal_text="",
                       output_artifact_types=["PatchSummary"]),
        ],
    )
    svc.register_pod(db, state_dir.events_jsonl, "pod_a")
    # Children are at before_gate, not ready — and the phantom is at
    # 'done'. So nothing is claimable.
    assert claim_next_task(db, "pod_a", state_dir.events_jsonl) is None
