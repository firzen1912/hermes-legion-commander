"""Direct CLI worker runtime and provider-neutral shared context.

Codex CLI and Claude Code cannot share private provider conversation state. This
module gives them a durable common memory instead: every stage receives the same
campaign brief, recent stage summaries, artifact index, decisions, and Git
snapshot. The context is supervisor-owned and must remain read-only to workers.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any

try:
    # Re-exported as part of worker_runtime's public surface (used via
    # worker_runtime.reconcile_usage by callers and tests); keep despite no in-module use.
    from .token_cost import build_prompt_preflight, reconcile_usage, append_jsonl  # noqa: F401
    from .prompt_metrics import extract_subagent_report, prompt_effectiveness
except ImportError:  # Support direct file loading in isolated validation fixtures.
    from hermes_legion_commander.token_cost import build_prompt_preflight, reconcile_usage, append_jsonl  # noqa: F401
    from hermes_legion_commander.prompt_metrics import extract_subagent_report, prompt_effectiveness

try:
    from .repo_graph import quick_repo_facts, refresh_repo_intelligence
except ImportError:  # Support direct file loading in isolated validation fixtures.
    from hermes_legion_commander.repo_graph import quick_repo_facts, refresh_repo_intelligence

try:
    from .anchored_truth import refresh_anchored_truth
except ImportError:  # Support direct file loading in isolated validation fixtures.
    from hermes_legion_commander.anchored_truth import refresh_anchored_truth

UTC = dt.timezone.utc

RUNTIME_EXECUTABLES = {
    "codex-cli": "codex",
    "claude-code": "claude",
}

ENTITLEMENT_MARKERS = (
    "third-party apps now draw",
    "add more at claude.ai/settings/usage",
    "billing entitlement",
    "payment required",
    "insufficient credits",
)

AUTH_MARKERS = (
    "invalid api key",
    "authentication required",
    "unauthorized",
    "forbidden",
    "login required",
)

NON_RETRYABLE_MARKERS = (
    "invalid api key",
    "authentication required",
    "unauthorized",
    "forbidden",
    "profile does not exist",
    "model not found",
    "invalid model",
    "third-party apps now draw",
    "add more at claude.ai/settings/usage",
    "billing entitlement",
    "payment required",
    "insufficient credits",
)

QUOTA_MARKERS = (
    "quota",
    "rate limit",
    "rate_limit",
    "resource_exhausted",
    "too many requests",
    "usage limit",
    "capacity",
    "retry after",
    "resets at",
    "try again later",
    "429",
)

ERROR_MARKERS = (
    "invalid_request_error",
    "non-retryable error",
    "provider error",
    "http 400",
    "http 401",
    "http 403",
    "error code:",
)

TOKEN_FIELD_ALIASES = {
    "input_tokens": "input_tokens",
    "prompt_tokens": "input_tokens",
    "cached_input_tokens": "cached_input_tokens",
    "cache_read_input_tokens": "cached_input_tokens",
    "cache_creation_input_tokens": "cache_creation_input_tokens",
    "output_tokens": "output_tokens",
    "completion_tokens": "output_tokens",
    "reasoning_tokens": "reasoning_tokens",
    "total_tokens": "total_tokens",
}

COST_FIELD_ALIASES = {
    "cost_usd": "cost_usd",
    "total_cost_usd": "cost_usd",
}

QUALITY_STATUSES = ("PASS", "BLOCKED", "NEEDS_HUMAN", "RUNNING", "QUOTA_PAUSED", "FAILED")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp-{os.getpid()}-{uuid.uuid4().hex[:12]}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_json(path: Path, payload: Any) -> None:
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def find_run_root(stage_dir: Path) -> Path:
    resolved = stage_dir.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / "job.json").is_file() or (candidate / "manifest.json").is_file():
            return candidate
    return stage_dir.parent.resolve()


def _git(repo: Path, *args: str) -> str:
    try:
        cp = subprocess.run(
            ["git", *args], cwd=repo, text=True, capture_output=True, check=False, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return cp.stdout.strip() if cp.returncode == 0 else ""


def git_snapshot(repo: Path) -> dict[str, Any]:
    if not repo.is_dir():
        return {"repo": str(repo), "available": False}
    return {
        "repo": str(repo.resolve()),
        "available": True,
        "head": _git(repo, "log", "-1", "--oneline"),
        "branch": _git(repo, "branch", "--show-current"),
        "status": _git(repo, "status", "--short"),
        "diff_stat": _git(repo, "diff", "--stat"),
        "diff_numstat": _git(repo, "diff", "--numstat"),
    }


def context_dir_for(stage_dir: Path) -> Path:
    return find_run_root(stage_dir) / "shared-context"


def _event_files(context_dir: Path) -> list[Path]:
    events = context_dir / "events"
    if not events.is_dir():
        return []
    return sorted(path for path in events.glob("*.json") if path.is_file())


def _load_events(context_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _event_files(context_dir):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    rows.sort(key=lambda row: (str(row.get("completed_at", "")), str(row.get("event_id", ""))))
    return rows


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip().replace(",", "")
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _walk_json(value: Any) -> list[tuple[str, Any]]:
    pairs: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            pairs.append((str(key), child))
            pairs.extend(_walk_json(child))
    elif isinstance(value, list):
        for child in value:
            pairs.extend(_walk_json(child))
    return pairs


def observed_usage_from_objects(objects: list[Any]) -> dict[str, Any]:
    """Extract best-effort token and cost fields from vendor JSON payloads.

    Provider CLI output formats change over time. The ledger intentionally stores
    these as observed fields rather than billing-grade totals: repeated cumulative
    fields are de-duplicated by taking the largest observed value for each metric.
    """
    observed: dict[str, list[float]] = {}
    source_fields: dict[str, set[str]] = {}
    for obj in objects:
        for raw_key, value in _walk_json(obj):
            key = raw_key.casefold()
            canonical = TOKEN_FIELD_ALIASES.get(key) or COST_FIELD_ALIASES.get(key)
            if canonical is None:
                continue
            number = _as_number(value)
            if number is None:
                continue
            observed.setdefault(canonical, []).append(number)
            source_fields.setdefault(canonical, set()).add(raw_key)
    metrics: dict[str, Any] = {}
    for canonical, values in observed.items():
        if not values:
            continue
        value = max(values)
        metrics[canonical] = int(value) if canonical.endswith("tokens") else value
    if "total_tokens" not in metrics:
        token_parts = [
            int(metrics.get("input_tokens", 0) or 0),
            int(metrics.get("output_tokens", 0) or 0),
            int(metrics.get("reasoning_tokens", 0) or 0),
        ]
        total = sum(token_parts)
        if total:
            metrics["total_tokens"] = total
    if metrics:
        metrics["source_fields"] = {key: sorted(value) for key, value in source_fields.items()}
    return metrics


def observed_usage_from_text(text: str) -> dict[str, Any]:
    objects: list[Any] = []
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        objects.append(json.loads(stripped))
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                objects.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return observed_usage_from_objects(objects)


def _status_from_text(text: str, returncode: int | None = None) -> str:
    normalized = text.upper()
    for status in QUALITY_STATUSES:
        if re.search(rf"(?m)^\s*(?:STATUS\s*[:=-]\s*)?{status}\b", normalized):
            return status
    if returncode not in (None, 0):
        return "FAILED"
    return "UNKNOWN"


def _version_mentions(text: str) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for match in re.finditer(r"\bv(\d+(?:\.\d+)?[A-Za-z]?)\b", text, flags=re.IGNORECASE):
        value = "v" + match.group(1).lower()
        if value not in seen:
            seen.add(value)
            values.append(value)
    return values[:50]


def _quality_signals(prompt: str, output: str, metadata: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    returncode = metadata.get("returncode")
    returncode_int = int(returncode) if isinstance(returncode, int | float) else None
    status = _status_from_text(output, returncode_int)
    prompt_versions = _version_mentions(prompt)
    output_versions = _version_mentions(output)
    overlap = [value for value in prompt_versions if value in set(output_versions)]
    output_lower = output.lower()
    check_signal = any(word in output_lower for word in ("pytest", "test", "checks", "passed", "failed"))
    risk_signal = any(word in output_lower for word in ("risk", "blocker", "compromise", "needs human", "unresolved"))
    changed_files_signal = bool(str(snapshot.get("status", "")).strip() or str(snapshot.get("diff_stat", "")).strip())
    score = 0.0
    if status == "PASS":
        score += 0.35
    elif status in {"BLOCKED", "NEEDS_HUMAN", "QUOTA_PAUSED"}:
        score += 0.15
    elif status == "UNKNOWN" and returncode_int == 0:
        score += 0.10
    if prompt_versions:
        score += 0.20 if overlap else 0.0
    else:
        score += 0.10
    if check_signal:
        score += 0.20
    if changed_files_signal:
        score += 0.15
    if risk_signal:
        score += 0.10
    return {
        "status": status,
        "requested_versions": prompt_versions,
        "reported_versions": output_versions,
        "version_overlap": overlap,
        "mentions_checks_or_tests": check_signal,
        "mentions_risks_or_blockers": risk_signal,
        "changed_files_observed": changed_files_signal,
        "quality_signal_score": round(min(score, 1.0), 3),
        "note": "Deterministic evidence signal, not ground-truth correctness. Use reviewer verdicts and validation results as authority.",
    }


def _roadmap_candidates(repo: Path) -> list[Path]:
    candidates: list[Path] = []
    for relative in (Path("request/roadmap.md"), Path("docs/roadmap.md"), Path("roadmap.md")):
        path = repo / relative
        if path.is_file():
            candidates.append(path)
    docs = repo / "docs"
    if docs.is_dir():
        for path in sorted(docs.rglob("*roadmap*.md")):
            if path.is_file() and path not in candidates:
                candidates.append(path)
    return candidates[:12]


def roadmap_snapshot(repo: Path, max_chars: int = 1200) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in _roadmap_candidates(repo):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        rows.append({
            "path": str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path),
            "sha256": sha256_text(text),
            "chars": len(text),
            "versions": _version_mentions(text),
            "excerpt": text[:max_chars],
        })
    return {"available": bool(rows), "roadmaps": rows}


def _event_usage(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("runtime_metadata", {})
    if not isinstance(metadata, dict):
        return {}
    usage = metadata.get("usage") or metadata.get("observed_usage") or {}
    return usage if isinstance(usage, dict) else {}


EFFORT_RANK = {"low": 0, "medium": 1, "high": 2}
EFFORT_BY_RANK = {value: key for key, value in EFFORT_RANK.items()}

TASK_TYPE_PATTERNS: dict[str, tuple[str, ...]] = {
    "documentation": ("document", "docs", "readme", "changelog", "comment", "explain"),
    "planning": ("plan", "roadmap", "scope", "estimate", "architecture", "design"),
    "implementation": ("implement", "code", "patch", "feature", "adapter", "refactor", "fix"),
    "testing": ("test", "pytest", "validation", "benchmark", "coverage", "regression"),
    "review": ("review", "audit", "cross validate", "judge", "compare", "verdict"),
    "research": ("research", "paper", "citation", "literature", "state of the art"),
    "release": ("release", "1.0", "ship", "tag", "version", "migration"),
}

RISK_PATTERNS: dict[str, tuple[str, ...]] = {
    "security": ("security", "auth", "authentication", "authorization", "credential", "secret", "sandbox", "injection", "rce", "xss", "csrf"),
    "data_loss": ("delete", "destructive", "migration", "backup", "rollback", "data loss", "drop table"),
    "concurrency": ("parallel", "simultaneous", "race", "deadlock", "lock", "async", "thread", "multiprocess"),
    "external_side_effects": ("deploy", "publish", "push", "release", "production", "billing", "payment"),
    "safety_critical": ("safety", "flight", "mavlink", "robot", "drone", "hardware", "actuator", "weapon"),
}


def _effort_rank(value: str | None) -> int:
    return EFFORT_RANK.get(str(value or "medium").strip().lower(), 1)


def _clamp_effort_rank(value: int) -> int:
    return max(0, min(2, int(value)))


def _effort_from_rank(value: int) -> str:
    return EFFORT_BY_RANK[_clamp_effort_rank(value)]


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _range_span(text: str) -> int | None:
    spans: list[int] = []
    patterns = (
        r"\bv(\d+)\s*[-–]\s*v?(\d+)\b",
        r"\bfrom[-_ ]version\s*[=:]?\s*(\d+)\b.*?\bto[-_ ]version\s*[=:]?\s*(\d+)\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL):
            try:
                start, end = int(match.group(1)), int(match.group(2))
            except (TypeError, ValueError):
                continue
            if end >= start:
                spans.append(end - start + 1)
    return max(spans) if spans else None


def assess_task_scope(prompt: str, cwd: Path | None = None, role: str = "") -> dict[str, Any]:
    """Classify request scope from observable task text and repository facts.

    The assessment is intentionally deterministic and auditable. It does not try
    to infer private worker ability; it records factual signals that explain why
    a stage deserves low, medium, or high effort before learned routing data is
    applied.
    """
    text = f"{role}\n{prompt}".lower()
    prompt_chars = len(prompt)
    task_types = sorted(name for name, patterns in TASK_TYPE_PATTERNS.items() if _contains_any(text, patterns))
    risk_flags = sorted(name for name, patterns in RISK_PATTERNS.items() if _contains_any(text, patterns))
    versions = _version_mentions(prompt)
    span = _range_span(prompt)
    roadmap = roadmap_snapshot(cwd, max_chars=0) if cwd is not None else {"available": False, "roadmaps": []}
    repo_facts = quick_repo_facts(cwd) if cwd is not None else {"file_count": 0, "language_counts": {}, "kind_counts": {}, "truncated": False}
    roadmap_versions: list[str] = []
    if isinstance(roadmap, dict):
        for row in roadmap.get("roadmaps", []):
            if isinstance(row, dict):
                roadmap_versions.extend(str(value) for value in row.get("versions", []))
    score = 0.0
    reasons: list[str] = []
    if prompt_chars < 2500:
        score += 0.5
        reasons.append("short prompt")
    elif prompt_chars < 12000:
        score += 1.2
        reasons.append("moderate prompt")
    elif prompt_chars < 40000:
        score += 2.2
        reasons.append("long prompt")
    else:
        score += 3.0
        reasons.append("very long prompt")
    if "documentation" in task_types and set(task_types) <= {"documentation", "planning"}:
        score -= 0.4
        reasons.append("documentation/planning only")
    if "implementation" in task_types:
        score += 1.2
        reasons.append("implementation requested")
    if "testing" in task_types:
        score += 0.8
        reasons.append("testing or validation requested")
    if "review" in task_types:
        score += 0.8
        reasons.append("review or cross-validation requested")
    if "research" in task_types:
        score += 0.7
        reasons.append("research/citation requested")
    if "release" in task_types:
        score += 1.0
        reasons.append("release/versioning requested")
    if risk_flags:
        score += min(3.0, 1.15 * len(risk_flags))
        reasons.append("risk flags: " + ", ".join(risk_flags))
    if versions:
        score += min(2.0, 0.35 * len(versions))
        reasons.append(f"{len(versions)} version mention(s)")
    if span:
        if span >= 10:
            score += 2.0
            reasons.append(f"large version jump span {span}")
        elif span >= 4:
            score += 1.2
            reasons.append(f"multi-version span {span}")
        else:
            score += 0.4
            reasons.append(f"small version span {span}")
    if "request/roadmap.md" in text:
        score += 0.3
        reasons.append("explicit request/roadmap.md alignment")
    repo_file_count = int(repo_facts.get("file_count", 0) or 0) if isinstance(repo_facts, dict) else 0
    if repo_file_count >= 500:
        score += 1.5
        reasons.append(f"large repository map: {repo_file_count} indexed files")
    elif repo_file_count >= 100:
        score += 0.8
        reasons.append(f"moderate repository map: {repo_file_count} indexed files")
    elif repo_file_count >= 30:
        score += 0.4
        reasons.append(f"small repository map: {repo_file_count} indexed files")
    score = max(0.0, round(score, 2))
    if score <= 1.5:
        bucket = "tiny"
    elif score <= 3.0:
        bucket = "small"
    elif score <= 5.2:
        bucket = "medium"
    elif score <= 7.5:
        bucket = "large"
    else:
        bucket = "critical"
    base_effort = {"tiny": "low", "small": "low", "medium": "medium", "large": "high", "critical": "high"}[bucket]
    if risk_flags and ("security" in risk_flags or "safety_critical" in risk_flags or "data_loss" in risk_flags):
        base_effort = "high"
    elif risk_flags and _effort_rank(base_effort) < 1:
        base_effort = "medium"
    return {
        "schema_version": 1,
        "scope_score": score,
        "scope_bucket": bucket,
        "base_effort": base_effort,
        "task_types": task_types,
        "risk_flags": risk_flags,
        "prompt_chars": prompt_chars,
        "requested_versions": versions,
        "version_span": span,
        "roadmap_available": bool(roadmap.get("available")) if isinstance(roadmap, dict) else False,
        "roadmap_versions": sorted(set(roadmap_versions))[:50],
        "repo_facts": repo_facts,
        "reasons": reasons,
    }


def _jsonl_rows(path: Path, max_rows: int = 250) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines[-max_rows:]:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _history_paths(context_dir: Path, max_paths: int = 80) -> list[Path]:
    run_root = context_dir.parent
    state_root = run_root.parent if run_root.parent != run_root else run_root
    candidates: list[Path] = []
    for relative in ("learning-ledger.jsonl", "scope-routing-ledger.jsonl"):
        path = context_dir / relative
        if path.is_file():
            candidates.append(path)
    try:
        found = sorted(
            state_root.rglob("learning-ledger.jsonl"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        )
        found.extend(sorted(
            state_root.rglob("scope-routing-ledger.jsonl"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
            reverse=True,
        ))
    except OSError:
        found = []
    seen: set[Path] = set()
    for path in [*candidates, *found]:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        candidates.append(resolved)
        if len(candidates) >= max_paths:
            break
    return candidates


def _load_learning_history(context_dir: Path, max_rows: int = 500) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _history_paths(context_dir):
        rows.extend(_jsonl_rows(path, max_rows=150))
        if len(rows) >= max_rows:
            break
    rows.sort(key=lambda row: str(row.get("completed_at") or row.get("decided_at") or ""))
    return rows[-max_rows:]


def _row_status(row: dict[str, Any]) -> str:
    return str(row.get("status") or row.get("outcome") or "UNKNOWN").upper()


def _row_quality(row: dict[str, Any]) -> float:
    value = _as_number(row.get("quality_signal_score") or row.get("observed_quality_score"))
    if value is not None:
        return max(0.0, min(1.0, value))
    status = _row_status(row)
    if status == "PASS":
        return 0.8
    if status in {"UNKNOWN", "RUNNING"}:
        return 0.45
    if status in {"BLOCKED", "NEEDS_HUMAN", "QUOTA_PAUSED"}:
        return 0.35
    return 0.1


def _row_tokens(row: dict[str, Any]) -> int:
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else None
    value = row.get("total_tokens_observed")
    if usage and value is None:
        value = usage.get("total_tokens")
    number = _as_number(value)
    return int(number) if number is not None else 0


def _row_scope_bucket(row: dict[str, Any]) -> str:
    scope = row.get("scope") if isinstance(row.get("scope"), dict) else None
    if scope:
        return str(scope.get("scope_bucket") or "")
    return str(row.get("scope_bucket") or "")


def _row_task_types(row: dict[str, Any]) -> set[str]:
    scope = row.get("scope") if isinstance(row.get("scope"), dict) else None
    values = scope.get("task_types", []) if scope else row.get("task_types", [])
    return {str(value) for value in values} if isinstance(values, list) else set()


def _candidate_history_score(agent: Any, effort: str, scope: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    runtime = str(getattr(agent, "runtime", ""))
    model = str(getattr(agent, "model", ""))
    name = str(getattr(agent, "name", ""))
    scope_bucket = str(scope.get("scope_bucket", ""))
    task_types = set(str(value) for value in scope.get("task_types", []))
    exact: list[dict[str, Any]] = []
    related: list[dict[str, Any]] = []
    for row in history:
        row_runtime = str(row.get("runtime") or row.get("selected_runtime") or "")
        row_model = str(row.get("model") or row.get("selected_model") or "")
        row_agent = str(row.get("agent") or row.get("selected_agent") or "")
        if row_runtime != runtime:
            continue
        if row_model and model and row_model != model:
            continue
        row_effort = str(row.get("effort") or row.get("selected_effort") or "")
        row_bucket = _row_scope_bucket(row)
        row_types = _row_task_types(row)
        if row_agent == name and row_effort == effort and row_bucket == scope_bucket:
            exact.append(row)
        elif row_effort == effort or row_bucket == scope_bucket or (task_types and row_types & task_types):
            related.append(row)
    matched = exact or related
    if not matched:
        return {"rows": 0, "pass_rate": None, "avg_quality": None, "avg_tokens": None, "score": 0.0}
    pass_like = sum(1 for row in matched if _row_status(row) == "PASS")
    qualities = [_row_quality(row) for row in matched]
    token_values = [tokens for tokens in (_row_tokens(row) for row in matched) if tokens > 0]
    pass_rate = pass_like / len(matched)
    avg_quality = sum(qualities) / len(qualities) if qualities else 0.0
    avg_tokens = sum(token_values) / len(token_values) if token_values else None
    token_penalty = 0.0
    if avg_tokens:
        token_penalty = min(0.45, avg_tokens / 200000.0)
    exact_bonus = 0.2 if exact else 0.0
    score = round(pass_rate * 1.2 + avg_quality + exact_bonus - token_penalty, 3)
    return {
        "rows": len(matched),
        "exact_rows": len(exact),
        "pass_rate": round(pass_rate, 3),
        "avg_quality": round(avg_quality, 3),
        "avg_tokens": round(avg_tokens, 1) if avg_tokens is not None else None,
        "score": score,
    }


def _scope_min_effort(scope: dict[str, Any], configured_effort: str, role: str = "") -> str:
    rank = _effort_rank(scope.get("base_effort"))
    role_text = role.lower()
    if any(flag in scope.get("risk_flags", []) for flag in ("security", "safety_critical", "data_loss")):
        rank = max(rank, 2)
    if any(word in role_text for word in ("security", "judge", "assurance", "release", "converger")):
        rank = max(rank, _effort_rank(configured_effort))
    return _effort_from_rank(rank)


def _replace_agent(agent: Any, *, model: str | None = None, effort: str | None = None) -> Any:
    updates: dict[str, Any] = {}
    if model is not None:
        updates["model"] = model
    if effort is not None:
        updates["effort"] = effort
    if not updates:
        return agent
    if dataclasses.is_dataclass(agent):
        return dataclasses.replace(agent, **updates)
    # Fallback for simple mutable test doubles.
    clone = type("ScopedAgent", (), {})()
    clone.__dict__.update(getattr(agent, "__dict__", {}))
    for key, value in updates.items():
        setattr(clone, key, value)
    return clone


def select_agent_for_scope(
    agents: dict[str, Any],
    requested_agent: str,
    prompt: str,
    cwd: Path,
    context_dir: Path,
    stage_dir: Path | None = None,
    *,
    role: str = "",
    allow_agent_switch: bool = True,
) -> tuple[str, Any, dict[str, Any]]:
    """Select a worker/model/effort from current scope and prior ledgers.

    Only configured agents and configured models are eligible. The planner may
    reduce effort for low-risk stages or raise it for high-risk stages; it does
    not invent model names that are absent from the user's configuration.
    """
    if requested_agent not in agents:
        raise KeyError(f"requested agent not configured: {requested_agent}")
    scope = assess_task_scope(prompt, cwd, role=role)
    history = _load_learning_history(context_dir)
    candidate_names = list(agents.keys()) if allow_agent_switch else [requested_agent]
    if requested_agent in candidate_names:
        candidate_names = [requested_agent, *[name for name in candidate_names if name != requested_agent]]
    rules: list[str] = []
    if not allow_agent_switch:
        rules.append("agent switch disabled for this competition/review lane")
    if not history:
        rules.append("no prior learning rows available; using deterministic scope rules")
    candidates: list[dict[str, Any]] = []
    for name in candidate_names:
        agent = agents[name]
        configured_effort = str(getattr(agent, "effort", "medium") or "medium")
        minimum = _scope_min_effort(scope, configured_effort, role=role or str(getattr(agent, "role", "")))
        efforts_to_try = [minimum]
        if history and not scope.get("risk_flags"):
            min_rank = _effort_rank(minimum)
            for rank in range(min_rank - 1, -1, -1):
                efforts_to_try.append(_effort_from_rank(rank))
        seen_efforts: set[str] = set()
        for effort in efforts_to_try:
            if effort in seen_efforts:
                continue
            seen_efforts.add(effort)
            evidence = _candidate_history_score(agent, effort, scope, history)
            score = evidence.get("score", 0.0) or 0.0
            score += {"low": 0.06, "medium": 0.03, "high": 0.0}[effort]
            if name == requested_agent:
                score += 0.12
            if _effort_rank(effort) < _effort_rank(minimum):
                if evidence.get("rows", 0) and (evidence.get("pass_rate") or 0) >= 0.8:
                    score += 0.08
                else:
                    score -= 0.6
            if scope.get("scope_bucket") in {"large", "critical"} and effort == "low":
                score -= 0.8
            candidates.append({
                "agent": name,
                "runtime": str(getattr(agent, "runtime", "")),
                "model": str(getattr(agent, "model", "")),
                "effort": effort,
                "configured_effort": configured_effort,
                "score": round(score, 3),
                "history": evidence,
            })
    selected = sorted(candidates, key=lambda row: (-float(row["score"]), row["agent"] != requested_agent, row["agent"], row["effort"]))[0]
    selected_agent = _replace_agent(
        agents[str(selected["agent"])],
        model=str(selected["model"]),
        effort=str(selected["effort"]),
    )
    if selected["agent"] != requested_agent:
        rules.append(f"selected {selected['agent']} because learned/configured evidence scored higher than requested {requested_agent}")
    if selected["effort"] != str(getattr(agents[str(selected["agent"])], "effort", "")):
        rules.append(f"adjusted effort to {selected['effort']} from configured {getattr(agents[str(selected['agent'])], 'effort', '')}")
    decision = {
        "schema_version": 1,
        "decided_at": dt.datetime.now(UTC).isoformat(),
        "requested_agent": requested_agent,
        "selected_agent": selected["agent"],
        "selected_runtime": selected["runtime"],
        "selected_model": selected["model"],
        "selected_effort": selected["effort"],
        "scope": scope,
        "history_rows_considered": len(history),
        "candidate_scores": candidates,
        "rules": rules,
        "rationale": (
            f"scope={scope['scope_bucket']} score={scope['scope_score']} -> {scope['base_effort']}; "
            f"selected {selected['agent']}/{selected['runtime']} model={selected['model'] or '<configured default>'} "
            f"effort={selected['effort']} using {selected['history'].get('rows', 0)} matched learning row(s)."
        ),
    }
    if stage_dir is not None:
        atomic_json(stage_dir / "scope-assessment.json", scope)
        atomic_json(stage_dir / "routing-decision.json", decision)
    write_scope_routing_decision(context_dir, stage_dir, decision)
    return str(selected["agent"]), selected_agent, decision


def _stage_for_preflight(stage_dir: Path) -> str:
    try:
        return stage_dir.relative_to(find_run_root(stage_dir)).as_posix()
    except ValueError:
        return stage_dir.name


def record_prompt_preflight(
    stage_dir: Path,
    agent: Any,
    prompt: str,
    scope_routing: dict[str, Any] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Estimate and persist prompt token/cost data before invoking a worker.

    This works for subscription/OAuth CLI sessions because it never calls the
    vendor API. The output is a shadow API-equivalent estimate plus an auth-mode
    inference so later runs can learn prompt size and cost pressure even when the
    native CLI does not expose authoritative usage fields.
    """
    context_dir = context_dir_for(stage_dir)
    history = _load_learning_history(context_dir)
    preflight = build_prompt_preflight(
        agent=agent,
        prompt=prompt,
        stage=_stage_for_preflight(stage_dir),
        scope_routing=scope_routing,
        history_rows=history,
        env=env,
    )
    atomic_json(stage_dir / "prompt-preflight.json", preflight)
    atomic_json(stage_dir / "prompt-cost-estimate.json", preflight.get("estimated_api_cost_usd", {}))
    (context_dir / "prompt-preflights").mkdir(parents=True, exist_ok=True)
    stage_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", preflight.get("stage", "stage")).strip("-") or "stage"
    atomic_json(context_dir / "prompt-preflights" / f"{stage_key}.json", preflight)
    ledger_row = {
        "estimated_at": preflight.get("estimated_at"),
        "stage": preflight.get("stage"),
        "agent": preflight.get("agent"),
        "runtime": preflight.get("runtime"),
        "provider": preflight.get("provider"),
        "model": preflight.get("model"),
        "effort": preflight.get("effort"),
        "auth_mode": (preflight.get("auth") or {}).get("mode"),
        "uses_subscription_or_oauth": (preflight.get("auth") or {}).get("uses_subscription_or_oauth"),
        "scope_bucket": (preflight.get("scope") or {}).get("scope_bucket"),
        "scope_score": (preflight.get("scope") or {}).get("scope_score"),
        "task_types": (preflight.get("scope") or {}).get("task_types", []),
        "risk_flags": (preflight.get("scope") or {}).get("risk_flags", []),
        "prompt_chars": (preflight.get("prompt") or {}).get("chars"),
        "estimated_input_tokens": (preflight.get("prompt") or {}).get("estimated_tokens"),
        "estimated_output_tokens": (preflight.get("output_tokens") or {}).get("expected"),
        "estimated_api_cost_usd": ((preflight.get("estimated_api_cost_usd") or {}).get("cost_usd") or {}).get("total_expected"),
        "estimated_api_cost_usd_range": {
            "lower": ((preflight.get("estimated_api_cost_usd") or {}).get("cost_usd") or {}).get("total_lower"),
            "upper": ((preflight.get("estimated_api_cost_usd") or {}).get("cost_usd") or {}).get("total_upper"),
        },
        "prompt_sha256": (preflight.get("prompt") or {}).get("sha256"),
    }
    append_jsonl(context_dir / "prompt-preflight-ledger.jsonl", ledger_row)
    _write_prompt_cost_summary(context_dir)
    return preflight


