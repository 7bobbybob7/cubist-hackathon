"""Synchronous subagent invocation.

Phase 4: only the **methodology** role is wired up — the parent calls
``framework subagent invoke methodology <task-yaml>`` to get a proposed
plan it can then surface to the user.

The subagent call is *not* recorded in ``budget_ledger`` (which is
reserved for pod calls per Section 15) — its stats are printed for the
parent to surface inline. ``parent_actions.jsonl`` records that the
tool was invoked.

For testability the API caller is injected. Production builds a real
client lazily from ``ANTHROPIC_API_KEY`` (or an explicit env var).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from framework.cli._context import CliContext
from framework.config import load_config
from framework.pod.anthropic_call import CallResult


_FRONTMATTER = re.compile(r"^---\s*\n(.*?\n)---\s*\n(.*)$", re.DOTALL)


def parse_agent_md(text: str) -> tuple[dict[str, Any], str]:
    """Split a ``agents/<role>.md`` file into (frontmatter dict, body string)."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    fm = yaml.safe_load(m.group(1)) or {}
    body = m.group(2).strip() + "\n"
    return fm, body


PLANNING_INSTRUCTION = (
    "\n\n## This invocation: PLANNING\n"
    "Decompose the goal into a sequence of tasks. Each task will pass through a "
    "user-controlled before-gate and after-gate, so prefer many small tasks over "
    "a few large ones. Assign each task to one of: methodology, development, testing.\n"
    "\n"
    "Return ONE JSON object only, no surrounding prose, no code fences. Schema:\n"
    "{\n"
    '  "rationale": "<2-4 sentence overall plan rationale>",\n'
    '  "tasks": [\n'
    "    {\n"
    '      "agent_role": "development",\n'
    '      "goal_text": "<one-paragraph task description>",\n'
    '      "recommended_model": "claude-haiku-4-5-20251001|claude-sonnet-4-6|claude-opus-4-7",\n'
    '      "output_artifact_types": ["PatchSummary"],\n'
    '      "depends_on": [],\n'
    '      "priority": 0,\n'
    '      "rationale": "<1-2 sentences why this task exists>"\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "\n"
    "Rules:\n"
    "- Use Haiku for trivial / fanout tasks, Sonnet for normal coding/testing, "
    "Opus only when correctness is critical.\n"
    "- ``depends_on`` lists the *positions* (0-indexed) of prior tasks in this "
    "same plan that must complete first. Resolve to task IDs after creation.\n"
    "- Prefer priority 0 unless one task should clearly run before its peers.\n"
    "\n"
    "Role capabilities (HARD CONSTRAINTS — violations are rejected):\n"
    "- methodology: read-only research and planning. Tools: filesystem_read, web_search. "
    "Output: ResearchBrief. Cannot edit files. Cannot run shell commands.\n"
    "- development: full filesystem write + bash. Output: PatchSummary. Use this "
    "role for ANY task that creates, writes, edits, or generates a file — "
    "including writing test files. The development role is the ONLY role that "
    "can create files.\n"
    "- testing: read-only file access + bash. Output: TestResult. Use ONLY for "
    "running existing tests (e.g. `pytest`) and reporting results. Never assign "
    "the testing role a goal that involves creating, writing, or editing files. "
    "If a test file does not yet exist, plan a separate development task to "
    "write it first.\n"
    "\n"
    "output_artifact_types must match the role's output: "
    "methodology→[ResearchBrief], development→[PatchSummary], testing→[TestResult].\n"
)


def build_planning_prompt(
    *,
    agent_md_text: str,
    task_goal: str,
    target_repo: str | None,
    rolling_summary: str,
    input_artifacts: list[dict[str, Any]],
) -> tuple[str, str, dict[str, Any]]:
    """Returns ``(system_prompt, user_message, frontmatter_dict)``."""
    frontmatter, body = parse_agent_md(agent_md_text)

    system = body + PLANNING_INSTRUCTION

    parts = [
        f"Goal:\n{task_goal}\n",
    ]
    if target_repo:
        parts.append(f"Target repo path: {target_repo}\n")
    if rolling_summary.strip():
        parts.append("Rolling summary (current run state):\n"
                     "------\n" + rolling_summary.strip() + "\n------\n")
    if input_artifacts:
        parts.append("Input artifacts:")
        for a in input_artifacts:
            parts.append(
                f"- {a['artifact_id']} ({a['artifact_type']}):\n"
                + json.dumps(a.get("content", {}), indent=2)
            )
    parts.append(
        "\nProduce the planning JSON now. Output the JSON object only — "
        "no prose, no code fences."
    )
    user = "\n".join(parts)
    return system, user, frontmatter


