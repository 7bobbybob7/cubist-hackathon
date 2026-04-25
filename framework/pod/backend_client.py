"""HTTP client used by a pod to talk to the FastAPI backend.

Defined as a thin wrapper around an ``httpx.Client`` so tests can inject a
``fastapi.testclient.TestClient`` (which is httpx-compatible) instead of
spinning up a real network listener.
"""
from __future__ import annotations

from typing import Any

import httpx


class BackendError(RuntimeError):
    pass


class BackendClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        http_client: httpx.Client | None = None,
        timeout: float = 30.0,
    ):
        if http_client is not None:
            self._http = http_client
            self._owns_client = False
        else:
            if base_url is None:
                raise ValueError("must supply base_url or http_client")
            self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
            self._owns_client = True

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    # --- pod operations -------------------------------------------------

    def register_pod(self, pod_id: str) -> dict[str, Any]:
        r = self._http.post("/pods", json={"pod_id": pod_id})
        r.raise_for_status()
        return r.json()

    def claim(self, pod_id: str) -> dict[str, Any] | None:
        r = self._http.post(f"/pods/{pod_id}/claim")
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    def mark_running(self, task_id: str) -> dict[str, Any]:
        r = self._http.post(f"/tasks/{task_id}/start")
        r.raise_for_status()
        return r.json()

    def submit_result(self, task_id: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self._http.post(f"/tasks/{task_id}/submit", json=body)
        r.raise_for_status()
        return r.json()

    def report_failure(
        self, task_id: str, error_message: str,
        failure_mode: str = "logic_error", retry_count: int = 0,
    ) -> dict[str, Any]:
        r = self._http.post(f"/tasks/{task_id}/fail", json={
            "error_message": error_message,
            "failure_mode": failure_mode,
            "retry_count": retry_count,
        })
        r.raise_for_status()
        return r.json()

    # --- read helpers ---------------------------------------------------

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        r = self._http.get(f"/artifacts/{artifact_id}")
        r.raise_for_status()
        return r.json()

    def list_artifacts(
        self, *, type: str | None = None, task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if type:
            params["type"] = type
        if task_id:
            params["task_id"] = task_id
        r = self._http.get("/artifacts", params=params)
        r.raise_for_status()
        return r.json()

    def list_tasks(
        self, status: str | None = None, *, include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if include_archived:
            params["include_archived"] = "true"
        r = self._http.get("/tasks", params=params)
        r.raise_for_status()
        return r.json()

    def get_task(self, task_id: str) -> dict[str, Any]:
        r = self._http.get(f"/tasks/{task_id}")
        r.raise_for_status()
        return r.json()

    def list_events(
        self, *, limit: int = 100, task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if task_id:
            params["task_id"] = task_id
        r = self._http.get("/events", params=params)
        r.raise_for_status()
        return r.json()

    def get_state(self, *, recent_events: int = 10) -> dict[str, Any]:
        r = self._http.get("/state", params={"recent_events": recent_events})
        r.raise_for_status()
        return r.json()

    def get_summary(self) -> str:
        r = self._http.get("/summary")
        r.raise_for_status()
        return r.json().get("content", "")

    def get_agent_config(self, role: str) -> str:
        r = self._http.get(f"/agents/{role}")
        r.raise_for_status()
        return r.json().get("content", "")

    def db_query(
        self, sql: str, params: list[Any] | None = None,
    ) -> dict[str, Any]:
        r = self._http.post("/db/query", json={"sql": sql, "params": params or []})
        r.raise_for_status()
        return r.json()

    # --- write helpers --------------------------------------------------

    def create_task(
        self, spec: dict[str, Any], *, initial_status: str = "before_gate",
    ) -> dict[str, Any]:
        r = self._http.post(
            "/tasks", json=spec, params={"initial_status": initial_status},
        )
        r.raise_for_status()
        return r.json()

    def edit_task(self, task_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        r = self._http.patch(f"/tasks/{task_id}", json=fields)
        r.raise_for_status()
        return r.json()

    def approve_before(self, task_id: str) -> dict[str, Any]:
        r = self._http.post(f"/tasks/{task_id}/gate/before/approve")
        r.raise_for_status()
        return r.json()

    def reject_before(self, task_id: str, reason: str) -> dict[str, Any]:
        r = self._http.post(
            f"/tasks/{task_id}/gate/before/reject", json={"reason": reason},
        )
        r.raise_for_status()
        return r.json()

    def approve_after(self, task_id: str) -> dict[str, Any]:
        r = self._http.post(f"/tasks/{task_id}/gate/after/approve")
        r.raise_for_status()
        return r.json()

    def reject_after(self, task_id: str, reason: str) -> dict[str, Any]:
        r = self._http.post(
            f"/tasks/{task_id}/gate/after/reject", json={"reason": reason},
        )
        r.raise_for_status()
        return r.json()

    def requeue_task(self, task_id: str) -> dict[str, Any]:
        r = self._http.post(f"/tasks/{task_id}/requeue")
        r.raise_for_status()
        return r.json()

    def session_reset(self) -> dict[str, Any]:
        r = self._http.post("/session/reset")
        r.raise_for_status()
        return r.json()

    def update_summary(self, content: str) -> dict[str, Any]:
        r = self._http.post("/summary", json={"content": content})
        r.raise_for_status()
        return r.json()

    def record_parent_action(
        self, *, tool: str, args: dict[str, Any],
        result: str = "ok", caller: str = "parent",
    ) -> None:
        r = self._http.post("/parent_actions", json={
            "tool": tool, "args": args, "result": result, "caller": caller,
        })
        r.raise_for_status()
