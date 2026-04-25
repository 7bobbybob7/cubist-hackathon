"""Load framework-state/config.yaml with defaults.

Per Section 18 of the methodology, plus a ``pricing`` section so cost
computation isn't wired into call sites.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "budget": {
        "daily_cap_usd": 50.00,
    },
    "models": {
        "haiku": "claude-haiku-4-5-20251001",
        "sonnet": "claude-sonnet-4-6",
        "opus": "claude-opus-4-7",
        "methodology_default": "claude-opus-4-7",
    },
    "rolling_summary": {
        "max_tokens": 2000,
    },
    "retries": {
        "per_call": 3,
    },
    # Per-MTok USD. Cache reads ≈ 0.1× input price; cache writes ≈ 1.25×.
    "pricing": {
        "claude-opus-4-7":              {"input": 5.00, "output": 25.00},
        "claude-opus-4-6":              {"input": 5.00, "output": 25.00},
        "claude-sonnet-4-6":            {"input": 3.00, "output": 15.00},
        "claude-haiku-4-5":             {"input": 1.00, "output": 5.00},
        "claude-haiku-4-5-20251001":    {"input": 1.00, "output": 5.00},
    },
    "pod": {
        "max_tokens": 4096,
        "idle_sleep_seconds": 2.0,
    },
}


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load config.yaml if present, merged with defaults. Missing file is OK."""
    if path is None:
        return dict(DEFAULT_CONFIG)
    p = Path(path)
    if not p.exists():
        return dict(DEFAULT_CONFIG)
    with p.open("r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}
    return _deep_merge(DEFAULT_CONFIG, user_cfg)


def write_default_config(path: str | Path) -> None:
    """Write the default config to ``path`` (used by ``framework run start``)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        yaml.safe_dump(DEFAULT_CONFIG, f, sort_keys=False, default_flow_style=False)
