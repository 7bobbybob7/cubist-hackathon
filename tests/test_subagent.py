"""Phase 4 — methodology agent + planning loop end-to-end.

The test stitches the entire flow together with **two** fake Anthropic
clients (one for the methodology call, one for the pod), driving the
exact path the spec describes in Section 11:

  goal → methodology agent → proposed plan YAML → framework plan create
   → before_gate (per task) → approve → ready → pod claim
   → pod call → submit → after_gate → approve → done.
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from framework import services as svc
from framework.api.app import create_app
from framework.bootstrap import bootstrap_run
from framework.cli import commands as C
from framework.cli._context import CliContext
from framework.cli.subagent import (
    PLANNING_INSTRUCTION, PlanContractViolation, build_planning_prompt,
    parse_agent_md, parse_planning_response, validate_role_contracts,
)
from framework.pod.anthropic_call import CallResult
from framework.pod.backend_client import BackendClient
from framework.pod.worker import process_one_task
from framework.config import load_config


# ---------------- helpers --------------------------------------------

@dataclass
class _Usage:
    input_tokens: int = 200
    output_tokens: int = 100
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _Block:
    type: str
    text: str


@dataclass
class _Resp:
    content: list
    usage: _Usage
    stop_reason: str = "end_turn"


class FakeAnthropic:
    def __init__(self, response_text: str, usage: _Usage | None = None,
                 raise_exc: BaseException | None = None):
        self._text = response_text
        self._usage = usage or _Usage()
        self._raise = raise_exc
        self.calls: list[dict] = []
        self.messages = self  # so .messages.create == self.create

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise:
            raise self._raise
        return _Resp(
            content=[_Block(type="text", text=self._text)],
            usage=self._usage,
        )


def _caller_for(fake: FakeAnthropic):
    from framework.pod.anthropic_call import call_messages
    return lambda **kw: call_messages(fake, **kw)


# ---------------- pure-function tests --------------------------------

def test_parse_agent_md_extracts_frontmatter():
    text = (
        "---\n"
        "role: methodology\n"
        "default_model: claude-opus-4-7\n"
        "---\n"
        "# Methodology Subagent\n\n"
        "## Role\nYou plan things.\n"
    )
    fm, body = parse_agent_md(text)
    assert fm["role"] == "methodology"
    assert fm["default_model"] == "claude-opus-4-7"
    assert body.startswith("# Methodology Subagent")


def test_parse_agent_md_handles_missing_frontmatter():
    fm, body = parse_agent_md("just text\n")
    assert fm == {}
    assert body == "just text\n"


def test_build_planning_prompt_includes_inputs_and_summary():
    md = (
        "---\nrole: methodology\n---\n"
        "You are a planner."
    )
    system, user, _ = build_planning_prompt(
        agent_md_text=md,
        task_goal="Implement UCI",
        target_repo="/tmp/repo",
        rolling_summary="## Goal\nImplement UCI",
        input_artifacts=[{
            "artifact_id": "a_x",
            "artifact_type": "ResearchBrief",
            "content": {"summary": "spec is at..."},
        }],
    )
    assert "PLANNING" in system
    assert "Implement UCI" in user
    assert "/tmp/repo" in user
    assert "a_x (ResearchBrief)" in user
    assert "spec is at..." in user


def test_parse_planning_response_validates_roles():
    good = json.dumps({
        "rationale": "ok",
        "tasks": [
            {"agent_role": "development", "goal_text": "do",
             "output_artifact_types": ["PatchSummary"]},
        ],
    })
    plan = parse_planning_response(good)
    assert len(plan["tasks"]) == 1

    with pytest.raises(ValueError):
        parse_planning_response(json.dumps({
            "tasks": [{"agent_role": "wizard", "goal_text": "x"}],
        }))


def test_validate_role_contracts_passes_clean_plan():
    plan = {"tasks": [
        {"agent_role": "development", "goal_text": "create file.py",
         "output_artifact_types": ["PatchSummary"]},
        {"agent_role": "testing", "goal_text": "run pytest",
         "output_artifact_types": ["TestResult"]},
    ]}
    assert validate_role_contracts(plan) == []


def test_validate_role_contracts_flags_testing_writing_files():
    """The bug we hit on the live FizzBuzz run: methodology agent
    assigned 'create test_fizzbuzz.py' to the testing role, which is
    read-only by design."""
    plan = {"tasks": [
        {"agent_role": "testing", "goal_text": "Create test_fizzbuzz.py",
         "output_artifact_types": ["TestResult"]},
    ]}
    issues = validate_role_contracts(plan)
    assert len(issues) == 1
    assert "testing" in issues[0] and "create" in issues[0].lower()


def test_validate_role_contracts_flags_wrong_artifact_type():
    plan = {"tasks": [
        {"agent_role": "testing", "goal_text": "run pytest",
         "output_artifact_types": ["PatchSummary"]},
    ]}
    issues = validate_role_contracts(plan)
    assert any("cannot produce" in i for i in issues)


def test_subagent_invoke_raises_on_contract_violation(bootstrapped_env, tmp_path):
    """End-to-end: a plan that asks testing to write files should fail
    closed at invoke-time, not at pod-claim time."""
    from framework.cli.subagent import cmd_subagent_invoke

    ctx, _, _, _, _ = bootstrapped_env
    task_yaml = tmp_path / "task.yaml"
    task_yaml.write_text(yaml.safe_dump({"goal": "x", "target_repo": "/tmp/r"}))
    fake = FakeAnthropic(_planning_response([
        {"agent_role": "testing", "goal_text": "Create test_x.py",
         "output_artifact_types": ["TestResult"], "depends_on": [], "priority": 0,
         "rationale": "..."},
    ]))
    with pytest.raises(PlanContractViolation):
        cmd_subagent_invoke(
            ctx, "methodology", str(task_yaml),
            anthropic_caller=_caller_for(fake),
        )


def test_parse_planning_response_strips_code_fences():
    text = "```json\n{\"tasks\": [{\"agent_role\": \"development\", \"goal_text\": \"do\"}]}\n```"
    plan = parse_planning_response(text)
    assert plan["tasks"][0]["agent_role"] == "development"


# ---------------- subagent invoke against TestClient -----------------

@pytest.fixture
def bootstrapped_env(tmp_path):
    """Run start, then build a TestClient bound to the same state dir.
    Returns ctx, db, paths, out, err.
    """
    target = tmp_path / "repo"
    target.mkdir()
    state_root = tmp_path / "fw"
    bootstrap_run(state_root, goal="Implement UCI", target_repo=str(target))

    app = create_app(state_root)  # idempotent — picks up the existing dir
    test_client = TestClient(app)
    backend = BackendClient(http_client=test_client)
    paths = app.state.paths
    db = app.state.db
    out = io.StringIO()
    err = io.StringIO()
    ctx = CliContext(backend=backend, paths=paths, stdout=out, stderr=err)
    yield ctx, db, paths, out, err
    test_client.close()


def _planning_response(tasks: list[dict]) -> str:
    return json.dumps({"rationale": "small first plan", "tasks": tasks})


def test_subagent_invoke_writes_proposed_plan(bootstrapped_env, tmp_path):
    from framework.cli.subagent import cmd_subagent_invoke

    ctx, db, paths, out, _ = bootstrapped_env
    task_yaml = tmp_path / "task.yaml"
    task_yaml.write_text(yaml.safe_dump({
        "goal": "Implement UCI handshake in engine.py",
        "target_repo": str(paths.root.parent / "repo"),
    }))

    fake = FakeAnthropic(_planning_response([
        {
            "agent_role": "methodology", "goal_text": "Survey the codebase",
            "recommended_model": "claude-opus-4-7",
            "output_artifact_types": ["ResearchBrief"],
            "depends_on": [], "priority": 0,
            "rationale": "need to understand layout first",
        },
        {
            "agent_role": "development",
            "goal_text": "Implement UCI parser in engine/uci.py",
            "recommended_model": "claude-sonnet-4-6",
            "output_artifact_types": ["PatchSummary"],
            "depends_on": [0],  # positional ref to the first task
            "priority": 0,
            "rationale": "main implementation",
        },
        {
            "agent_role": "testing",
            "goal_text": "Run integration tests on the parser",
            "recommended_model": "claude-haiku-4-5-20251001",
            "output_artifact_types": ["TestResult"],
            "depends_on": [1],
            "priority": 0,
            "rationale": "verify behavior",
        },
    ]))

    rc = cmd_subagent_invoke(
        ctx, "methodology", str(task_yaml),
        anthropic_caller=_caller_for(fake),
    )
    assert rc == 0

    output = yaml.safe_load(out.getvalue())
    assert output["proposed_plan_path"].startswith(str(paths.plan_dir))
    assert len(output["tasks"]) == 3

    # Proposed plan YAML on disk is consumable by framework plan create
    proposed = Path(output["proposed_plan_path"])
    assert proposed.exists()
    plan_yaml = yaml.safe_load(proposed.read_text())
    assert "tasks" in plan_yaml
    assert len(plan_yaml["tasks"]) == 3
    # Positional depends_on are preserved as ints in the proposed YAML;
    # `framework plan create` resolves them to real task_ids at creation time.
    assert plan_yaml["tasks"][0]["depends_on"] == []
    assert plan_yaml["tasks"][1]["depends_on"] == [0]
    assert plan_yaml["tasks"][2]["depends_on"] == [1]

    # Round-trip through plan create: positional deps become real IDs
    rc = C.cmd_plan_create(ctx, proposed)
    assert rc == 0
    created = svc.list_tasks(db)
    assert len(created) == 3
    by_position = {t.goal_text: t for t in created}
    survey = by_position["Survey the codebase"]
    parser = by_position["Implement UCI parser in engine/uci.py"]
    tests = by_position["Run integration tests on the parser"]
    assert survey.depends_on == []
    assert parser.depends_on == [survey.task_id]
    assert tests.depends_on == [parser.task_id]

    # working_dir defaults from the planning input's target_repo, so pods
    # can sandbox tools to it. Methodology agents that don't emit
    # working_dir explicitly should still get it filled in.
    expected_wd = str(paths.root.parent / "repo")
    assert survey.working_dir == expected_wd
    assert parser.working_dir == expected_wd
    assert tests.working_dir == expected_wd

    # parent_actions row recorded
    rows = db.query_all(
        "SELECT * FROM parent_actions WHERE tool = 'framework_subagent_invoke'"
    )
    assert len(rows) == 1
    args = json.loads(rows[0]["args"])
    assert args["role"] == "methodology"
    assert args["tasks"] == 3

    # Stats block present and reasonable
    stats = output["stats"]
    assert stats["input_tokens"] == 200
    assert stats["output_tokens"] == 100
    assert stats["cost_usd"] >= 0


def test_subagent_invoke_rejects_non_methodology_role(bootstrapped_env, tmp_path):
    from framework.cli.subagent import cmd_subagent_invoke

    ctx, _, _, _, _ = bootstrapped_env
    task_yaml = tmp_path / "task.yaml"
    task_yaml.write_text(yaml.safe_dump({"goal": "x"}))
    with pytest.raises(NotImplementedError):
        cmd_subagent_invoke(
            ctx, "development", str(task_yaml),
            anthropic_caller=_caller_for(FakeAnthropic(_planning_response([
                {"agent_role": "development", "goal_text": "x",
                 "output_artifact_types": ["PatchSummary"]},
            ]))),
        )


# ---------------- the end-to-end planning loop -----------------------

def test_end_to_end_planning_loop(bootstrapped_env, tmp_path):
    """The full Section 11 loop: methodology → plan create → both gates
    → pod → both gates → done. Uses TWO fake Anthropic clients (one for
    the methodology call, one for the pod) so no network."""
    from framework.cli.subagent import cmd_subagent_invoke

    ctx, db, paths, out, _ = bootstrapped_env

    # 1. User submits a goal; parent invokes the methodology agent.
    task_yaml = tmp_path / "task.yaml"
    task_yaml.write_text(yaml.safe_dump({"goal": "Add greet() function"}))
    methodology_fake = FakeAnthropic(_planning_response([
        {
            "agent_role": "development",
            "goal_text": "Implement greet() in main.py",
            "recommended_model": "claude-haiku-4-5-20251001",
            "output_artifact_types": ["PatchSummary"],
            "depends_on": [], "priority": 0,
            "rationale": "the only thing to do",
        },
    ]))
    cmd_subagent_invoke(
        ctx, "methodology", str(task_yaml),
        anthropic_caller=_caller_for(methodology_fake),
    )
    plan_output = yaml.safe_load(out.getvalue())
    proposed = plan_output["proposed_plan_path"]

    # 2. Parent surfaces plan, user approves the structure → plan create.
    out.truncate(0); out.seek(0)
    C.cmd_plan_create(ctx, proposed)
    tasks = svc.list_tasks(db)
    assert len(tasks) == 1
    tid = tasks[0].task_id
    assert tasks[0].status == "before_gate"

    # 3. Before gate — surface, then approve.
    out.truncate(0); out.seek(0)
    C.cmd_plan_show(ctx)
    shown = yaml.safe_load(out.getvalue())
    assert shown[0]["task_id"] == tid

    C.cmd_gate_before_approve(ctx, tid)
    assert svc.get_task(db, tid).status == "ready"

    # 4. Pod claims and processes (with its own fake Anthropic).
    pod_fake = FakeAnthropic(
        '{"files_changed": ["main.py"], "rationale": "added greet"}'
    )
    cfg = load_config(paths.config_yaml)
    status = process_one_task(
        "pod_a", backend=ctx.backend,
        anthropic_caller=_caller_for(pod_fake), config=cfg,
    )
    assert status == "done"
    assert svc.get_task(db, tid).status == "after_gate"

    # 5. After gate — surface artifact + stats, then approve.
    arts = svc.list_artifacts(db, task_id=tid)
    assert len(arts) == 1
    assert arts[0].artifact_type == "PatchSummary"
    assert arts[0].content["files_changed"] == ["main.py"]

    C.cmd_gate_after_approve(ctx, tid)
    assert svc.get_task(db, tid).status == "done"

    # 6. Parent updates rolling summary.
    new_summary = tmp_path / "summary.md"
    new_summary.write_text(
        "## Goal\nAdd greet()\n\n"
        "## Completed milestones\n- main.py greet() implemented\n"
    )
    C.cmd_summary_update(ctx, new_summary)
    assert "greet" in paths.rolling_summary.read_text()

    # Audit trail: parent_actions covers every framework tool we used.
    tools = {r["tool"] for r in db.query_all("SELECT tool FROM parent_actions")}
    expected = {
        "framework_subagent_invoke",
        "framework_plan_create",
        "framework_plan_show",
        "framework_gate_before_approve",
        "framework_gate_after_approve",
        "framework_summary_update",
    }
    missing = expected - tools
    assert not missing, f"missing parent_actions: {missing}"

    # Event stream: the canonical lifecycle events all fired exactly once
    # for our single task.
    types = [r["type"] for r in db.query_all(
        "SELECT type FROM events WHERE task_id = ? ORDER BY ts ASC", (tid,)
    )]
    for ev in ("task_created", "task_before_gate", "task_approved_before",
               "task_claimed", "task_completed", "task_after_gate",
               "task_approved_after"):
        assert ev in types, f"missing event {ev} in {types}"
