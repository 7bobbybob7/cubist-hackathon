"""Pod worker loop.

Section 13 of the methodology: ``claim → run → submit``. Boring and
reliable. The interesting bits — retries on transient API errors — are
delegated to the Anthropic SDK's built-in ``max_retries``, which is
configured when ``build_anthropic_client`` is called.

Phase 5 changes:
- The system prompt is now ``framework-state/agents/<role>.md``
  (fetched fresh per task, per Section 10), not the hardcoded one.
- The user message includes the rolling summary and any input
  artifacts the task spec references.
- The pod validates the model's output type against the agent's
  ``output_artifact_contract`` and files a FailureReport on mismatch.
- ``submit_result`` triggers a per-agent ProgressLogEntry on the
  backend side.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from framework.pod.anthropic_call import CallResult, call_messages
from framework.pod.backend_client import BackendClient
from framework.pod.prompt import (
    build_pod_prompt, contract_types, parse_artifact_content,
    primary_artifact_type,
)

log = logging.getLogger(__name__)


AnthropicCaller = Callable[..., CallResult]


def _classify_failure(exc: BaseException) -> str:
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "rate" in name or "api" in name or "status" in name or "connection" in name:
        return "api_error"
    return "logic_error"


def _resolve_model(
    task: dict[str, Any],
    frontmatter: dict[str, Any],
    config: dict[str, Any],
) -> str:
    return (
        task.get("recommended_model")
        or frontmatter.get("default_model")
        or config["models"]["sonnet"]
    )


class ContractViolation(Exception):
    """Raised when the model's output type isn't in the agent's contract."""


def _validate_contract(
    artifact_type: str, frontmatter: dict[str, Any], role: str,
) -> None:
    contract = contract_types(frontmatter)
    if not contract:
        # Empty contract = unconstrained (no validation).
        return
    # ProgressLogEntry is synthesized by the framework, not submitted by
    # the pod, so it's allowed to be absent from the pod's output.
    submittable = [t for t in contract if t != "ProgressLogEntry"]
    if not submittable:
        return
    if artifact_type not in submittable:
        raise ContractViolation(
            f"role {role!r} contract is {submittable!r}; "
            f"task asked for {artifact_type!r}, which is not allowed."
        )


def process_one_task(
    pod_id: str,
    *,
    backend: BackendClient,
    anthropic_caller: AnthropicCaller,
    config: dict[str, Any],
) -> str:
    """Process at most one task. Returns one of:
       ``"idle"``  — no ready task to claim
       ``"done"``  — task claimed, executed, submitted, after_gate emitted
       ``"failed"`` — task claimed but the API call failed, FailureReport submitted
    """
    task = backend.claim(pod_id)
    if task is None:
        return "idle"

    task_id = task["task_id"]
    role = task["agent_role"]
    log.info("claimed task %s (role=%s)", task_id, role)

    try:
        backend.mark_running(task_id)
    except Exception:
        log.exception("mark_running failed for %s — skipping", task_id)
        return "failed"

    # Load agent .md fresh, plus rolling summary and input artifacts.
    try:
        agent_md = backend.get_agent_config(role)
    except Exception as e:
        log.exception("could not load agent config for role %s", role)
        try:
            backend.report_failure(
                task_id, f"agent config missing for role {role!r}: {e}",
                failure_mode="logic_error",
            )
        except Exception:
            log.exception("report_failure also failed for %s", task_id)
        return "failed"

    rolling_summary = ""
    try:
        rolling_summary = backend.get_summary()
    except Exception:
        log.warning("could not fetch rolling summary; continuing without it")

    input_artifacts: list[dict[str, Any]] = []
    for aid in task.get("input_artifact_ids", []) or []:
        try:
            input_artifacts.append(backend.get_artifact(aid))
        except Exception:
            log.warning("could not fetch input artifact %s; skipping", aid)

    # Up-front contract validation: if the task asks for an artifact type
    # the role isn't allowed to produce, fail before burning tokens.
    primary_type = primary_artifact_type(task)
    try:
        # Re-parse here so we have frontmatter for both validation and prompt.
        from framework.pod.prompt import parse_agent_md
        frontmatter, _ = parse_agent_md(agent_md)
        _validate_contract(primary_type, frontmatter, role)
    except ContractViolation as e:
        log.error("contract violation for task %s: %s", task_id, e)
        try:
            backend.report_failure(
                task_id, str(e), failure_mode="logic_error",
            )
        except Exception:
            log.exception("report_failure failed")
        return "failed"

    # Build prompt and call the model.
    try:
        system, user, _ = build_pod_prompt(
            agent_md_text=agent_md,
            task=task,
            rolling_summary=rolling_summary,
            input_artifacts=input_artifacts,
        )
        model = _resolve_model(task, frontmatter, config)
        result = anthropic_caller(
            model=model,
            system=system,
            user=user,
            max_tokens=config["pod"]["max_tokens"],
            pricing=config["pricing"],
        )
    except Exception as e:
        log.exception("API call failed for %s", task_id)
        try:
            backend.report_failure(
                task_id, str(e), failure_mode=_classify_failure(e),
                retry_count=config.get("retries", {}).get("per_call", 3),
            )
        except Exception:
            log.exception("report_failure also failed for %s", task_id)
        return "failed"

    artifact = {
        "artifact_type": primary_type,
        "produced_by_task": task_id,
        "produced_by_agent": role,
        "tokens_in": result.input_tokens,
        "tokens_out": result.output_tokens,
        "cost_usd": result.cost_usd,
        "duration_seconds": result.duration_seconds,
        "model": result.model,
        "content": parse_artifact_content(result.text),
    }
    backend.submit_result(task_id, {
        "artifacts": [artifact],
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cost_usd": result.cost_usd,
        "duration_seconds": result.duration_seconds,
        "model": result.model,
    })
    log.info("submitted task %s (model=%s cost=$%.4f dur=%.2fs)",
             task_id, result.model, result.cost_usd, result.duration_seconds)
    return "done"


def pod_loop(
    pod_id: str,
    *,
    backend: BackendClient,
    anthropic_caller: AnthropicCaller,
    config: dict[str, Any],
    sleep_fn: Callable[[float], None] = time.sleep,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Run the pod forever (or until ``should_stop()`` returns True)."""
    backend.register_pod(pod_id)
    log.info("pod %s registered, entering loop", pod_id)
    idle_sleep = float(config["pod"]["idle_sleep_seconds"])

    while not should_stop():
        status = process_one_task(
            pod_id,
            backend=backend,
            anthropic_caller=anthropic_caller,
            config=config,
        )
        if status == "idle" and not should_stop():
            sleep_fn(idle_sleep)
