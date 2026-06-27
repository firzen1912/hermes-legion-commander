"""Workflow governance for Legion Commander patches.

The governance layer turns repository state into reviewable controls before and
after model work:

* risk escalation based on changed files,
* ownership/specialist routing hints,
* patch-budget checks,
* evidence-diff explanation,
* regression memory injection,
* local/CI parity warnings,
* PR merge-readiness scoring,
* PR review comments,
* branch cleanup, and
* a static release/dashboard page.

The implementation is intentionally local-first and stdlib-only. GitHub features
use the GitHub CLI OAuth/keyring session through ``github_health``.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fnmatch
import html
import json
import os
import platform
import re
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path
from typing import Any

try:
    from .github_health import GitHubHealthError, check_health, find_gh, owner_repo_from_remote, remote_url, run_gh
except ImportError:  # pragma: no cover - direct fixture imports
    from hermes_legion_commander.github_health import GitHubHealthError, check_health, find_gh, owner_repo_from_remote, remote_url, run_gh

UTC = dt.timezone.utc

RISK_VALUE = {"low": 1, "medium": 2, "high": 3, "critical": 4}
RISK_NAME = {v: k for k, v in RISK_VALUE.items()}

# Conservative defaults; repository owners can override with [ownership] in a
# Commander config or with shared-context/governance/ownership.toml.
DEFAULT_OWNERSHIP: dict[str, list[str]] = {
    "docs/**": ["documentation", "collaborating"],
    "README.md": ["documentation", "collaborating"],
    ".github/workflows/**": ["ci_release", "competing"],
    "requirements/**": ["dependency_security", "competing"],
    "pyproject.toml": ["dependency_security", "competing"],
    "src/security/**": ["security_assurance", "competing"],
    "src/release/**": ["release_engineer", "competing"],
    "src/safety/**": ["safety_assurance", "competing"],
    "src/command/**": ["command_path", "competing"],
    "src/adapters/**": ["adapter_contracts", "competing"],
    "configs/reference-config/**": ["evidence_reconciler", "competing"],
    "results/evidence/**": ["evidence_reconciler", "final_verify"],
    "evidence/**": ["evidence_reconciler", "final_verify"],
}

RISK_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("critical", "safety/actuation or vehicle authority", "src/safety/**"),
    ("critical", "command-path or actuation-adjacent change", "src/command/**"),
    ("critical", "release gate / provenance / qualification logic", "src/release/**"),
    ("critical", "GitHub workflow / release automation", ".github/workflows/**"),
    ("critical", "reference hardware trust gate", "configs/reference-config/**"),
    ("high", "security or cryptography change", "src/security/**"),
    ("high", "identity / auth / key lifecycle", "**/*keyring*.py"),
    ("high", "identity / auth / key lifecycle", "**/*identity*.py"),
    ("high", "dependency or lockfile change", "requirements/**"),
    ("high", "dependency or project metadata change", "pyproject.toml"),
    ("high", "evidence package or signed artifact", "evidence/**"),
    ("high", "committed release evidence", "results/evidence/**"),
    ("medium", "tests changed", "tests/**"),
    ("medium", "tooling changed", "tools/**"),
    ("medium", "configuration changed", "configs/**"),
    ("low", "documentation changed", "docs/**"),
    ("low", "documentation changed", "README.md"),
    ("low", "documentation changed", "CHANGELOG.md"),
)

DEFAULT_BUDGET = {
    "max_files": 40,
    "max_lines": 2000,
    "max_security_files": 5,
    "max_evidence_files": 20,
    "max_workflow_files": 3,
}

REGRESSION_HINTS = (
    ("CRLF", "Normalize CR/CRLF to LF before hashing or signing committed text artifacts; prefer git-blob or LF-normalized bytes."),
    ("line-ending", "Evidence signatures and digest pins must be line-ending stable across Windows and Linux CI."),
    ("Dependabot", "Dependency updates require GitHub health gating and blocking alerts must be zero before merge."),
    ("workflow", "If CI fails after a push, inspect GitHub run logs before committing more roadmap work."),
    ("Co-Authored-By", "Do not add Co-Authored-By trailers when the repo policy forbids them."),
)


def now_iso() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run(cmd: list[str], *, cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def git(repo: Path, *args: str, timeout: int = 60) -> str:
    cp = _run(["git", *args], cwd=repo, timeout=timeout)
    if cp.returncode != 0:
        raise RuntimeError((cp.stderr or cp.stdout or "git command failed").strip())
    return cp.stdout.strip()


def git_ok(repo: Path, *args: str, timeout: int = 60) -> tuple[bool, str]:
    cp = _run(["git", *args], cwd=repo, timeout=timeout)
    return cp.returncode == 0, (cp.stdout or cp.stderr).strip()


def _safe_rel(path: str) -> str:
    return path.replace("\\", "/").strip()


def current_branch(repo: Path) -> str:
    ok, out = git_ok(repo, "branch", "--show-current")
    return out if ok else ""


def current_head(repo: Path) -> str:
    ok, out = git_ok(repo, "rev-parse", "HEAD")
    return out if ok else ""


def tracked_file_count(repo: Path) -> int:
    ok, out = git_ok(repo, "ls-files")
    return len([ln for ln in out.splitlines() if ln.strip()]) if ok else 0


def _default_base_ref(repo: Path, base_branch: str = "dev", remote: str = "origin") -> str:
    ok, _ = git_ok(repo, "rev-parse", "--verify", f"{remote}/{base_branch}")
    if ok:
        return f"{remote}/{base_branch}"
    ok, _ = git_ok(repo, "rev-parse", "--verify", base_branch)
    if ok:
        return base_branch
    return "HEAD"


def changed_files(repo: Path, *, base_ref: str | None = None) -> list[str]:
    """Return files changed against base plus dirty/untracked files."""
    rows: set[str] = set()
    base_ref = base_ref or _default_base_ref(repo)
    if base_ref != "HEAD":
        ok, out = git_ok(repo, "diff", "--name-only", f"{base_ref}...HEAD", timeout=120)
        if ok:
            rows.update(_safe_rel(ln) for ln in out.splitlines() if ln.strip())
    ok, out = git_ok(repo, "diff", "--name-only", timeout=120)
    if ok:
        rows.update(_safe_rel(ln) for ln in out.splitlines() if ln.strip())
    ok, out = git_ok(repo, "diff", "--cached", "--name-only", timeout=120)
    if ok:
        rows.update(_safe_rel(ln) for ln in out.splitlines() if ln.strip())
    ok, out = git_ok(repo, "ls-files", "--others", "--exclude-standard", timeout=120)
    if ok:
        rows.update(_safe_rel(ln) for ln in out.splitlines() if ln.strip())
    return sorted(rows)


def diff_numstat(repo: Path, *, base_ref: str | None = None) -> dict[str, Any]:
    base_ref = base_ref or _default_base_ref(repo)
    cmd = ["diff", "--numstat"]
    if base_ref != "HEAD":
        cmd.append(f"{base_ref}...HEAD")
    ok, out = git_ok(repo, *cmd, timeout=120)
    files = 0
    added = 0
    deleted = 0
    by_file: dict[str, dict[str, int | str]] = {}
    if ok:
        for ln in out.splitlines():
            parts = ln.split("\t")
            if len(parts) < 3:
                continue
            a, d, path = parts[0], parts[1], _safe_rel(parts[2])
            ai = 0 if a == "-" else int(a or 0)
            di = 0 if d == "-" else int(d or 0)
            files += 1
            added += ai
            deleted += di
            by_file[path] = {"added": ai, "deleted": di, "lines": ai + di}
    return {"files": files, "added": added, "deleted": deleted, "lines": added + deleted, "by_file": by_file}


def _matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.lstrip("**/"))


def classify_risk(paths: list[str]) -> dict[str, Any]:
    max_val = 1 if paths else 0
    hits: list[dict[str, str]] = []
    counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for path in paths:
        path_hit = False
        for level, reason, pattern in RISK_PATTERNS:
            if _matches(path, pattern):
                val = RISK_VALUE[level]
                max_val = max(max_val, val)
                counts[level] += 1
                hits.append({"path": path, "risk": level, "reason": reason, "pattern": pattern})
                path_hit = True
                break
        if not path_hit:
            counts["low"] += 1
    level = RISK_NAME.get(max_val, "none") if paths else "none"
    if max_val >= RISK_VALUE["critical"]:
        recommended = "competing"
        required = ["cross-validation", "final-verify", "github-health"]
    elif max_val >= RISK_VALUE["high"]:
        recommended = "competing"
        required = ["security-review", "github-health"]
    elif max_val >= RISK_VALUE["medium"]:
        recommended = "collaborating"
        required = ["focused-tests"]
    else:
        recommended = "alternating"
        required = ["basic-tests"] if paths else []
    return {
        "risk_level": level,
        "risk_value": max_val,
        "counts": counts,
        "hits": hits,
        "recommended_mode": recommended,
        "required_gates": required,
        "escalate_to_competing": recommended == "competing",
    }


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_ownership(repo: Path, config: Path | None = None, context_dir: Path | None = None) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {k: list(v) for k, v in DEFAULT_OWNERSHIP.items()}
    sources = []
    if config:
        sources.append(config)
    if context_dir:
        sources.append(context_dir / "governance" / "ownership.toml")
    sources.append(repo / "config" / "legion_ownership.toml")
    sources.append(repo / ".legion-ownership.toml")
    for source in sources:
        data = _load_toml(source)
        table = data.get("ownership") if isinstance(data.get("ownership"), dict) else {}
        for pattern, owners in table.items():
            if isinstance(owners, str):
                rows[str(pattern)] = [owners]
            elif isinstance(owners, list):
                rows[str(pattern)] = [str(item) for item in owners]
    return rows


def route_owners(paths: list[str], ownership: dict[str, list[str]]) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    specialists: set[str] = set()
    modes: set[str] = set()
    unmatched: list[str] = []
    for path in paths:
        hit = False
        for pattern, owners in ownership.items():
            if _matches(path, pattern):
                hit = True
                matches.append({"path": path, "pattern": pattern, "owners": owners})
                for owner in owners:
                    if owner in {"competing", "collaborating", "alternating", "final_verify"}:
                        modes.add(owner)
                    else:
                        specialists.add(owner)
                break
        if not hit:
            unmatched.append(path)
    if "competing" in modes:
        preferred_mode = "competing"
    elif "collaborating" in modes:
        preferred_mode = "collaborating"
    else:
        preferred_mode = "alternating"
    return {
        "preferred_mode": preferred_mode,
        "specialists": sorted(specialists),
        "mode_hints": sorted(modes),
        "matches": matches,
        "unmatched": unmatched,
    }


def patch_budget(paths: list[str], diff: dict[str, Any], *, budget: dict[str, int] | None = None) -> dict[str, Any]:
    budget = {**DEFAULT_BUDGET, **(budget or {})}
    security_files = [p for p in paths if p.startswith("src/security/") or "keyring" in p or "identity" in p]
    evidence_files = [p for p in paths if p.startswith("results/evidence/") or p.startswith("evidence/")]
    workflow_files = [p for p in paths if p.startswith(".github/workflows/")]
    checks = {
        "files": {"value": len(paths), "limit": budget["max_files"], "ok": len(paths) <= budget["max_files"]},
        "lines": {"value": int(diff.get("lines", 0)), "limit": budget["max_lines"], "ok": int(diff.get("lines", 0)) <= budget["max_lines"]},
        "security_files": {"value": len(security_files), "limit": budget["max_security_files"], "ok": len(security_files) <= budget["max_security_files"]},
        "evidence_files": {"value": len(evidence_files), "limit": budget["max_evidence_files"], "ok": len(evidence_files) <= budget["max_evidence_files"]},
        "workflow_files": {"value": len(workflow_files), "limit": budget["max_workflow_files"], "ok": len(workflow_files) <= budget["max_workflow_files"]},
    }
    return {"ok": all(row["ok"] for row in checks.values()), "budget": budget, "checks": checks}


def evidence_diff(repo: Path, paths: list[str], *, base_ref: str | None = None) -> dict[str, Any]:
    evidence_paths = [p for p in paths if p.startswith("results/evidence/") or p.startswith("evidence/")]
    rows: list[dict[str, Any]] = []
    for path in evidence_paths:
        kind = "semantic-evidence"
        low = path.lower()
        if low.endswith((".sig", ".pem", ".pub")) or "public_key" in low or "trust-root" in low:
            kind = "signature-or-key-churn"
        elif low.endswith(".json") and any(token in low for token in ("manifest", "summary", "results", "requirements_trace")):
            kind = "structured-evidence"
        elif low.endswith((".jsonl", ".log")):
            kind = "run-log-churn"
        elif low.endswith((".md", ".txt")):
            kind = "human-readable-evidence"
        rows.append({"path": path, "kind": kind, "review_note": _evidence_note(kind)})
    return {
        "changed_evidence_files": rows,
        "count": len(rows),
        "requires_explanation": bool(rows),
        "summary": _evidence_summary(rows),
    }


def _evidence_note(kind: str) -> str:
    if kind == "signature-or-key-churn":
        return "Verify this is expected regeneration, not key substitution."
    if kind == "run-log-churn":
        return "Prefer not committing timestamp-only operational churn unless it is release evidence."
    if kind == "structured-evidence":
        return "Check semantic gate fields and line-ending-stable hashes/signatures."
    return "Review evidence diff for weakened gates, self-awarded claims, or stale paths."


def _evidence_summary(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No committed evidence artifacts changed."
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row.get("kind"))] = counts.get(str(row.get("kind")), 0) + 1
    return ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))


def local_ci_parity(repo: Path) -> dict[str, Any]:
    ok, autocrlf = git_ok(repo, "config", "--get", "core.autocrlf")
    autocrlf = autocrlf if ok else "<unset>"
    gitattributes = repo / ".gitattributes"
    workflows = repo / ".github" / "workflows"
    workflow_os: set[str] = set()
    py_versions: set[str] = set()
    if workflows.is_dir():
        for path in list(workflows.glob("*.yml")) + list(workflows.glob("*.yaml")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"runs-on:\s*([^\n]+)", text):
                workflow_os.add(m.group(1).strip().strip("'\""))
            for m in re.finditer(r"python-version(?:s)?[:=]\s*([^\n]+)", text):
                py_versions.add(m.group(1).strip().strip("'\"[]"))
    warnings: list[str] = []
    if platform.system().lower().startswith("win") and autocrlf.lower() in {"true", "input"} and not gitattributes.is_file():
        warnings.append("Windows core.autocrlf is enabled and .gitattributes is absent; hashing/signing raw text bytes can fail on Linux CI.")
    if workflow_os and not any("windows" in osname.lower() for osname in workflow_os) and platform.system().lower().startswith("win"):
        warnings.append("Local OS is Windows but CI appears Linux-focused; validate path separators, shell commands, and line endings.")
    return {
        "local_platform": platform.platform(),
        "python_version": platform.python_version(),
        "git_core_autocrlf": autocrlf,
        "gitattributes_present": gitattributes.is_file(),
        "workflow_runs_on": sorted(workflow_os),
        "workflow_python_versions": sorted(py_versions),
        "warnings": warnings,
        "ok": not warnings,
    }


def _read_jsonl(path: Path, limit: int = 50) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except json.JSONDecodeError:
            continue
    return rows


def regression_memory(context_dir: Path, task_prompt: str, paths: list[str]) -> dict[str, Any]:
    memory_file = context_dir / "regression-memory.jsonl"
    rows = _read_jsonl(memory_file, 80)
    generated: list[dict[str, Any]] = []
    text = " ".join([task_prompt, " ".join(paths)]).lower()
    for trigger, rule in REGRESSION_HINTS:
        if trigger.lower() in text:
            generated.append({"trigger": trigger, "rule": rule, "source": "built-in"})
    active_rules: list[dict[str, Any]] = []
    active_rules.extend(rows[-20:])
    active_rules.extend(generated)
    return {"memory_file": str(memory_file), "rule_count": len(active_rules), "active_rules": active_rules}


def append_regression_memory(context_dir: Path, *, title: str, rule: str, evidence: str = "") -> dict[str, Any]:
    context_dir.mkdir(parents=True, exist_ok=True)
    path = context_dir / "regression-memory.jsonl"
    row = {"recorded_at": now_iso(), "title": title, "rule": rule, "evidence": evidence}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    lessons = context_dir / "prompt-lessons.md"
    with lessons.open("a", encoding="utf-8") as fh:
        fh.write(f"\n- **{title}:** {rule}" + (f" Evidence: {evidence}" if evidence else "") + "\n")
    return row


def _workflow_names(repo: Path) -> tuple[str, ...]:
    root = repo / ".github" / "workflows"
    names: list[str] = []
    if root.is_dir():
        for path in sorted([*root.glob("*.yml"), *root.glob("*.yaml")]):
            name = path.stem
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
                if line.strip().startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip("'\"") or name
                    break
            names.append(name)
    return tuple(dict.fromkeys(names))


def github_health_snapshot(repo: Path, branch: str | None = None, head_sha: str | None = None, out_dir: Path | None = None) -> dict[str, Any]:
    gh = find_gh()
    if gh is None:
        return {"available": False, "error": "gh not found"}
    try:
        return check_health(
            repo=repo,
            gh_path=gh,
            branch=branch or current_branch(repo),
            head_sha=head_sha or current_head(repo),
            require_workflows=_workflow_names(repo),
            wait=False,
            out_dir=out_dir,
        )
    except Exception as exc:
        return {"available": False, "error": str(exc), "gh_path": str(gh)}


def merge_readiness(repo: Path, governance: dict[str, Any], github: dict[str, Any] | None = None) -> dict[str, Any]:
    github = github or governance.get("github_health") or {}
    score = 100
    blockers: list[str] = []
    warnings: list[str] = []
    risk = governance.get("risk", {})
    budget = governance.get("patch_budget", {})
    parity = governance.get("ci_parity", {})
    evidence = governance.get("evidence_diff", {})
    if risk.get("risk_level") == "critical":
        score -= 25
        warnings.append("Critical-risk files changed; require competing mode/final verification.")
    elif risk.get("risk_level") == "high":
        score -= 15
        warnings.append("High-risk files changed; require security/release review.")
    if not budget.get("ok", True):
        score -= 15
        blockers.append("Patch budget exceeded.")
    if parity.get("warnings"):
        score -= 8
        warnings.extend(str(w) for w in parity.get("warnings", []))
    if evidence.get("requires_explanation"):
        score -= 5
        warnings.append(f"Evidence changed: {evidence.get('summary')}")
    if github:
        workflow = github.get("workflow_gate", {}) if isinstance(github.get("workflow_gate"), dict) else {}
        dep = github.get("dependabot_gate", {}) if isinstance(github.get("dependabot_gate"), dict) else {}
        if github.get("available") is False or github.get("error"):
            score -= 10
            warnings.append(f"GitHub health unavailable: {github.get('error')}")
        if workflow and not workflow.get("ok"):
            score -= 30
            blockers.append("Required GitHub workflows are not all green.")
        if dep and not dep.get("ok"):
            score -= 30
            blockers.append("Blocking Dependabot alerts are open.")
    status = "ready" if score >= 90 and not blockers else "review" if score >= 70 and not blockers else "blocked"
    return {"score": max(0, min(100, score)), "status": status, "blockers": blockers, "warnings": warnings}


def render_governance_markdown(report: dict[str, Any]) -> str:
    risk = report.get("risk", {})
    routing = report.get("ownership_routing", {})
    budget = report.get("patch_budget", {})
    readiness = report.get("merge_readiness", {})
    evidence = report.get("evidence_diff", {})
    parity = report.get("ci_parity", {})
    lines = [
        f"# Legion Commander Governance — {readiness.get('status', 'review').upper()}",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Repository: `{report.get('repo')}`",
        f"- Branch: `{report.get('branch')}`",
        f"- HEAD: `{str(report.get('head_sha') or '')[:12]}`",
        f"- Changed files: `{len(report.get('changed_files', []))}`",
        f"- Risk: `{risk.get('risk_level')}` → recommended mode `{risk.get('recommended_mode')}`",
        f"- Merge readiness: `{readiness.get('score')}/100` `{readiness.get('status')}`",
        "",
        "## Blockers",
        "",
    ]
    blockers = readiness.get("blockers") or []
    lines.extend([f"- {b}" for b in blockers] or ["- None detected."])
    lines.extend(["", "## Warnings", ""])
    warnings = readiness.get("warnings") or []
    lines.extend([f"- {w}" for w in warnings] or ["- None detected."])
    lines.extend([
        "",
        "## Specialist routing",
        "",
        f"- Preferred mode: `{routing.get('preferred_mode')}`",
        f"- Specialists: `{', '.join(routing.get('specialists', [])) or '<none>'}`",
        "",
        "## Patch budget",
        "",
        f"- Budget OK: `{budget.get('ok')}`",
    ])
    for name, row in (budget.get("checks") or {}).items():
        lines.append(f"- {name}: `{row.get('value')}/{row.get('limit')}` ok=`{row.get('ok')}`")
    lines.extend(["", "## Evidence diff", "", f"- {evidence.get('summary')}"])
    for item in evidence.get("changed_evidence_files", [])[:30]:
        lines.append(f"- `{item.get('path')}` — {item.get('kind')}: {item.get('review_note')}")
    lines.extend(["", "## Local/CI parity", ""])
    for warning in parity.get("warnings", []):
        lines.append(f"- {warning}")
    if not parity.get("warnings"):
        lines.append("- No local/CI parity warnings detected.")
    lines.extend(["", "## Changed files", ""])
    for path in report.get("changed_files", [])[:100]:
        lines.append(f"- `{path}`")
    if len(report.get("changed_files", [])) > 100:
        lines.append("- [truncated]")
    lines.append("")
    return "\n".join(lines)


def write_governance_artifacts(context_dir: Path, report: dict[str, Any]) -> None:
    out = context_dir / "governance"
    out.mkdir(parents=True, exist_ok=True)
    (out / "governance-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "merge-readiness.json").write_text(json.dumps(report.get("merge_readiness", {}), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "merge-readiness.md").write_text(render_governance_markdown(report), encoding="utf-8")
    (out / "changed-files.json").write_text(json.dumps(report.get("changed_files", []), indent=2) + "\n", encoding="utf-8")
    (context_dir / "GOVERNANCE.md").write_text(render_governance_markdown(report), encoding="utf-8")


def refresh_governance(
    context_dir: Path,
    repo: Path,
    *,
    task_prompt: str = "",
    base_ref: str | None = None,
    include_github: bool = True,
    config: Path | None = None,
) -> dict[str, Any]:
    repo = repo.resolve()
    context_dir.mkdir(parents=True, exist_ok=True)
    paths = changed_files(repo, base_ref=base_ref)
    diff = diff_numstat(repo, base_ref=base_ref)
    ownership = load_ownership(repo, config=config, context_dir=context_dir)
    github = github_health_snapshot(repo, out_dir=context_dir / "governance" / "github-health") if include_github else {"available": False, "error": "disabled"}
    report = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "repo": str(repo),
        "branch": current_branch(repo),
        "head_sha": current_head(repo),
        "tracked_file_count": tracked_file_count(repo),
        "base_ref": base_ref or _default_base_ref(repo),
        "changed_files": paths,
        "diff": diff,
        "risk": classify_risk(paths),
        "ownership_routing": route_owners(paths, ownership),
        "patch_budget": patch_budget(paths, diff),
        "evidence_diff": evidence_diff(repo, paths, base_ref=base_ref),
        "ci_parity": local_ci_parity(repo),
        "regression_memory": regression_memory(context_dir, task_prompt, paths),
        "github_health": github,
    }
    report["merge_readiness"] = merge_readiness(repo, report, github)
    write_governance_artifacts(context_dir, report)
    render_dashboard(repo, context_dir, report)
    return report


def render_pr_comment(report: dict[str, Any]) -> str:
    readiness = report.get("merge_readiness", {})
    risk = report.get("risk", {})
    github = report.get("github_health", {}) if isinstance(report.get("github_health"), dict) else {}
    workflow = github.get("workflow_gate", {}) if isinstance(github.get("workflow_gate"), dict) else {}
    dep = github.get("dependabot_gate", {}) if isinstance(github.get("dependabot_gate"), dict) else {}
    return "\n".join([
        "## Legion Commander Review",
        "",
        f"**Merge readiness:** `{readiness.get('score')}/100` — `{readiness.get('status')}`",
        f"**Risk:** `{risk.get('risk_level')}` → recommended mode `{risk.get('recommended_mode')}`",
        f"**Workflows:** `{workflow.get('ok')}` pending=`{len(workflow.get('pending_runs', []) or [])}` failed=`{len(workflow.get('failed_runs', []) or [])}`",
        f"**Dependabot:** `{dep.get('ok')}` open=`{dep.get('open_alert_count')}` blocking=`{len(dep.get('blocking_alerts', []) or [])}`",
        "",
        "### Blockers",
        *([f"- {item}" for item in readiness.get("blockers", [])] or ["- None detected."]),
        "",
        "### Warnings",
        *([f"- {item}" for item in readiness.get("warnings", [])] or ["- None detected."]),
        "",
        "_Generated by Hermes Legion Commander governance._",
        "",
    ])


def post_pr_comment(repo: Path, *, branch_or_pr: str, body: str, gh_path: Path | None = None) -> dict[str, Any]:
    gh = find_gh(gh_path)
    if gh is None:
        raise RuntimeError("gh not found")
    owner_repo = owner_repo_from_remote(remote_url(repo))
    args = ["pr", "comment", branch_or_pr, "--body", body]
    if owner_repo:
        args.extend(["--repo", owner_repo])
    result = run_gh(gh, args, cwd=repo, timeout=90)
    return {"ok": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr, "command": result.command}


def render_dashboard(repo: Path, context_dir: Path, report: dict[str, Any]) -> Path:
    out = context_dir / "dashboard"
    out.mkdir(parents=True, exist_ok=True)
    md = render_governance_markdown(report)
    title = f"Legion Commander Dashboard — {report.get('branch') or ''}"
    body = "\n".join(f"<p>{html.escape(line)}</p>" if line and not line.startswith("#") else f"<h1>{html.escape(line.lstrip('# '))}</h1>" for line in md.splitlines())
    page = f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>{html.escape(title)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:980px;margin:2rem auto;line-height:1.45}}code{{background:#eee;padding:.1rem .25rem;border-radius:4px}}p{{margin:.35rem 0}}h1{{margin-top:1.5rem}}</style>
</head><body>{body}</body></html>\n"""
    path = out / "index.html"
    path.write_text(page, encoding="utf-8")
    return path