def _write_prompt_cost_summary(context_dir: Path) -> None:
    rows = _jsonl_rows(context_dir / "prompt-preflight-ledger.jsonl", max_rows=2000)
    totals = {
        "stage_count": len(rows),
        "estimated_input_tokens": 0,
        "estimated_output_tokens": 0,
        "estimated_api_cost_usd": 0.0,
        "by_runtime": {},
        "by_auth_mode": {},
    }
    for row in rows:
        input_tokens = int(_as_number(row.get("estimated_input_tokens")) or 0)
        output_tokens = int(_as_number(row.get("estimated_output_tokens")) or 0)
        cost = float(_as_number(row.get("estimated_api_cost_usd")) or 0.0)
        totals["estimated_input_tokens"] += input_tokens
        totals["estimated_output_tokens"] += output_tokens
        totals["estimated_api_cost_usd"] = round(float(totals["estimated_api_cost_usd"]) + cost, 6)
        for bucket_name, key in (("by_runtime", str(row.get("runtime") or "unknown")), ("by_auth_mode", str(row.get("auth_mode") or "unknown"))):
            bucket = totals[bucket_name].setdefault(key, {"stage_count": 0, "estimated_input_tokens": 0, "estimated_output_tokens": 0, "estimated_api_cost_usd": 0.0})
            bucket["stage_count"] += 1
            bucket["estimated_input_tokens"] += input_tokens
            bucket["estimated_output_tokens"] += output_tokens
            bucket["estimated_api_cost_usd"] = round(float(bucket["estimated_api_cost_usd"]) + cost, 6)
    atomic_json(context_dir / "prompt-cost-summary.json", {"schema_version": 1, "updated_at": dt.datetime.now(UTC).isoformat(), **totals})
    lines = [
        "# Prompt token and shadow API cost summary",
        "",
        "These estimates are recorded before invoking Codex CLI or Claude Code. They are local, offline estimates and do not require API keys.",
        "Costs are API-equivalent USD estimates for learning/comparison while native CLI usage may be covered by ChatGPT/Claude subscription/OAuth or usage credits.",
        "",
        f"- Stages estimated: `{totals['stage_count']}`",
        f"- Estimated input tokens: `{totals['estimated_input_tokens']}`",
        f"- Estimated output tokens: `{totals['estimated_output_tokens']}`",
        f"- Estimated API-equivalent cost: `${totals['estimated_api_cost_usd']}`",
        "",
    ]
    for row in rows[-10:]:
        lines.append(
            f"- `{row.get('stage')}` `{row.get('runtime')}`/`{row.get('model') or '<default>'}` "
            f"auth `{row.get('auth_mode')}`: input `{row.get('estimated_input_tokens')}`, "
            f"expected output `{row.get('estimated_output_tokens')}`, shadow cost `${row.get('estimated_api_cost_usd')}`."
        )
    atomic_write(context_dir / "prompt-cost-summary.md", "\n".join(lines).rstrip() + "\n")


