"""Phase 2 tests: pod worker against the FastAPI backend, with the
Anthropic SDK replaced by an in-process fake.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from framework import services as svc
from framework.api.app import create_app
from framework.config import load_config
from framework.db import Database
from framework.models import TaskCreate
from framework.pod.anthropic_call import CallResult, compute_cost
from framework.pod.backend_client import BackendClient
from framework.pod.prompt import build_prompt, parse_artifact_content
from framework.pod.worker import process_one_task


# ---------------- fakes ----------------------------------------------

@dataclass
class _FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class _FakeBlock:
    type: str
    text: str


@dataclass
class _FakeResponse:
    content: list
    usage: _FakeUsage
    stop_reason: str = "end_turn"


class FakeAnthropic:
    """Captures calls and returns a scripted response."""

    def __init__(self, response_text: str = '{"summary": "ok", "notes": []}',
                 usage: _FakeUsage | None = None,
                 raise_exc: BaseException | None = None):
        self._response_text = response_text
        self._usage = usage or _FakeUsage()
        self._raise = raise_exc
        self.calls: list[dict] = []
        # Mimic the SDK shape: client.messages.create(...)
        self.messages = self  # let .messages.create be self.create

    def create(self, **kwargs) -> _FakeResponse:
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return _FakeResponse(
            content=[_FakeBlock(type="text", text=self._response_text)],
            usage=self._usage,
        )


def _caller_for(fake: FakeAnthropic) -> Callable[..., CallResult]:
    """Wrap the fake so it returns a real CallResult, exercising the
    same code path the production caller uses."""
    from framework.pod.anthropic_call import call_messages
    return lambda **kw: call_messages(fake, **kw)


# ---------------- fixtures -------------------------------------------

@pytest.fixture
def app_and_backend(tmp_path):
    from tests.conftest import _copy_agent_templates
    app = create_app(tmp_path / "fw")
    _copy_agent_templates(app.state.paths)
    test_client = TestClient(app)
    backend = BackendClient(http_client=test_client)
    db = app.state.db
    paths = app.state.paths
    yield app, backend, db, paths
    test_client.close()


@pytest.fixture
def config():
    return load_config(None)  # defaults


def _make_ready_task(db, paths, **overrides) -> str:
    spec = TaskCreate(
        agent_role=overrides.pop("agent_role", "development"),
        goal_text=overrides.pop("goal_text", "Implement the foo function"),
        recommended_model=overrides.pop("model", "claude-haiku-4-5-20251001"),
        output_artifact_types=overrides.pop("output_artifact_types", ["PatchSummary"]),
    )
    t = svc.create_task(db, paths.events_jsonl, spec)
    svc.approve_before(db, paths.events_jsonl, t.task_id)
    return t.task_id


# ---------------- tests ----------------------------------------------

def test_prompt_builder_includes_role_and_schema_hint():
    task = {
        "task_id": "t_1",
        "agent_role": "development",
        "goal_text": "do X",
        "output_artifact_types": ["PatchSummary"],
    }
    system, user = build_prompt(task)
    assert "development subagent" in system
    assert "ephemeral" not in system  # cache_control is wrapped at call site
    assert "do X" in user
    assert "PatchSummary" in user
    assert "files_changed" in user  # schema hint is present


def test_parse_artifact_content_handles_code_fences():
    obj = parse_artifact_content('```json\n{"summary": "x"}\n```')
    assert obj == {"summary": "x"}


def test_parse_artifact_content_falls_back_for_invalid_json():
    obj = parse_artifact_content("not json at all")
    assert obj["_parse_error"] is True
    assert obj["raw_text"] == "not json at all"


def test_compute_cost_includes_cache_pricing():
    pricing = {"claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0}}
    usage = _FakeUsage(
        input_tokens=1_000_000, output_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
    )
    cost = compute_cost("claude-haiku-4-5-20251001", usage, pricing)
    # 1.0 input + 5.0 output + 0.1 cache_read + 1.25 cache_create
    assert cost == pytest.approx(1.0 + 5.0 + 0.1 + 1.25)


def test_compute_cost_zero_for_unknown_model():
    assert compute_cost("nope", _FakeUsage(), {}) == 0.0


def test_anthropic_call_wraps_system_in_cache_control(app_and_backend, config):
    _, _, _, _ = app_and_backend
    fake = FakeAnthropic()
    caller = _caller_for(fake)
    result = caller(
        model="claude-haiku-4-5-20251001",
        system="You are a development subagent.",
        user="Hello",
        max_tokens=512,
        pricing=config["pricing"],
    )
    assert result.text == '{"summary": "ok", "notes": []}'
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    # System block must include cache_control
    sys_block = fake.calls[0]["system"][0]
    assert sys_block["cache_control"] == {"type": "ephemeral"}
    assert sys_block["type"] == "text"
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "Hello"}]


def test_process_one_task_happy_path(app_and_backend, config):
    _, backend, db, paths = app_and_backend
    tid = _make_ready_task(db, paths)
    backend.register_pod("pod_a")

    fake = FakeAnthropic(
        response_text='{"files_changed": ["x.py"], "rationale": "did it"}',
        usage=_FakeUsage(input_tokens=200, output_tokens=80),
    )
    status = process_one_task(
        "pod_a", backend=backend,
        anthropic_caller=_caller_for(fake), config=config,
    )
    assert status == "done"

    task = svc.get_task(db, tid)
    assert task.status == "after_gate"
    arts = svc.list_artifacts(db, task_id=tid)
    assert len(arts) == 1
    assert arts[0].artifact_type == "PatchSummary"
    assert arts[0].content == {"files_changed": ["x.py"], "rationale": "did it"}
    assert arts[0].tokens_in == 200
    assert arts[0].tokens_out == 80

    # Budget ledger row + JSONL line
    rows = db.query_all("SELECT * FROM budget_ledger WHERE task_id = ?", (tid,))
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 200
    assert rows[0]["output_tokens"] == 80
    assert rows[0]["model"] == "claude-haiku-4-5-20251001"
    assert rows[0]["cost_usd"] > 0

    lines = paths.budget_ledger_jsonl.read_text().strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["task_id"] == tid

    # Exactly one Anthropic call was made
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == "claude-haiku-4-5-20251001"


def test_process_one_task_idle_when_queue_empty(app_and_backend, config):
    _, backend, _, _ = app_and_backend
    backend.register_pod("pod_a")
    fake = FakeAnthropic()
    assert process_one_task(
        "pod_a", backend=backend,
        anthropic_caller=_caller_for(fake), config=config,
    ) == "idle"
    assert fake.calls == []  # no API call when nothing to do


def test_process_one_task_failure_produces_failure_report(app_and_backend, config):
    _, backend, db, paths = app_and_backend
    tid = _make_ready_task(db, paths)
    backend.register_pod("pod_a")

    fake = FakeAnthropic(raise_exc=RuntimeError("simulated API blow-up"))
    status = process_one_task(
        "pod_a", backend=backend,
        anthropic_caller=_caller_for(fake), config=config,
    )
    assert status == "failed"

    task = svc.get_task(db, tid)
    # FailureReport routes through the after gate (Section 16).
    assert task.status == "after_gate"

    arts = svc.list_artifacts(db, task_id=tid)
    assert len(arts) == 1
    assert arts[0].artifact_type == "FailureReport"
    assert "simulated API blow-up" in arts[0].content["error_message"]


def test_after_gate_reject_returns_to_before_gate_and_can_replay(
    app_and_backend, config,
):
    _, backend, db, paths = app_and_backend
    tid = _make_ready_task(db, paths)
    backend.register_pod("pod_a")
    fake = FakeAnthropic()
    process_one_task("pod_a", backend=backend,
                     anthropic_caller=_caller_for(fake), config=config)
    assert svc.get_task(db, tid).status == "after_gate"

    # User rejects the artifact at the after gate.
    svc.reject_after(db, paths.events_jsonl, tid, "redo with different model")
    t = svc.get_task(db, tid)
    assert t.status == "before_gate"
    assert t.retry_count == 1

    # User edits + re-approves, pod re-runs.
    svc.approve_before(db, paths.events_jsonl, tid)
    process_one_task("pod_a", backend=backend,
                     anthropic_caller=_caller_for(fake), config=config)
    assert svc.get_task(db, tid).status == "after_gate"
    # Two artifacts now, one per attempt.
    arts = svc.list_artifacts(db, task_id=tid)
    assert len(arts) == 2


def test_pod_loop_stops_when_should_stop_returns_true(app_and_backend, config):
    """Smoke test for pod_loop: hand it a fake stop predicate that flips
    after one iteration, prove it doesn't spin forever."""
    from framework.pod.worker import pod_loop

    _, backend, db, paths = app_and_backend
    _make_ready_task(db, paths)
    fake = FakeAnthropic()

    # Stop after the first non-idle iteration finishes (i.e. as soon as
    # we re-enter the loop top with should_stop checking again).
    iterations = {"n": 0}

    def stop():
        iterations["n"] += 1
        return iterations["n"] > 2  # let register_pod + 1 work iter happen

    pod_loop(
        "pod_a", backend=backend,
        anthropic_caller=_caller_for(fake), config=config,
        sleep_fn=lambda _s: None,
        should_stop=stop,
    )
    # The one ready task got processed.
    assert len(fake.calls) >= 1
