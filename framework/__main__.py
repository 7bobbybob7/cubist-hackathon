"""CLI entry point for the framework.

Dispatches three kinds of subcommand:
- ``admin``: backend / initdb / start-pod (process lifecycle)
- ``cli``:   the Section 5.2 framework tools the parent calls
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _default_state_dir() -> str:
    return os.environ.get("FRAMEWORK_STATE_DIR", "./framework-state")


def _default_backend_url() -> str:
    return os.environ.get("FRAMEWORK_BACKEND_URL", "http://127.0.0.1:8765")


def _pod_api_key_env(pod_id: str) -> str:
    """Convention: ``ANTHROPIC_API_KEY_POD_<ID>`` per pod.

    ``pod_a`` → ``ANTHROPIC_API_KEY_POD_A``, ``pod_b`` →
    ``ANTHROPIC_API_KEY_POD_B``, etc. Each pod gets its own key so
    runaway spend can be traced (and rate-limited) per pod.
    """
    suffix = pod_id.upper()
    if suffix.startswith("POD_"):
        suffix = suffix[len("POD_"):]
    return f"ANTHROPIC_API_KEY_POD_{suffix}"


def _run_admin(args) -> int:
    state_dir = args.state_dir or _default_state_dir()
    if args._name == "backend":
        import uvicorn
        from framework.api.app import create_app
        app = create_app(state_dir)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
    if args._name == "initdb":
        from framework.db import init_db
        from framework.state import StatePaths
        paths = StatePaths(state_dir)
        paths.ensure()
        init_db(paths.db)
        print(f"initialized {paths.db}")
        return 0
    if args._name == "start-pod":
        from framework.pod.__main__ import main as pod_main
        api_key_env = args.api_key_env or _pod_api_key_env(args.pod_id)
        return pod_main([
            args.pod_id,
            "--state-dir", state_dir,
            "--backend-url", args.backend_url or _default_backend_url(),
            "--api-key-env", api_key_env,
        ])
    raise SystemExit(f"unknown admin command: {args._name}")


def _run_cli(args) -> int:
    from framework.cli._context import CliContext
    from framework.pod.backend_client import BackendClient
    from framework.state import StatePaths

    state_dir = args.state_dir or _default_state_dir()
    backend_url = args.backend_url or _default_backend_url()

    paths = StatePaths(state_dir)

    # `run start` bootstraps the state dir, so we tolerate it missing.
    is_run_start = getattr(args, "run_cmd", None) == "start"
    if not is_run_start:
        paths.ensure()  # idempotent

    backend = BackendClient(base_url=backend_url)
    ctx = CliContext(backend=backend, paths=paths)
    try:
        return args.func(ctx, args)
    finally:
        backend.close()


def main(argv: list[str] | None = None) -> int:
    from framework.cli.parser import build_parser
    args = build_parser().parse_args(argv)
    if getattr(args, "_kind", None) == "admin":
        return _run_admin(args)
    return _run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