def _stage_key(stage_dir: Path | None, context_dir: Path) -> str:
    if stage_dir is None:
        return "unknown-stage"
    try:
        return stage_dir.relative_to(context_dir.parent).as_posix()
    except ValueError:
        return stage_dir.name


def write_scope_routing_decision(context_dir: Path, stage_dir: Path | None, decision: dict[str, Any]) -> None:
    (context_dir / "routing-decisions").mkdir(parents=True, exist_ok=True)
    stage_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", _stage_key(stage_dir, context_dir)).strip("-") or "stage"
    atomic_json(context_dir / "routing-decisions" / f"{stage_key}.json", decision)
    rows = _jsonl_rows(context_dir / "scope-routing-ledger.jsonl", max_rows=1000)
    rows.append({
        "decided_at": decision.get("decided_at"),
        "stage": _stage_key(stage_dir, context_dir),
        "requested_agent": decision.get("requested_agent"),
        "selected_agent": decision.get("selected_agent"),
        "selected_runtime": decision.get("selected_runtime"),
        "selected_model": decision.get("selected_model"),
        "selected_effort": decision.get("selected_effort"),
        "scope_bucket": (decision.get("scope") or {}).get("scope_bucket"),
        "scope_score": (decision.get("scope") or {}).get("scope_score"),
        "task_types": (decision.get("scope") or {}).get("task_types", []),
        "risk_flags": (decision.get("scope") or {}).get("risk_flags", []),
        "history_rows_considered": decision.get("history_rows_considered", 0),
        "rationale": decision.get("rationale"),
    })
    rows = rows[-1000:]
    atomic_write(context_dir / "scope-routing-ledger.jsonl", "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n")
    summary = [
        "# Scope-aware model and effort routing",
        "",
        "The supervisor selects only configured workers/models. Decisions combine deterministic request scope with prior ledger outcomes.",
        "",
    ]
    for row in rows[-10:]:
        summary.append(
            f"- `{row.get('stage')}`: scope `{row.get('scope_bucket')}` score `{row.get('scope_score')}`; "
            f"selected `{row.get('selected_agent')}` / `{row.get('selected_runtime')}` / "
            f"effort `{row.get('selected_effort')}`."
        )
    atomic_write(context_dir / "scope-routing-summary.md", "\n".join(summary).rstrip() + "\n")


