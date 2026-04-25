"""Pod tool definitions: filesystem read/write and bash, sandboxed to working_dir.

The agent .md frontmatter's ``allowed_tools`` decides which tools are
exposed to the model for a given task. Mapping:

    filesystem_read  -> read_file
    filesystem_write -> write_file
    bash, code_execution -> bash

All paths are resolved relative to ``working_dir`` and must stay inside it.
``bash`` runs with ``cwd=working_dir`` and a hard timeout. The shell is not
hermetic — a sufficiently motivated model can ``cd ..`` out — but the
default cwd plus the per-call timeout is enough for a development loop.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable


_BASH_TIMEOUT_SECONDS = 60
_READ_MAX_BYTES = 200_000


def _resolve_inside(working_dir: Path, requested: str) -> Path:
    """Resolve ``requested`` relative to ``working_dir`` and assert it
    stays inside. Raises ValueError on escape."""
    if not requested:
        raise ValueError("path must be non-empty")
    base = working_dir.resolve()
    candidate = (base / requested).resolve() if not Path(requested).is_absolute() \
        else Path(requested).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as e:
        raise ValueError(
            f"path {requested!r} resolves outside working_dir {str(base)!r}"
        ) from e
    return candidate


_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "write_file": {
        "name": "write_file",
        "description": (
            "Write text content to a file inside the working directory. "
            "Overwrites if the file exists. Creates parent directories. "
            "Path may be absolute (must stay inside working_dir) or relative."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target file path"},
                "content": {"type": "string", "description": "Full file contents"},
            },
            "required": ["path", "content"],
        },
    },
    "read_file": {
        "name": "read_file",
        "description": (
            "Read a UTF-8 text file inside the working directory. "
            "Returns up to ~200KB."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File to read"},
            },
            "required": ["path"],
        },
    },
    "bash": {
        "name": "bash",
        "description": (
            "Run a shell command with cwd set to the working directory. "
            f"Hard timeout {_BASH_TIMEOUT_SECONDS}s. Returns stdout + stderr "
            "and the exit code. Use for running tests, listing files, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
            },
            "required": ["command"],
        },
    },
}


def _allowed_tool_names(allowed: list[str] | None) -> list[str]:
    """Map agent.md ``allowed_tools`` entries to concrete tool names."""
    out: list[str] = []
    if not allowed:
        return out
    a = {str(x).lower() for x in allowed}
    if "filesystem_read" in a:
        out.append("read_file")
    if "filesystem_write" in a:
        out.append("write_file")
    if "bash" in a or "code_execution" in a:
        out.append("bash")
    return out


def build_tools(
    frontmatter: dict[str, Any],
    working_dir: str | Path | None,
) -> tuple[list[dict[str, Any]], Callable[[str, dict[str, Any]], dict[str, Any]] | None]:
    """Return (tool_schemas, handler) for the role's allowed tools.

    Returns ``([], None)`` when the role has no tools or no working_dir is
    configured — the caller should then use the plain (non-agentic)
    messages flow.
    """
    if working_dir is None:
        return [], None
    wd = Path(str(working_dir))
    if not wd.exists() or not wd.is_dir():
        return [], None

    names = _allowed_tool_names(frontmatter.get("allowed_tools"))
    if not names:
        return [], None
    schemas = [_TOOL_SCHEMAS[n] for n in names]

    def handler(name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "write_file":
                target = _resolve_inside(wd, str(args.get("path", "")))
                target.parent.mkdir(parents=True, exist_ok=True)
                content = args.get("content", "")
                if not isinstance(content, str):
                    content = str(content)
                target.write_text(content, encoding="utf-8")
                return {"ok": True, "bytes_written": len(content.encode("utf-8"))}
            if name == "read_file":
                target = _resolve_inside(wd, str(args.get("path", "")))
                data = target.read_bytes()[:_READ_MAX_BYTES]
                return {"ok": True, "content": data.decode("utf-8", errors="replace")}
            if name == "bash":
                cmd = str(args.get("command", ""))
                if not cmd.strip():
                    return {"ok": False, "error": "empty command"}
                try:
                    proc = subprocess.run(
                        cmd, shell=True, cwd=str(wd),
                        capture_output=True, text=True,
                        timeout=_BASH_TIMEOUT_SECONDS,
                    )
                except subprocess.TimeoutExpired:
                    return {
                        "ok": False,
                        "error": f"timeout after {_BASH_TIMEOUT_SECONDS}s",
                    }
                # Truncate to keep tool_result tokens bounded.
                stdout = (proc.stdout or "")[-8000:]
                stderr = (proc.stderr or "")[-4000:]
                return {
                    "ok": proc.returncode == 0,
                    "exit_code": proc.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                }
            return {"ok": False, "error": f"unknown tool {name!r}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return schemas, handler
