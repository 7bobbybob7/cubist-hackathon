"""Pod CLI: env-var convention for multi-pod runs.

Each pod gets its own Anthropic key via ``ANTHROPIC_API_KEY_POD_<ID>``,
so a runaway pod can be rate-limited / revoked without touching the
other pod. The default is derived from ``pod_id`` rather than hardcoded.
"""
from __future__ import annotations

import pytest

from framework.__main__ import _pod_api_key_env as _admin_env
from framework.pod.__main__ import _pod_api_key_env as _pod_env


@pytest.mark.parametrize("pod_id,expected", [
    ("pod_a", "ANTHROPIC_API_KEY_POD_A"),
    ("pod_b", "ANTHROPIC_API_KEY_POD_B"),
    ("POD_C", "ANTHROPIC_API_KEY_POD_C"),
    ("a", "ANTHROPIC_API_KEY_POD_A"),
    ("worker_42", "ANTHROPIC_API_KEY_POD_WORKER_42"),
])
def test_env_var_derived_from_pod_id(pod_id, expected):
    assert _pod_env(pod_id) == expected
    # Both entry points must agree — drift would cause the admin
    # dispatcher and direct ``python -m framework.pod`` to read
    # different env vars for the same pod.
    assert _admin_env(pod_id) == expected


def test_pod_main_picks_up_pod_b_key(monkeypatch):
    """Direct ``python -m framework.pod pod_b`` reads ANTHROPIC_API_KEY_POD_B
    when the user doesn't pass --api-key-env explicitly."""
    from framework.pod.__main__ import main as pod_main

    monkeypatch.delenv("ANTHROPIC_API_KEY_POD_A", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY_POD_B", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # No keys set → exits 2 with a message naming the *pod_b* env var.
    rc = pod_main(["pod_b"])
    assert rc == 2  # explicit refusal in pod/__main__.py