def _write_learning_artifacts(context_dir: Path, events: list[dict[str, Any]]) -> None:
    ledger_rows: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "schema_version": 1,
        "updated_at": dt.datetime.now(UTC).isoformat(),
        "stage_count": len(events),
        "by_agent": {},
        "by_runtime": {},
        "by_scope": {},
        "totals": {"prompt_chars": 0, "output_chars": 0, "total_tokens": 0, "cost_usd": 0.0, "estimated_input_tokens": 0, "estimated_api_cost_usd": 0.0, "pass_like": 0},
    }
    for row in events:
        usage = _event_usage(row)
        quality = row.get("quality_signals", {}) if isinstance(row.get("quality_signals"), dict) else {}
        prompt_metrics = row.get("prompt_metrics", {}) if isinstance(row.get("prompt_metrics"), dict) else {}
        output_chars = int(row.get("output_chars", 0) or 0)
        prompt_chars = int(prompt_metrics.get("chars", 0) or 0)
        scope = row.get("scope_assessment", {}) if isinstance(row.get("scope_assessment"), dict) else {}
        routing = row.get("routing_decision", {}) if isinstance(row.get("routing_decision"), dict) else {}
        tokens = int(usage.get("total_tokens", 0) or 0)
        cost = float(usage.get("cost_usd", 0.0) or 0.0)
        preflight = (row.get("runtime_metadata") or {}).get("prompt_preflight", {}) if isinstance(row.get("runtime_metadata"), dict) else {}
        prompt_pre = preflight.get("prompt", {}) if isinstance(preflight, dict) else {}
        cost_pre = preflight.get("estimated_api_cost_usd", {}) if isinstance(preflight, dict) else {}
        cost_pre_values = cost_pre.get("cost_usd", {}) if isinstance(cost_pre, dict) else {}
        estimated_input_tokens = int(prompt_pre.get("estimated_tokens", 0) or 0) if isinstance(prompt_pre, dict) else 0
        estimated_cost = float(cost_pre_values.get("total_expected", 0.0) or 0.0) if isinstance(cost_pre_values, dict) else 0.0
        status = str(quality.get("status", "UNKNOWN"))
        entry = {
            "event_id": row.get("event_id"),
            "completed_at": row.get("completed_at"),
            "stage": row.get("stage"),
            "agent": row.get("agent"),
            "runtime": row.get("runtime"),
            "model": row.get("model"),
            "effort": row.get("effort"),
            "status": status,
            "quality_signal_score": quality.get("quality_signal_score"),
            "requested_versions": quality.get("requested_versions", []),
            "reported_versions": quality.get("reported_versions", []),
            "scope_bucket": scope.get("scope_bucket"),
            "scope_score": scope.get("scope_score"),
            "task_types": scope.get("task_types", []),
            "risk_flags": scope.get("risk_flags", []),
            "selected_agent": routing.get("selected_agent"),
            "selected_effort": routing.get("selected_effort"),
            "prompt_chars": prompt_chars,
            "output_chars": output_chars,
            "total_tokens_observed": tokens,
            "cost_usd_observed": cost,
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_api_cost_usd": estimated_cost,
            "auth_mode": (preflight.get("auth") or {}).get("mode") if isinstance(preflight, dict) else None,
            "prompt_artifact": row.get("prompt_artifact"),
            "output_artifact": row.get("artifact"),
            "output_sha256": row.get("output_sha256"),
        }
        ledger_rows.append(entry)
        for bucket_name in ("by_agent", "by_runtime"):
            key = str(row.get("agent" if bucket_name == "by_agent" else "runtime", "unknown")) or "unknown"
            bucket = summary[bucket_name].setdefault(key, {
                "stage_count": 0, "prompt_chars": 0, "output_chars": 0,
                "total_tokens_observed": 0, "cost_usd_observed": 0.0,
                "estimated_input_tokens": 0, "estimated_api_cost_usd": 0.0,
                "pass_like": 0, "statuses": {},
            })
            bucket["stage_count"] += 1
            bucket["prompt_chars"] += prompt_chars
            bucket["output_chars"] += output_chars
            bucket["total_tokens_observed"] += tokens
            bucket["cost_usd_observed"] = round(float(bucket["cost_usd_observed"]) + cost, 6)
            bucket["estimated_input_tokens"] += estimated_input_tokens
            bucket["estimated_api_cost_usd"] = round(float(bucket["estimated_api_cost_usd"]) + estimated_cost, 6)
            bucket["statuses"][status] = bucket["statuses"].get(status, 0) + 1
            if status == "PASS":
                bucket["pass_like"] += 1
        scope_key = str(scope.get("scope_bucket") or "unknown")
        scope_bucket = summary["by_scope"].setdefault(scope_key, {
            "stage_count": 0, "prompt_chars": 0, "output_chars": 0,
            "total_tokens_observed": 0, "cost_usd_observed": 0.0,
            "estimated_input_tokens": 0, "estimated_api_cost_usd": 0.0,
            "pass_like": 0, "statuses": {},
        })
        scope_bucket["stage_count"] += 1
        scope_bucket["prompt_chars"] += prompt_chars
        scope_bucket["output_chars"] += output_chars
        scope_bucket["total_tokens_observed"] += tokens
        scope_bucket["cost_usd_observed"] = round(float(scope_bucket["cost_usd_observed"]) + cost, 6)
        scope_bucket["estimated_input_tokens"] += estimated_input_tokens
        scope_bucket["estimated_api_cost_usd"] = round(float(scope_bucket["estimated_api_cost_usd"]) + estimated_cost, 6)
        scope_bucket["statuses"][status] = scope_bucket["statuses"].get(status, 0) + 1
        if status == "PASS":
            scope_bucket["pass_like"] += 1
        summary["totals"]["prompt_chars"] += prompt_chars
        summary["totals"]["output_chars"] += output_chars
        summary["totals"]["total_tokens"] += tokens
        summary["totals"]["cost_usd"] = round(float(summary["totals"]["cost_usd"]) + cost, 6)
        summary["totals"]["estimated_input_tokens"] += estimated_input_tokens
        summary["totals"]["estimated_api_cost_usd"] = round(float(summary["totals"]["estimated_api_cost_usd"]) + estimated_cost, 6)
        if status == "PASS":
            summary["totals"]["pass_like"] += 1
    for group in ("by_agent", "by_runtime", "by_scope"):
        for bucket in summary[group].values():
            output_chars = int(bucket.get("output_chars", 0) or 0)
            tokens = int(bucket.get("total_tokens_observed", 0) or 0)
            stages = int(bucket.get("stage_count", 0) or 0)
            bucket["tokens_per_output_char"] = round(tokens / output_chars, 4) if output_chars and tokens else None
            bucket["average_output_chars"] = round(output_chars / stages, 1) if stages else 0
    atomic_write(context_dir / "learning-ledger.jsonl", "\n".join(json.dumps(row, sort_keys=True) for row in ledger_rows) + ("\n" if ledger_rows else ""))
    atomic_json(context_dir / "learning-summary.json", summary)
    atomic_json(context_dir / "prompt-effectiveness.json", prompt_effectiveness(events))
    lesson_lines = [
        "# Legion token and accuracy learning summary",
        "",
        "Use this supervisor-generated summary to reduce redundant context on later stages while preserving roadmap fidelity.",
        "Accuracy values are deterministic evidence signals, not proof of correctness.",
        "",
    ]
    if not events:
        lesson_lines.append("No completed stages have learning data yet.")
    else:
        best_runtime = None
        runtime_buckets = summary.get("by_runtime", {})
        if runtime_buckets:
            best_runtime = sorted(
                runtime_buckets.items(),
                key=lambda item: (
                    -(item[1].get("pass_like", 0) or 0),
                    item[1].get("tokens_per_output_char") if item[1].get("tokens_per_output_char") is not None else 10**9,
                    item[0],
                ),
            )[0]
        if best_runtime:
            lesson_lines.extend([
                f"- Most efficient observed runtime so far: `{best_runtime[0]}` ",
                f"  ({best_runtime[1].get('pass_like', 0)} PASS-like stages, "
                f"{best_runtime[1].get('total_tokens_observed', 0)} observed tokens).",
            ])
        recent = ledger_rows[-5:]
        for entry in recent:
            lesson_lines.append(
                f"- `{entry.get('stage')}` used `{entry.get('runtime')}`/`{entry.get('effort')}`; "
                f"status `{entry.get('status')}`, score `{entry.get('quality_signal_score')}`, "
                f"observed tokens `{entry.get('total_tokens_observed')}`, estimated input `{entry.get('estimated_input_tokens')}`, shadow API cost `${entry.get('estimated_api_cost_usd')}`."
            )
        lesson_lines.append("- Prefer concise prompts that cite exact prior artifacts instead of replaying full prior outputs when the ledger shows high token use.")
    atomic_write(context_dir / "prompt-lessons.md", "\n".join(lesson_lines).rstrip() + "\n")