def list_legion_branches(repo: Path, *, remote: str = "origin") -> list[dict[str, Any]]:
    ok, out = git_ok(repo, "branch", "-a", "--format=%(refname:short) %(committerdate:iso8601) %(objectname:short)")
    rows: list[dict[str, Any]] = []
    if not ok:
        return rows
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        ref = parts[0]
        clean = ref.removeprefix(f"remotes/{remote}/")
        if "legion-commander-" not in clean:
            continue
        rows.append({"ref": ref, "branch": clean, "sha": parts[-1], "is_remote": ref.startswith("remotes/")})
    return rows


def cleanup_branches(repo: Path, *, merged: bool = True, older_than_days: int = 14, dry_run: bool = True, remote: str = "origin") -> dict[str, Any]:
    rows = list_legion_branches(repo, remote=remote)
    deleted: list[str] = []
    candidates: list[str] = []
    cutoff = dt.datetime.now(UTC) - dt.timedelta(days=older_than_days)
    for row in rows:
        branch = str(row["branch"])
        if row.get("is_remote"):
            continue
        # Approximate age from last commit date.
        ok, date_txt = git_ok(repo, "log", "-1", "--format=%cI", branch)
        if ok:
            try:
                when = dt.datetime.fromisoformat(date_txt.replace("Z", "+00:00"))
                if when > cutoff:
                    continue
            except ValueError:
                pass
        if merged:
            ok, _ = git_ok(repo, "merge-base", "--is-ancestor", branch, "HEAD")
            if not ok:
                continue
        candidates.append(branch)
        if not dry_run:
            ok, out = git_ok(repo, "branch", "-d", branch)
            if ok:
                deleted.append(branch)
    return {"dry_run": dry_run, "candidates": candidates, "deleted": deleted, "branch_count": len(rows)}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hermes-legion-commander governance", description="Analyze patch risk, PR readiness, regression memory, branches, and dashboard artifacts.")
    sub = p.add_subparsers(dest="command", required=True)
    ck = sub.add_parser("check", help="Refresh governance artifacts and print merge readiness")
    ck.add_argument("--repo", type=Path, default=Path.cwd())
    ck.add_argument("--context-dir", type=Path, help="Context directory for artifacts; defaults to <repo>/shared-context")
    ck.add_argument("--base-ref", help="Diff base, e.g. origin/dev")
    ck.add_argument("--no-github", action="store_true")
    ck.add_argument("--json", action="store_true")
    cm = sub.add_parser("comment", help="Post a Legion Commander review comment to a PR")
    cm.add_argument("--repo", type=Path, default=Path.cwd())
    cm.add_argument("--context-dir", type=Path)
    cm.add_argument("--pr", required=True, help="PR number, URL, or branch")
    cm.add_argument("--gh", type=Path)
    br = sub.add_parser("branches", help="List or cleanup Legion Commander branches")
    br.add_argument("action", choices=("list", "cleanup"))
    br.add_argument("--repo", type=Path, default=Path.cwd())
    br.add_argument("--remote", default="origin")
    br.add_argument("--older-than-days", type=int, default=14)
    br.add_argument("--delete", action="store_true", help="actually delete local merged branches; default is dry-run")
    mem = sub.add_parser("memory-add", help="Append a no-regression rule to shared memory")
    mem.add_argument("--context-dir", type=Path, required=True)
    mem.add_argument("--title", required=True)
    mem.add_argument("--rule", required=True)
    mem.add_argument("--evidence", default="")
    return p


