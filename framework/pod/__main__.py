"""Pod CLI: ``python -m framework.pod <pod_id>``."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from framework.config import load_config
from framework.pod.anthropic_call import (
    build_anthropic_client, call_messages, call_messages_agentic,
)
from framework.pod.backend_client import BackendClient
from framework.pod.worker import pod_loop
from framework.state import StatePaths


def _pod_api_key_env(pod_id: str) -> str:
    suffix = pod_id.upper()
    if suffix.startswith("POD_"):
        suffix = suffix[len("POD_"):]
    return f"ANTHROPIC_API_KEY_POD_{suffix}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="framework.pod")
    p.add_argument("pod_id", help="e.g. pod_a, pod_b, ...")
    p.add_argument("--state-dir", default=os.environ.get("FRAMEWORK_STATE_DIR", "./framework-state"))
    p.add_argument("--backend-url", default=os.environ.get("FRAMEWORK_BACKEND_URL", "http://127.0.0.1:8765"))
    p.add_argument("--api-key-env", default=None,
                   help="env var name holding the Anthropic API key for this pod. "
                        "Defaults to ANTHROPIC_API_KEY_POD_<ID> (e.g. pod_b → "
                        "ANTHROPIC_API_KEY_POD_B), with fallback to ANTHROPIC_API_KEY.")
    args = p.parse_args(argv)
    if args.api_key_env is None:
        args.api_key_env = _pod_api_key_env(args.pod_id)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    )

    api_key = os.environ.get(args.api_key_env) or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.stderr.write(
            f"error: {args.api_key_env} (or ANTHROPIC_API_KEY) must be set\n"
        )
        return 2

    paths = StatePaths(args.state_dir)
    paths.ensure()
    config = load_config(paths.root / "config.yaml")
    retries = int(config.get("retries", {}).get("per_call", 3))

    anthropic_client = build_anthropic_client(api_key, max_retries=retries)

    def caller(**kwargs):
        if "tools" in kwargs:
            return call_messages_agentic(anthropic_client, **kwargs)
        return call_messages(anthropic_client, **kwargs)

    backend = BackendClient(base_url=args.backend_url)
    stop_event = threading.Event()

    def _shutdown(signum, _frame):
        logging.info("received signal %d, stopping after current task", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        pod_loop(
            args.pod_id,
            backend=backend,
            anthropic_caller=caller,
            config=config,
            should_stop=stop_event.is_set,
        )
    finally:
        backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