def refresh_shared_memory(context_dir: Path) -> None:
    events = _load_events(context_dir)
    _write_learning_artifacts(context_dir, events)
    index_lines = [json.dumps(row, sort_keys=True) for row in events]
    atomic_write(context_dir / "stage-index.jsonl", "\n".join(index_lines) + ("\n" if index_lines else ""))

    parts = [
        "# Legion shared memory",
        "",
        "This is the canonical provider-neutral memory shared by Codex CLI and Claude Code.",
        "Provider-native hidden state is not shared. Use the artifacts and evidence below as the source of truth.",
        "",
    ]
    if not events:
        parts.append("No completed worker stages have been recorded yet.\n")
    for row in events:
        parts.extend(
            [
                f"## {row.get('stage', 'unknown stage')} — {row.get('agent', 'unknown')} / {row.get('runtime', 'unknown')}",
                "",
                f"- Completed: `{row.get('completed_at', '')}`",
                f"- Output artifact: `{row.get('artifact', '')}`",
                f"- Output SHA-256: `{row.get('output_sha256', '')}`",
                f"- Git status after stage: `{str(row.get('git_status', '')).replace(chr(10), ' | ')}`",
                f"- Prompt artifact: `{row.get('prompt_artifact', '')}`",
                f"- Quality signal: `{(row.get('quality_signals') or {}).get('quality_signal_score', '')}` / status `{(row.get('quality_signals') or {}).get('status', '')}`",
                f"- Observed usage: `{(row.get('runtime_metadata') or {}).get('usage', {})}`",
                f"- Prompt preflight: `{((row.get('runtime_metadata') or {}).get('prompt_preflight', {}) or {}).get('estimated_api_cost_usd', {})}`",
                "",
                str(row.get("excerpt", "")).strip() or "No textual output was captured.",
                "",
            ]
        )
    cost_summary = context_dir / "prompt-cost-summary.md"
    if cost_summary.is_file():
        parts.extend(["", "## Prompt token and cost summary", "", cost_summary.read_text(encoding="utf-8").strip(), ""])
    lessons = context_dir / "prompt-lessons.md"
    if lessons.is_file():
        parts.extend(["", "## Token and accuracy learning summary", "", lessons.read_text(encoding="utf-8").strip(), ""])
    atomic_write(context_dir / "shared-memory.md", "\n".join(parts).rstrip() + "\n")