_TS_OK = ("methodology", "development", "testing")
_VALID_ARTIFACT_TYPES = (
    "ResearchBrief", "PatchSummary", "TestResult",
    "FailureReport", "ProgressLogEntry",
)

# Each role's allowed primary output types. ProgressLogEntry is synthesized
# by the framework so it's not in this list; FailureReport is a fallback
# any role can emit and is also excluded.
_ROLE_PRIMARY_OUTPUTS = {
    "methodology": {"ResearchBrief"},
    "development": {"PatchSummary"},
    "testing":     {"TestResult"},
}

# Words that suggest a task is asking the role to write/create a file.
# Used to flag testing-role tasks that violate read-only.
_WRITE_VERBS = re.compile(
    r"\b(create|write|add|generate|implement|edit|modify|patch|"
    r"insert|append|delete|remove|refactor)\b",
    re.IGNORECASE,
)


def validate_role_contracts(plan: dict[str, Any]) -> list[str]:
    """Return a list of human-readable contract violations in ``plan['tasks']``.

    Empty list means the plan is internally consistent. Caller decides
    whether to raise or just warn.
    """
    issues: list[str] = []
    for i, t in enumerate(plan.get("tasks", []) or []):
        role = t.get("agent_role")
        out_types = list(t.get("output_artifact_types") or [])
        goal = (t.get("goal_text") or "").strip()

        allowed = _ROLE_PRIMARY_OUTPUTS.get(role, set())
        for at in out_types:
            if at in ("ProgressLogEntry", "FailureReport"):
                continue
            if at not in allowed:
                issues.append(
                    f"task[{i}] role={role!r} cannot produce {at!r} "
                    f"(allowed: {sorted(allowed) or 'none'})"
                )
        if role == "testing" and _WRITE_VERBS.search(goal):
            issues.append(
                f"task[{i}] role='testing' goal contains write-verb "
                f"({_WRITE_VERBS.search(goal).group(0)!r}); "
                "the testing role is read-only — split off a "
                "development task to do the writing"
            )
    return issues


class PlanContractViolation(ValueError):
    """Raised when the methodology agent emits a plan that violates role contracts."""


