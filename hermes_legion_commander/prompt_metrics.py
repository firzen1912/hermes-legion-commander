"""Measurable prompt-effectiveness metrics so Hermes can optimize its prompting.

Two concerns live here, both pure and unit-testable:

* Subagent observability. Workers are authorized (in the prompt contract) to spawn
  up to a small number of weaker, cheaper subagents to parallelize grunt work
  while keeping token/API usage low. They report what they spawned in a
  ``SUBAGENTS:`` block; ``extract_subagent_report`` parses that into a measured
  count, flags any breach of the cap, and captures the task lines. This measures
  adherence; the cap itself is enforced by the contract the worker receives.

* Prompt effectiveness. ``prompt_effectiveness`` aggregates recorded stage events
  into per-role measurable signals -- pass rate, input/output tokens, an
  output-per-input-token efficiency ratio, cost, subagent utilization, and
  retry/failover counts -- so the supervisor can see which prompts produce
  efficient, passing work and which waste tokens or fail, and adjust accordingly.
"""
from __future__ import annotations

import re
from typing import Any

SUBAGENT_CAP = 5

_SUBAGENT_HEADER = re.compile(r"^[ \t]*SUBAGENTS?[ \t]*:[ \t]*(.*)$", re.IGNORECASE | re.MULTILINE)
_LEADING_INT = re.compile(r"(\d+)")
_STAGE_ROLE = re.compile(r"(?:^|/)(?:v\d+/)?(?:\d+[-_])?([A-Za-z][A-Za-z0-9_-]*)")


def extract_subagent_report(output: str, cap: int = SUBAGENT_CAP) -> dict[str, Any]:
    """Parse a worker's reported subagent usage from a ``SUBAGENTS:`` block.

    Returns ``reported`` (whether the worker emitted the block), ``spawned`` (the
    count it reported, or the number of task bullets if no explicit number),
    ``over_cap`` (spawned exceeds ``cap``), ``cap``, and ``tasks`` (the bullet
    lines under the header). Absent block -> a zeroed, not-reported result.
    """
    if not output:
        return {"reported": False, "spawned": 0, "over_cap": False, "cap": cap, "tasks": []}
    match = _SUBAGENT_HEADER.search(output)
    if not match:
        return {"reported": False, "spawned": 0, "over_cap": False, "cap": cap, "tasks": []}

    header_remainder = match.group(1).strip()
    explicit = _LEADING_INT.search(header_remainder)
    spawned: int | None = int(explicit.group(1)) if explicit else None
    if header_remainder.lower() in {"none", "0", "no subagents", "n/a"}:
        spawned = 0

    # Collect bullet/task lines immediately following the header.
    tasks: list[str] = []
    lines = output[match.end():].splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if tasks:
                break
            continue
        if stripped[0] in "-*•" or re.match(r"^\d+[.)]\s", stripped):
            tasks.append(re.sub(r"^[-*•\d.)]+\s*", "", stripped))
        elif tasks:
            break
        else:
            break

    if spawned is None:
        spawned = len(tasks)
    spawned = max(0, spawned)
    return {
        "reported": True,
        "spawned": spawned,
        "over_cap": spawned > cap,
        "cap": cap,
        "tasks": tasks[:50],
    }


def _role_of(stage: str) -> str:
    """Derive a role bucket from a stage path like ``v51/06-security-assurance``."""
    if not stage:
        return "unknown"
    tail = stage.replace("\\", "/").split("/")[-1]
    m = _STAGE_ROLE.search(tail)
    role = m.group(1) if m else tail
    return role or "unknown"


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _is_pass(event: dict[str, Any]) -> bool:
    quality = event.get("quality_signals") if isinstance(event.get("quality_signals"), dict) else {}
    status = str(quality.get("status", "")).upper()
    return status in {"PASS", "PASSED", "OK", "GREEN"}


def prompt_effectiveness(events: list[dict[str, Any]], cap: int | None = None) -> dict[str, Any]:
    """Aggregate stage events into per-role measurable signals for optimization."""
    if cap is None:
        # Reflect the cap actually used at recording time (configurable per run).
        cap = SUBAGENT_CAP
        for event in events:
            sub = event.get("subagents")
            if isinstance(sub, dict) and isinstance(sub.get("cap"), int):
                cap = sub["cap"]
                break
    by_role: dict[str, dict[str, Any]] = {}
    totals = {
        "stage_count": 0,
        "pass_count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "subagents_spawned": 0,
        "subagent_cap_breaches": 0,
        "retries": 0,
        "failovers": 0,
    }

    for event in events:
        role = _role_of(str(event.get("stage", "")))
        meta = event.get("runtime_metadata") if isinstance(event.get("runtime_metadata"), dict) else {}
        usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
        prompt_metrics = event.get("prompt_metrics") if isinstance(event.get("prompt_metrics"), dict) else {}
        sub = event.get("subagents") if isinstance(event.get("subagents"), dict) else {}

        input_tokens = _int(prompt_metrics.get("estimated_tokens"))
        output_tokens = _int(usage.get("output_tokens"))
        total_tokens = _int(usage.get("total_tokens"))
        cost = float(usage.get("cost_usd", 0.0) or 0.0)
        spawned = _int(sub.get("spawned"))
        breach = 1 if sub.get("over_cap") else 0
        attempts = _int(meta.get("attempts"))
        retries = max(0, attempts - 1) if attempts else 0
        failovers = len(meta.get("failovers")) if isinstance(meta.get("failovers"), list) else 0
        passed = _is_pass(event)

        bucket = by_role.setdefault(role, {
            "stage_count": 0, "pass_count": 0,
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0,
            "subagents_spawned": 0, "subagent_cap_breaches": 0, "retries": 0, "failovers": 0,
        })
        for key, val in (
            ("stage_count", 1), ("pass_count", int(passed)),
            ("input_tokens", input_tokens), ("output_tokens", output_tokens),
            ("total_tokens", total_tokens), ("subagents_spawned", spawned),
            ("subagent_cap_breaches", breach), ("retries", retries), ("failovers", failovers),
        ):
            bucket[key] += val
            totals[key] += val
        bucket["cost_usd"] = round(bucket["cost_usd"] + cost, 6)
        totals["cost_usd"] = round(totals["cost_usd"] + cost, 6)

    for bucket in by_role.values():
        _finalize(bucket)
    _finalize(totals)

    return {
        "schema_version": 1,
        "subagent_cap": cap,
        "totals": totals,
        "by_role": by_role,
    }


def _finalize(bucket: dict[str, Any]) -> None:
    """Add derived ratios that the supervisor optimizes against."""
    n = bucket.get("stage_count", 0) or 0
    inp = bucket.get("input_tokens", 0) or 0
    out = bucket.get("output_tokens", 0) or 0
    bucket["pass_rate"] = round(bucket.get("pass_count", 0) / n, 4) if n else None
    # Output value produced per input token spent: a terseness/efficiency signal.
    bucket["output_per_input_token"] = round(out / inp, 4) if inp else None
    bucket["avg_total_tokens"] = round(bucket.get("total_tokens", 0) / n, 1) if n else None
    bucket["avg_cost_usd"] = round(bucket.get("cost_usd", 0.0) / n, 6) if n else None
    bucket["avg_subagents"] = round(bucket.get("subagents_spawned", 0) / n, 2) if n else None