def ensure_shared_context(stage_dir: Path, cwd: Path, agent: Any) -> Path:
    run_root = find_run_root(stage_dir)
    context_dir = run_root / "shared-context"
    (context_dir / "events").mkdir(parents=True, exist_ok=True)
    (context_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    job: dict[str, Any] = {}
    job_path = run_root / "job.json"
    if not job_path.is_file():
        job_path = run_root / "manifest.json"
    if job_path.is_file():
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            job = {}

    brief_path = context_dir / "campaign-brief.md"
    if not brief_path.exists():
        brief = [
            "# Legion campaign brief",
            "",
            "All workers operate on the same isolated Git worktree and use this directory as canonical shared memory.",
            "The context directory is supervisor-owned and read-only from the worker's perspective.",
            "Do not merge, push, deploy, tag, publish, release, alter credentials, or operate hardware.",
            "",
            "## Job metadata",
            "",
            "```json",
            json.dumps(job, indent=2, sort_keys=True),
            "```",
            "",
            "## Initial repository snapshot",
            "",
            "```json",
            json.dumps(git_snapshot(cwd), indent=2, sort_keys=True),
            "```",
            "",
            "## Roadmap snapshot",
            "",
            "Roadmap candidates include `request/roadmap.md`, `docs/roadmap.md`, `roadmap.md`, and `docs/**/*roadmap*.md` when present.",
            "",
            "```json",
            json.dumps(roadmap_snapshot(cwd), indent=2, sort_keys=True),
            "```",
        ]
        atomic_write(brief_path, "\n".join(brief).rstrip() + "\n")

    instructions = context_dir / "CONTEXT.md"
    if not instructions.exists():
        atomic_write(
            instructions,
            "# Shared context contract\n\n"
            "This directory is the canonical cross-model memory for the current Hermes Legion Commander run.\n\n"
            "- Read `campaign-brief.md`, `shared-memory.md`, and `stage-index.jsonl` before acting.\n"
            "- Read referenced files under `artifacts/` when a prior result is material.\n"
            "- Treat this directory as read-only. The supervisor checks its integrity.\n"
            "- Read `learning-summary.json`, `prompt-effectiveness.json`, `prompt-cost-summary.md`, and `prompt-lessons.md` when present; use them to reduce redundant context, choose efficient evidence paths, and delegate grunt work to cheap subagents where it lowers token cost.\n"
            "- Read `repo-context-pack.md` and `repo-map/REPO_MAP.md` before broad repository searches; use the map to open targeted files first.\n"
            "- Align changes and review claims with the roadmap snapshot, especially `request/roadmap.md` when the target repository has one.\n"
            "- Perform repository changes only in the current working directory.\n"
            "- Do not rely on private provider conversation history for shared facts.\n",
        )

    try:
        refresh_repo_intelligence(context_dir, cwd)
    except Exception as exc:  # pragma: no cover - navigation aid must not block orchestration
        atomic_json(context_dir / "repo-map-error.json", {"error": str(exc), "updated_at": dt.datetime.now(UTC).isoformat()})
    refresh_shared_memory(context_dir)
    atomic_json(
        context_dir / "runtime.json",
        {
            "agent": getattr(agent, "name", ""),
            "runtime": getattr(agent, "runtime", ""),
            "model": getattr(agent, "model", ""),
            "effort": getattr(agent, "effort", ""),
            "worktree": str(cwd.resolve()),
            "updated_at": dt.datetime.now(UTC).isoformat(),
        },
    )
    return context_dir




def _make_tree_writable(path: Path) -> None:
    """Best-effort permission reset before replacing a prior context snapshot."""
    if not path.exists():
        return
    for item in sorted(path.rglob("*"), reverse=True):
        try:
            item.chmod(stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if item.is_dir() else 0))
        except OSError:
            pass
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    except OSError:
        pass


