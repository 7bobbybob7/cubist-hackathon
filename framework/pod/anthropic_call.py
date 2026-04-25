"""Make an Anthropic API call and meter it.

The caller is injected so tests can drop in a fake Anthropic client that
returns canned responses (or raises) without touching the network.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class CallResult:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    cost_usd: float
    duration_seconds: float
    model: str
    raw_stop_reason: str | None = None


class AnthropicLike(Protocol):
    """Minimal duck-typed interface we depend on. ``anthropic.Anthropic``
    satisfies it; tests use a small stand-in."""
    @property
    def messages(self) -> Any: ...


def compute_cost(model: str, usage: Any, pricing: dict[str, dict[str, float]]) -> float:
    p = pricing.get(model)
    if p is None:
        return 0.0
    in_rate = p["input"]
    out_rate = p["output"]
    in_tok = getattr(usage, "input_tokens", 0) or 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (
        in_tok        / 1_000_000 * in_rate
        + out_tok     / 1_000_000 * out_rate
        + cache_read  / 1_000_000 * in_rate * 0.1
        + cache_create / 1_000_000 * in_rate * 1.25
    )


def call_messages(
    client: AnthropicLike,
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int,
    pricing: dict[str, dict[str, float]],
) -> CallResult:
    """One Anthropic Messages API call, metered.

    The system prompt is wrapped in a ``cache_control: ephemeral`` block so
    the agent role description gets cached across tasks of the same role.
    Retries on transient 429/5xx are handled by the SDK (we set
    ``max_retries`` when building the client).
    """
    t0 = time.monotonic()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user}],
    )
    dt = time.monotonic() - t0

    text_parts = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text_parts.append(block.text)
    text = "".join(text_parts)

    usage = response.usage
    return CallResult(
        text=text,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        cost_usd=compute_cost(model, usage, pricing),
        duration_seconds=dt,
        model=model,
        raw_stop_reason=getattr(response, "stop_reason", None),
    )


def build_anthropic_client(api_key: str, *, max_retries: int = 3):
    """Build a real anthropic.Anthropic client. Imported lazily so unit
    tests don't need the SDK at import time."""
    import anthropic
    return anthropic.Anthropic(api_key=api_key, max_retries=max_retries)