def cli_main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "check":
            context_dir = args.context_dir or (args.repo / "shared-context")
            report = refresh_governance(context_dir, args.repo, base_ref=args.base_ref, include_github=not args.no_github)
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(render_governance_markdown(report))
                print(f"Artifacts written to: {context_dir / 'governance'}")
            return 0 if report.get("merge_readiness", {}).get("status") != "blocked" else 1
        if args.command == "comment":
            context_dir = args.context_dir or (args.repo / "shared-context")
            path = context_dir / "governance" / "governance-report.json"
            if path.is_file():
                report = json.loads(path.read_text(encoding="utf-8"))
            else:
                report = refresh_governance(context_dir, args.repo)
            result = post_pr_comment(args.repo, branch_or_pr=args.pr, body=render_pr_comment(report), gh_path=args.gh)
            print(json.dumps(result, indent=2, sort_keys=True, default=str))
            return 0 if result.get("ok") else 1
        if args.command == "branches":
            if args.action == "list":
                print(json.dumps(list_legion_branches(args.repo, remote=args.remote), indent=2, sort_keys=True))
                return 0
            result = cleanup_branches(args.repo, remote=args.remote, older_than_days=args.older_than_days, dry_run=not args.delete)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "memory-add":
            print(json.dumps(append_regression_memory(args.context_dir, title=args.title, rule=args.rule, evidence=args.evidence), indent=2, sort_keys=True))
            return 0
    except Exception as exc:
        print(f"governance: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cli_main())