def _make_tree_read_only(path: Path) -> None:
    """Best-effort read-only permissions for a worker context snapshot.

    This is defense in depth, not the sole protection. The supervisor also
    hashes every file in the snapshot and rejects the stage if anything changes.
    """
    for item in path.rglob("*"):
        try:
            if item.is_dir():
                item.chmod(stat.S_IRUSR | stat.S_IXUSR)
            else:
                item.chmod(stat.S_IRUSR)
        except OSError:
            pass
    try:
        path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    except OSError:
        pass


def create_worker_context_snapshot(stage_dir: Path, canonical_context: Path) -> Path:
    """Create a stage-local immutable snapshot of the canonical shared memory.

    Vendor CLIs receive the snapshot, never the canonical directory. This keeps
    a worker from corrupting cross-model memory even when a CLI's directory flag
    grants broader access than read-only semantics. Checkpoint competitors can
    also run in parallel because each stage gets an independent snapshot.
    """
    snapshot = stage_dir / "worker-context"
    if snapshot.exists():
        _make_tree_writable(snapshot)
        shutil.rmtree(snapshot)

    def ignore_worker_context_entries(directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name.endswith(".tmp") or (name.startswith(".") and name.endswith(".tmp"))}
        if Path(directory).name == "repo-map":
            ignored.add("cache")
        if Path(directory).name == "artifacts":
            ignored.add("prompts")
        return ignored

    shutil.copytree(
        canonical_context,
        snapshot,
        ignore=ignore_worker_context_entries,
    )
    return snapshot


def seal_worker_context_snapshot(snapshot: Path) -> None:
    """Apply best-effort read-only permissions after prompt assembly is complete."""
    _make_tree_read_only(snapshot)


