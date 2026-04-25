"""Pydantic models for the FastAPI surface.

Mirror the YAML schemas from Section 9 of the methodology.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

AgentRole = Literal["methodology", "development", "testing"]
TaskStatus = Literal[
    "created", "before_gate", "ready", "claimed",
    "running", "after_gate", "done", "rejected",
]
ArtifactType = Literal[
    "ResearchBrief", "PatchSummary", "TestResult",
    "FailureReport", "ProgressLogEntry",
]


class TaskCreate(BaseModel):
    task_id: str | None = None  # server-assigned if absent
    parent_task_id: str | None = None
    agent_role: str
    goal_text: str
    input_artifact_ids: list[str] = Field(default_factory=list)
    output_artifact_types: list[str] = Field(default_factory=list)
    recommended_model: str | None = None
    priority: int = 0
    depends_on: list[str] = Field(default_factory=list)
    working_dir: str | None = None


class TaskEdit(BaseModel):
    """Field-level edit at the before gate.

    Only fields present (non-None) are updated.
    """
    goal_text: str | None = None
    agent_role: str | None = None
    recommended_model: str | None = None
    priority: int | None = None
    input_artifact_ids: list[str] | None = None
    output_artifact_types: list[str] | None = None
    depends_on: list[str] | None = None
    working_dir: str | None = None


class TaskOut(BaseModel):
    task_id: str
    parent_task_id: str | None
    agent_role: str
    goal_text: str
    input_artifact_ids: list[str]
    output_artifact_types: list[str]
    recommended_model: str | None
    priority: int
    created_at: str
    depends_on: list[str]
    working_dir: str | None
    status: str
    pod_id: str | None
    claimed_at: str | None
    started_at: str | None
    completed_at: str | None
    rejection_reason: str | None
    retry_count: int
    archived_at: str | None = None

    @classmethod
    def from_row(cls, row) -> "TaskOut":
        d = dict(row)
        d["input_artifact_ids"] = json.loads(d.get("input_artifact_ids") or "[]")
        d["output_artifact_types"] = json.loads(d.get("output_artifact_types") or "[]")
        d["depends_on"] = json.loads(d.get("depends_on") or "[]")
        return cls(**d)


class ArtifactCreate(BaseModel):
    artifact_id: str | None = None
    artifact_type: str
    produced_by_task: str
    produced_by_agent: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    duration_seconds: float | None = None
    model: str | None = None
    content: Any  # type-specific body; stored as JSON

    @field_validator("content")
    @classmethod
    def _content_is_serializable(cls, v):
        json.dumps(v)
        return v


class ArtifactOut(BaseModel):
    artifact_id: str
    artifact_type: str
    produced_by_task: str
    produced_by_agent: str
    produced_at: str
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    duration_seconds: float | None
    model: str | None
    content: Any

    @classmethod
    def from_row(cls, row) -> "ArtifactOut":
        d = dict(row)
        d["content"] = json.loads(d["content"]) if d["content"] else None
        return cls(**d)


class GateRejectIn(BaseModel):
    reason: str


class PodRegister(BaseModel):
    pod_id: str


class PodOut(BaseModel):
    pod_id: str
    status: str
    last_seen: str | None
    current_task_id: str | None
    registered_at: str


class SubmitResultIn(BaseModel):
    """Pod posts this when it finishes a task.

    Creates one or more artifacts, writes a budget-ledger row, and
    transitions the task to ``after_gate``.
    """
    artifacts: list[ArtifactCreate]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    model: str | None = None


class FailureIn(BaseModel):
    error_message: str
    failure_mode: str = "logic_error"
    retry_count: int = 0


class EventOut(BaseModel):
    event_id: str
    ts: str
    type: str
    task_id: str | None
    payload: dict


class BudgetEntry(BaseModel):
    ts: str
    pod_id: str
    task_id: str
    agent_role: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_seconds: float