def parse_planning_response(text: str) -> dict[str, Any]:
    """Parse the JSON the model returned. Tolerates code fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    plan = json.loads(text)
    if not isinstance(plan, dict) or "tasks" not in plan:
        raise ValueError("planning response missing 'tasks' key")
    if not isinstance(plan["tasks"], list) or not plan["tasks"]:
        raise ValueError("planning response 'tasks' must be a non-empty list")
    for i, t in enumerate(plan["tasks"]):
        if t.get("agent_role") not in _TS_OK:
            raise ValueError(
                f"task[{i}].agent_role must be one of {_TS_OK}; got {t.get('agent_role')!r}"
            )
        if not t.get("goal_text"):
            raise ValueError(f"task[{i}] missing goal_text")
        out_types = t.get("output_artifact_types") or []
        for at in out_types:
            if at not in _VALID_ARTIFACT_TYPES:
                raise ValueError(
                    f"task[{i}].output_artifact_types includes unknown {at!r}"
                )
    return plan


def _strip_to_taskspec(
    t: dict[str, Any], idx: int, *, default_working_dir: str | None = None,
) -> dict[str, Any]:
    """Drop fields ``framework plan create`` doesn't accept (rationale, etc.)
    and validate positional depends_on (forward refs / self-refs are dropped).

    Positional ``depends_on`` (integers) are preserved as-is for
    ``cmd_plan_create`` to resolve into real task IDs at create time.

    ``working_dir`` defaults to ``default_working_dir`` (the planning
    invocation's ``target_repo``) when the model doesn't supply one. Pods
    need this set or they can't expose filesystem/bash tools to the role.
    """
    deps_raw = t.get("depends_on", []) or []
    deps_clean: list = []
    for d in deps_raw:
        if isinstance(d, int):
            if 0 <= d < idx:
                deps_clean.append(d)
            # else: forward / self-reference — drop
        elif isinstance(d, str):
            deps_clean.append(d)
    return {
        "agent_role": t["agent_role"],
        "goal_text": t["goal_text"],
        "recommended_model": t.get("recommended_model"),
        "output_artifact_types": t.get("output_artifact_types") or [],
        "depends_on": deps_clean,
        "priority": int(t.get("priority", 0)),
        "working_dir": t.get("working_dir") or default_working_dir,
    }


def _utc_ts() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


# ---------------- the CLI command ------------------------------------

def cmd_subagent_invoke(
    ctx: CliContext,
    role: str,
    task_yaml_path: str,
    *,
    anthropic_caller: Callable[..., CallResult] | None = None,
    api_key_env: str = "ANTHROPIC_API_KEY",
) -> int:
    if role != "methodology":
        raise NotImplementedError(
            f"Phase 4 wires up the 'methodology' role only; got {role!r}"
        )

    # Load agent role config
    agent_md_path = ctx.paths.agents_dir / f"{role}.md"
    if not agent_md_path.exists():
        raise FileNotFoundError(
            f"agent config not found: {agent_md_path}. "
            "Run `framework run start` first."
        )
    agent_md_text = agent_md_path.read_text(encoding="utf-8")

    # Load task spec
    task = yaml.safe_load(Path(task_yaml_path).read_text(encoding="utf-8"))
    if not isinstance(task, dict) or not task.get("goal"):
        raise ValueError("task YAML must have at least a 'goal' field")

    # Load rolling summary from disk (anti-context-rot pipe per Section 12).
    summary = (
        ctx.paths.rolling_summary.read_text(encoding="utf-8")
        if ctx.paths.rolling_summary.exists() else ""
    )

    # Resolve any input artifacts referenced in the task
    inputs = []
    for aid in task.get("input_artifact_ids", []) or []:
        inputs.append(ctx.backend.get_artifact(aid))

    system, user, frontmatter = build_planning_prompt(
        agent_md_text=agent_md_text,
        task_goal=task["goal"],
        target_repo=task.get("target_repo"),
        rolling_summary=summary,
        input_artifacts=inputs,
    )

    # Resolve model
    cfg = load_config(ctx.paths.config_yaml if ctx.paths.config_yaml.exists() else None)
    model = (
        task.get("recommended_model")
        or frontmatter.get("default_model")
        or cfg["models"]["methodology_default"]
    )

    # Build the caller if not injected
    if anthropic_caller is None:
        api_key = os.environ.get(api_key_env) or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                f"{api_key_env} (or ANTHROPIC_API_KEY) must be set for subagent invoke"
            )
        from framework.pod.anthropic_call import build_anthropic_client, call_messages
        client = build_anthropic_client(
            api_key, max_retries=int(cfg.get("retries", {}).get("per_call", 3)),
        )
        anthropic_caller = lambda **kw: call_messages(client, **kw)

    result = anthropic_caller(
        model=model,
        system=system,
        user=user,
        # Plans can be longer than a single artifact body. Give them room.
        max_tokens=int(cfg["pod"]["max_tokens"]) * 4,
        pricing=cfg["pricing"],
    )

    plan = parse_planning_response(result.text)

    issues = validate_role_contracts(plan)
    if issues:
        raise PlanContractViolation(
            "methodology agent produced a plan that violates role contracts:\n  - "
            + "\n  - ".join(issues)
            + "\n\nRe-invoke the methodology agent or hand-edit the plan."
        )

    # Write the proposed plan to plan/proposed_<ts>.yaml in a shape
    # `framework plan create` accepts directly. We synthesize positional
    # placeholder IDs ("__t0", "__t1", ...) only for depends_on resolution
    # at create time; the actual IDs are assigned by the backend.
    target_repo = task.get("target_repo")
    cleaned_tasks = [
        _strip_to_taskspec(t, i, default_working_dir=target_repo)
        for i, t in enumerate(plan["tasks"])
    ]

    plan_dir = ctx.paths.plan_dir
    plan_dir.mkdir(parents=True, exist_ok=True)
    proposed_path = plan_dir / f"proposed_{_utc_ts()}.yaml"
    proposed_path.write_text(
        yaml.safe_dump(
            {"tasks": cleaned_tasks, "rationale": plan.get("rationale", "")},
            sort_keys=False, default_flow_style=False,
        ),
        encoding="utf-8",
    )

    ctx.log_action("framework_subagent_invoke", {
        "role": role,
        "model": result.model,
        "tasks": len(cleaned_tasks),
        "proposed_path": str(proposed_path),
    })

    output = {
        "role": role,
        "proposed_plan_path": str(proposed_path),
        "rationale": plan.get("rationale", ""),
        "tasks": [
            {
                "position": i,
                "agent_role": t["agent_role"],
                "recommended_model": t["recommended_model"],
                "depends_on_positions": plan["tasks"][i].get("depends_on", []),
                "priority": t["priority"],
                "goal_text": t["goal_text"],
                "rationale": plan["tasks"][i].get("rationale", ""),
            }
            for i, t in enumerate(cleaned_tasks)
        ],
        "stats": {
            "model": result.model,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": round(result.cost_usd, 6),
            "duration_seconds": round(result.duration_seconds, 3),
        },
        "next_step": (
            "Review the plan with the user. When aligned, run "
            f"`framework plan create {proposed_path}`. Each task will then "
            "land in `before_gate` for individual approval."
        ),
    }
    ctx.stdout.write(yaml.safe_dump(output, sort_keys=False, default_flow_style=False))
    return 0