def _context_manifest(context_dir: Path) -> dict[str, str]:
    """Hash supervisor-owned context.

    Canonical directories hash only immutable contract files because supervisor
    events may be appended concurrently. Stage-local ``worker-context``
    snapshots are static, so every file is hashed and protected.
    """
    rows: dict[str, str] = {}
    if context_dir.name == "worker-context":
        candidates = sorted(path for path in context_dir.rglob("*") if path.is_file())
    else:
        candidates = [context_dir / name for name in ("CONTEXT.md", "campaign-brief.md")]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            name = path.relative_to(context_dir).as_posix()
            rows[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return rows


def build_prompt_with_shared_context(
    prompt: str,
    context_dir: Path,
    cwd: Path,
    max_prompt_chars: int,
    context_budget: int = 36000,
    include_git_snapshot: bool = True,
) -> str:
    try:
        refresh_repo_intelligence(context_dir, cwd, task_prompt=prompt)
    except Exception as exc:  # pragma: no cover - navigation aid must not block worker execution
        atomic_json(context_dir / "repo-map-error.json", {"error": str(exc), "updated_at": dt.datetime.now(UTC).isoformat()})
    try:
        refresh_anchored_truth(context_dir, cwd, task_prompt=prompt)
    except Exception as exc:  # pragma: no cover - truth refresh should degrade to an explicit error artifact, not block execution
        atomic_json(context_dir / "anchored-truth-error.json", {"error": str(exc), "updated_at": dt.datetime.now(UTC).isoformat()})
    try:
        from .workflow_governance import refresh_governance
        refresh_governance(context_dir, cwd, task_prompt=prompt)
    except Exception as exc:  # pragma: no cover - governance is a prompt aid; record explicit degradation
        atomic_json(context_dir / "governance-error.json", {"error": str(exc), "updated_at": dt.datetime.now(UTC).isoformat()})
    refresh_shared_memory(context_dir)
    blocks: list[str] = []
    for name in ("ANCHORED_TRUTH.md", "GOVERNANCE.md", "CONTEXT.md", "campaign-brief.md", "scope-routing-summary.md", "prompt-cost-summary.md", "prompt-lessons.md", "repo-context-pack.md", "repo-map/REPO_MAP.md", "shared-memory.md"):
        path = context_dir / name
        if path.is_file():
            blocks.append(f"## {name}\n\n{path.read_text(encoding='utf-8')}")
    if include_git_snapshot:
        snapshot = json.dumps(git_snapshot(cwd), indent=2, sort_keys=True)
        blocks.append(f"## Current repository snapshot\n\n```json\n{snapshot}\n```")
    context = "\n\n".join(blocks)
    if len(context) > context_budget:
        context = context[:context_budget] + "\n\n[Shared context excerpt truncated; full files are available in the shared context directory.]"
    header = (
        "# HERMES LEGION COMMANDER SHARED CONTEXT\n\n"
        f"Canonical context directory: {context_dir}\n"
        "Both native workers receive this same provider-neutral memory. Read it before acting.\n"
        "Do not modify the context directory. Work only in the current isolated Git worktree.\n\n"
    )
    combined = header + context + "\n\n# CURRENT STAGE TASK\n\n" + prompt
    if len(combined) > max_prompt_chars:
        task_budget = max(4000, max_prompt_chars - len(header) - min(len(context), context_budget) - 64)
        combined = header + context + "\n\n# CURRENT STAGE TASK\n\n" + prompt[:task_budget]
        if len(prompt) > task_budget:
            combined += "\n\n[Current task truncated by configured prompt limit.]"
    return combined[:max_prompt_chars]


def render_command(
    agent: Any,
    prompt: str,
    prompt_file: Path,
    context_dir: Path,
    stage_dir: Path,
    cwd: Path,
    output_file: Path,
) -> list[str]:
    values = {
        "prompt": prompt,
        "prompt_file": str(prompt_file),
        "context_dir": str(context_dir),
        "stage_dir": str(stage_dir),
        "cwd": str(cwd),
        "output_file": str(output_file),
        "role": getattr(agent, "role", ""),
        "name": getattr(agent, "name", ""),
        "runtime": getattr(agent, "runtime", ""),
        "provider": getattr(agent, "provider", ""),
        "model": getattr(agent, "model", ""),
        "effort": getattr(agent, "effort", ""),
    }
    rendered: list[str] = []
    for part in agent.command:
        if part == "{model_args}":
            if values["model"]:
                rendered.extend(["--model", values["model"]])
            continue
        if part == "{effort_args}":
            if not values["effort"]:
                continue
            if values["runtime"] == "codex-cli":
                rendered.extend(["-c", f'model_reasoning_effort="{values["effort"]}"'])
            elif values["runtime"] == "claude-code":
                rendered.extend(["--effort", values["effort"]])
            continue
        if part == "{omit_if_empty_model}":
            continue
        rendered.append(part.format(**values))
    return rendered



def run_worker_process(
    command: list[str] | tuple[str, ...],
    *,
    cwd: Path,
    prompt: str | None,
    timeout: int,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a native worker with an explicitly UTF-8 stdin/stdout boundary.

    Windows' default subprocess text encoding can be a legacy code page. Codex
    requires UTF-8 when reading a prompt from stdin, so relying on ``text=True``
    can corrupt characters such as em dashes, mathematical symbols, and
    non-English roadmap text. Encode the prompt ourselves and decode output
    deterministically on every platform.
    """
    input_bytes = None if prompt is None else prompt.encode("utf-8", errors="strict")
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        input=input_bytes,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=env,
    )
    stdout_bytes = completed.stdout or b""
    stderr_bytes = completed.stderr or b""
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
    )

def stdin_for(agent: Any, prompt: str) -> str | None:
    transport = getattr(agent, "prompt_transport", "argument")
    return prompt if transport == "stdin" else None


def classify_worker_failure(stdout: str, stderr: str, returncode: int) -> str:
    """Classify worker availability failures for quota-aware failover."""
    text = f"{stdout}\n{stderr}".lower()
    if any(marker in text for marker in ENTITLEMENT_MARKERS):
        return "entitlement"
    if any(marker in text for marker in AUTH_MARKERS):
        return "authentication"
    if returncode in {8, 29, 75, 429} or any(marker in text for marker in QUOTA_MARKERS):
        return "quota"
    if returncode != 0 or any(marker in text for marker in ERROR_MARKERS):
        return "worker_error"
    return "none"


def sanitized_worker_environment(agent: Any, base: dict[str, str] | None = None) -> dict[str, str]:
    """Return a subprocess environment with known cross-provider overrides removed.

    Native CLI credential stores remain available. Only variables explicitly
    configured on the worker are removed; API keys are not removed implicitly.
    """
    env = dict(os.environ if base is None else base)
    for name in getattr(agent, "unset_env", ()):
        env.pop(str(name), None)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def is_quota_error(stdout: str, stderr: str, returncode: int) -> bool:
    return classify_worker_failure(stdout, stderr, returncode) == "quota"


def _json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def normalize_worker_output(
    agent: Any,
    stdout: str,
    stderr: str,
    returncode: int,
    output_file: Path,
) -> tuple[str, dict[str, Any]]:
    runtime = getattr(agent, "runtime", "")
    output_format = getattr(agent, "output_format", "text")
    metadata: dict[str, Any] = {"runtime": runtime, "returncode": returncode}
    text = f"{stdout}\n{stderr}".lower()

    if returncode != 0:
        raise RuntimeError(f"worker exited with code {returncode}")
    if any(marker in text for marker in ERROR_MARKERS):
        raise RuntimeError("worker output contains a provider/client error marker")

    if runtime == "codex-cli" or output_format == "codex-jsonl":
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        failures = [event for event in events if event.get("type") in {"error", "turn.failed"}]
        if failures:
            raise RuntimeError(f"Codex reported failure events: {failures[-1]}")
        thread = next((event.get("thread_id") for event in events if event.get("type") == "thread.started"), None)
        if thread:
            metadata["session_id"] = thread
        if output_file.is_file() and output_file.stat().st_size:
            result = output_file.read_text(encoding="utf-8").strip()
        else:
            messages = [
                str(event.get("item", {}).get("text", ""))
                for event in events
                if event.get("type") == "item.completed"
                and isinstance(event.get("item"), dict)
                and event["item"].get("type") == "agent_message"
            ]
            result = messages[-1].strip() if messages else ""
        if not result:
            raise RuntimeError("Codex completed without a final agent message")
        metadata["event_count"] = len(events)
        usage = observed_usage_from_objects(events)
        if usage:
            metadata["usage"] = usage
        return result, metadata

    if runtime == "claude-code" or output_format == "claude-json":
        obj = _json_object(stdout)
        if obj is None:
            result = stdout.strip()
        else:
            if obj.get("is_error") is True or obj.get("subtype") in {"error", "failure"}:
                raise RuntimeError(f"Claude Code reported an error: {obj}")
            result = str(obj.get("result") or obj.get("response") or "").strip()
            for key in ("session_id", "cost_usd", "total_cost_usd", "duration_ms", "num_turns"):
                if key in obj:
                    metadata[key] = obj[key]
            usage = observed_usage_from_objects([obj])
            if usage:
                metadata["usage"] = usage
        if not result:
            raise RuntimeError("Claude Code completed without a result")
        if "usage" not in metadata:
            usage = observed_usage_from_text(stdout)
            if usage:
                metadata["usage"] = usage
        return result, metadata


    result = stdout.strip()
    if not result:
        raise RuntimeError("worker completed without textual output")
    return result, metadata


def record_stage_event(
    stage_dir: Path,
    cwd: Path,
    agent: Any,
    normalized_output: str,
    metadata: dict[str, Any],
    capture_git: bool = True,
    prompt: str | None = None,
    raw_stdout: str | None = None,
    raw_stderr: str | None = None,
    command: list[str] | tuple[str, ...] | None = None,
) -> None:
    context_dir = context_dir_for(stage_dir)
    (context_dir / "events").mkdir(parents=True, exist_ok=True)
    (context_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    completed = dt.datetime.now(UTC)
    completed_at = completed.isoformat()
    stage = str(stage_dir.relative_to(find_run_root(stage_dir)))
    raw_event_id = f"{completed_at}-{stage}-{getattr(agent, 'name', '')}"
    agent_slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(getattr(agent, "name", "") or "agent")).strip("-")[:16]
    digest = hashlib.sha256(raw_event_id.encode("utf-8", errors="replace")).hexdigest()[:12]
    event_id = f"{completed.strftime('%Y%m%dT%H%M%S')}-{agent_slug or 'agent'}-{digest}"
    artifact = context_dir / "artifacts" / f"{event_id}.md"
    atomic_write(artifact, normalized_output.rstrip() + "\n")
    prompt_artifact = None
    prompt_metrics: dict[str, Any] = {}
    if prompt is not None:
        prompt_path = context_dir / "artifacts" / "prompts" / f"{event_id}.md"
        atomic_write(prompt_path, prompt.rstrip() + "\n")
        prompt_artifact = str(prompt_path.relative_to(context_dir))
        prompt_metrics = {"chars": len(prompt), "sha256": sha256_text(prompt)}
        preflight = metadata.get("prompt_preflight") if isinstance(metadata.get("prompt_preflight"), dict) else {}
        prompt_pre = preflight.get("prompt", {}) if isinstance(preflight, dict) else {}
        if isinstance(prompt_pre, dict):
            prompt_metrics["estimated_tokens"] = prompt_pre.get("estimated_tokens")
            prompt_metrics["estimator_version"] = prompt_pre.get("estimator_version")
        cost_pre = preflight.get("estimated_api_cost_usd", {}) if isinstance(preflight, dict) else {}
        cost_values = cost_pre.get("cost_usd", {}) if isinstance(cost_pre, dict) else {}
        if isinstance(cost_values, dict):
            prompt_metrics["estimated_api_cost_usd"] = cost_values.get("total_expected")
    raw_metrics: dict[str, Any] = {}
    if raw_stdout is not None:
        raw_metrics["stdout_chars"] = len(raw_stdout)
        raw_metrics["stdout_sha256"] = sha256_text(raw_stdout)
        metadata.setdefault("usage", observed_usage_from_text(raw_stdout))
    if raw_stderr is not None:
        raw_metrics["stderr_chars"] = len(raw_stderr)
        raw_metrics["stderr_sha256"] = sha256_text(raw_stderr)
    if command is not None:
        metadata.setdefault("command", list(command))
    snapshot = git_snapshot(cwd) if capture_git else {"status": "", "diff_stat": ""}
    quality = _quality_signals(prompt or "", normalized_output, metadata, snapshot)
    routing_decision = metadata.get("scope_routing") if isinstance(metadata.get("scope_routing"), dict) else None
    scope_assessment = routing_decision.get("scope") if isinstance(routing_decision, dict) else None
    event = {
        "event_id": event_id,
        "completed_at": completed_at,
        "stage": stage,
        "agent": getattr(agent, "name", ""),
        "runtime": getattr(agent, "runtime", ""),
        "model": getattr(agent, "model", ""),
        "effort": getattr(agent, "effort", ""),
        "artifact": str(artifact.relative_to(context_dir)),
        "prompt_artifact": prompt_artifact,
        "prompt_metrics": prompt_metrics,
        "raw_metrics": raw_metrics,
        "output_sha256": sha256_text(normalized_output),
        "output_chars": len(normalized_output),
        "excerpt": normalized_output[:3500],
        "git_status": snapshot.get("status", ""),
        "diff_stat": snapshot.get("diff_stat", ""),
        "quality_signals": quality,
        "subagents": extract_subagent_report(normalized_output, int(metadata.get("subagent_cap", 5) or 5)),
        "scope_assessment": scope_assessment,
        "routing_decision": routing_decision,
        "roadmap_snapshot": roadmap_snapshot(cwd, max_chars=0),
        "runtime_metadata": metadata,
    }
    atomic_json(context_dir / "events" / f"{event_id}.json", event)
    refresh_shared_memory(context_dir)


def shared_context_integrity(context_dir: Path) -> dict[str, str]:
    return _context_manifest(context_dir)
