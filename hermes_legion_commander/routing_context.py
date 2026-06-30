"""Routing context for local Claude Code and Codex CLI workers.

The router is deliberately honest: it is an auditable, telemetry-seeded policy
that prepares Commander to orchestrate two locally authenticated CLI runtimes,
Anthropic Claude Code and OpenAI Codex CLI, with Thinker/Worker/Verifier/Judge
roles and traceable artifacts.

There is no remote model provider, API key provider registry, or HTTP endpoint
involved. Each runtime authenticates through its own CLI login, and the router
detects availability from PATH the same way ``doctor`` does.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

from .token_cost import infer_auth_mode

UTC = dt.timezone.utc

TASK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "security": ("security", "crypto", "key", "secret", "vulnerability", "dependabot", "threat", "auth", "signature", "provenance"),
    "release": ("release", "tag", "publish", "qualification", "gate", "manifest", "attestation", "go/no-go", "go no go"),
    "repo_workflow": ("github", "workflow", "ci", "pull request", "branch", "merge", "dependabot", "review"),
    "dependency": ("dependency", "dependencies", "requirement", "requirements", "lockfile", "lock file", "pin", "bump", "upgrade package", "package version", "pyproject", "poetry", "npm", "pip install"),
    "coding": ("implement", "fix", "patch", "test", "refactor", "code", "bug", "module", "function"),
    "docs": ("docs", "documentation", "readme", "roadmap", "prompt", "summary", "description"),
    "benchmark": ("benchmark", "swe", "terminalbench", "livecodebench", "gpqa", "scicode"),
    "long_context": ("long context", "large repo", "many files", "context", "summarize", "extract"),
}

DEPENDENCY_FILE_NAMES = {
    "pyproject.toml",
    "poetry.lock",
    "uv.lock",
    "pipfile",
    "pipfile.lock",
    "setup.cfg",
    "setup.py",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "cargo.toml",
    "cargo.lock",
    "go.mod",
    "go.sum",
}

ROLE_INTENT = {
    "thinker": "classify the task, plan, and choose scaffolding",
    "worker": "make the smallest correct implementation change",
    "verifier": "test, inspect, and challenge the result",
    "judge": "score readiness and decide escalation",
}

RUNTIMES: dict[str, dict[str, Any]] = {
    "claude": {
        "binary": "claude",
        "auth_args": ["auth", "status"],
        "provider": "anthropic-claude-code",
        "runtime": "claude-code",
        "roles": ["thinker", "verifier", "judge", "worker"],
        "auth_intent": "direct Claude Code OAuth or subscription session, not a provider API gateway",
    },
    "codex": {
        "binary": "codex",
        "auth_args": ["login", "status"],
        "provider": "openai-codex",
        "runtime": "codex-cli",
        "roles": ["worker", "verifier"],
        "auth_intent": "direct Codex CLI OAuth or ChatGPT session, not a provider API gateway",
    },
}


def _run_git(repo: Path, args: list[str]) -> str:
    try:
        completed = subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except Exception:
        return ""
    return completed.stdout.rstrip("\n") if completed.returncode == 0 else ""


def _git_changed_files(repo: Path, base_ref: str = "origin/dev") -> list[str]:
    base = _run_git(repo, ["merge-base", "HEAD", base_ref]) or base_ref
    out = _run_git(repo, ["diff", "--name-only", f"{base}...HEAD"])
    files = [line.strip() for line in out.splitlines() if line.strip()]
    status = _run_git(repo, ["status", "--porcelain"])
    for row in status.splitlines():
        if len(row) > 3:
            files.append(row[3:].strip())
    return sorted(set(files))


def _is_dependency_file(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    if name in DEPENDENCY_FILE_NAMES:
        return True
    if name.startswith("requirements") or name.startswith("constraints"):
        return True
    return normalized.startswith("requirements/") or "/requirements/" in normalized


def classify_task(task_prompt: str, changed_files: list[str] | None = None) -> dict[str, Any]:
    text = task_prompt.lower()
    categories: dict[str, int] = {}
    for category, words in TASK_KEYWORDS.items():
        score = sum(1 for word in words if word in text)
        if score:
            categories[category] = score
    for path in changed_files or []:
        p = path.replace("\\", "/").lower()
        if any(part in p for part in ("security", "crypto", "auth", "keyring", "dependabot")):
            categories["security"] = categories.get("security", 0) + 2
        if any(part in p for part in ("release", "evidence", "manifest", "qualification")):
            categories["release"] = categories.get("release", 0) + 2
        if p.startswith(".github/") or "workflow" in p:
            categories["repo_workflow"] = categories.get("repo_workflow", 0) + 2
        if _is_dependency_file(path):
            categories["dependency"] = categories.get("dependency", 0) + 2
        elif p.endswith((".md", ".rst", ".txt")):
            categories["docs"] = categories.get("docs", 0) + 1
    primary = max(categories, key=categories.get) if categories else "coding"
    return {"primary": primary, "scores": categories, "changed_file_count": len(changed_files or [])}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_learning_telemetry(context_dir: Path) -> dict[str, Any]:
    ledger_paths = [
        context_dir / "learning-ledger.jsonl",
        context_dir / "prompt-preflight-ledger.jsonl",
        context_dir / "governance" / "governance-report.json",
    ]
    rows: list[dict[str, Any]] = []
    for path in ledger_paths:
        if not path.is_file():
            continue
        if path.suffix == ".jsonl":
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        else:
            data = _read_json(path)
            if data:
                rows.append(data)
    by_runtime: dict[str, dict[str, Any]] = {}
    for row in rows:
        runtime = str(row.get("worker") or row.get("provider") or row.get("runtime") or row.get("selected_agent") or "unknown")
        rec = by_runtime.setdefault(runtime, {"count": 0, "success": 0, "quality": [], "tokens": []})
        rec["count"] += 1
        status = str(row.get("status") or row.get("result") or row.get("conclusion") or "").lower()
        if status in {"success", "passed", "ok", "pass"} or row.get("all_pillars_ok") is True:
            rec["success"] += 1
        for key in ("quality", "score", "merge_readiness_score"):
            try:
                rec["quality"].append(float(row[key]))
            except Exception:
                pass
        metrics = row.get("prompt_metrics") if isinstance(row.get("prompt_metrics"), dict) else {}
        try:
            rec["tokens"].append(float(metrics.get("estimated_tokens") or metrics.get("estimated_input_tokens")))
        except Exception:
            pass
    summary: dict[str, Any] = {"rows": len(rows), "runtimes": {}, "workers": {}, "providers": {}}
    for runtime, rec in by_runtime.items():
        summary_row = {
            "count": rec["count"],
            "success_rate": round(rec["success"] / rec["count"], 3) if rec["count"] else 0.0,
            "mean_quality": round(statistics.mean(rec["quality"]), 3) if rec["quality"] else None,
            "mean_estimated_tokens": round(statistics.mean(rec["tokens"]), 1) if rec["tokens"] else None,
        }
        summary["runtimes"][runtime] = summary_row
        summary["workers"][runtime] = summary_row
        summary["providers"][runtime] = summary_row
    return summary


def _runtime_status(
    binary: str,
    auth_args: list[str],
    *,
    runtime: str,
    provider: str,
    check_auth: bool,
    env: dict[str, str] | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    """Detect a CLI runtime via PATH, optionally probing OAuth login state."""
    env = env or dict(os.environ)
    path = shutil.which(binary)
    installed = path is not None
    authenticated: bool | None = None
    if installed and check_auth:
        try:
            completed = subprocess.run([path or binary, *auth_args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
            authenticated = completed.returncode == 0
        except Exception:
            authenticated = False
    available = installed and authenticated is not False
    return {
        "installed": installed,
        "tool_present": installed,
        "authenticated": authenticated,
        "path": path or "",
        "executable_path": path or "",
        "available": available,
        "auth_mode": infer_auth_mode(runtime, provider, env),
    }


def model_roster(repo: Path, context_dir: Path, *, check_auth: bool = False, env: dict[str, str] | None = None) -> dict[str, Any]:
    del repo, context_dir
    pool: dict[str, Any] = {}
    runtime_health: dict[str, Any] = {}
    for name, spec in RUNTIMES.items():
        status = _runtime_status(
            spec["binary"],
            list(spec["auth_args"]),
            runtime=spec["runtime"],
            provider=spec["provider"],
            check_auth=check_auth,
            env=env,
        )
        runtime_health[name] = status
        pool[name] = {
            "provider": spec["provider"],
            "runtime": spec["runtime"],
            "roles": list(spec["roles"]),
            "available": status["available"],
            "installed": status["installed"],
            "tool_present": status["tool_present"],
            "authenticated": status["authenticated"],
            "executable": spec["binary"],
            "executable_path": status["executable_path"],
            "auth_mode": status["auth_mode"],
            "auth_intent": spec["auth_intent"],
        }
    return {
        "pool": pool,
        "runtime_health": runtime_health,
        "worker_health": runtime_health,
        "generated_at": dt.datetime.now(UTC).isoformat(),
    }


def route_plan(
    *,
    repo: Path,
    context_dir: Path,
    task_prompt: str,
    base_ref: str = "origin/dev",
    check_auth: bool = False,
) -> dict[str, Any]:
    changed = _git_changed_files(repo, base_ref=base_ref)
    task = classify_task(task_prompt, changed)
    roster = model_roster(repo, context_dir, check_auth=check_auth)
    telemetry = read_learning_telemetry(context_dir)
    primary = task["primary"]
    risk_high = primary in {"security", "release", "repo_workflow", "dependency"} or any(
        _is_dependency_file(path) for path in changed
    ) or any(
        fnmatch.fnmatch(path, pattern)
        for path in changed
        for pattern in ("hiveas/security/**", "hiveas/release/**", "src/security/**", "src/release/**", ".github/**", "requirements/**", "configs/reference-config/**", "evidence/**", "results/evidence/**")
    )
    recommended_mode = "competing" if risk_high else ("alternating" if primary in {"docs", "repo_workflow"} else "collaborating")
    roles = [
        {"role": "thinker", "preferred": "claude", "fallback": "codex", "intent": ROLE_INTENT["thinker"]},
        {"role": "worker", "preferred": "codex", "fallback": "claude", "intent": ROLE_INTENT["worker"]},
        {"role": "verifier", "preferred": "claude", "fallback": "codex", "intent": ROLE_INTENT["verifier"]},
    ]
    if recommended_mode == "competing":
        roles.append({"role": "judge", "preferred": "claude", "fallback": "codex", "intent": ROLE_INTENT["judge"]})
    enabled_pool = [name for name, spec in roster["pool"].items() if spec.get("available")]
    checks = ["anchored-truth", "governance", "github-health"]
    if recommended_mode == "competing":
        checks.extend(["cross-validation", "final-verify"])
    plan = {
        "generated_at": dt.datetime.now(UTC).isoformat(),
        "repo": str(repo),
        "base_ref": base_ref,
        "task_classification": task,
        "changed_files": changed[:200],
        "recommended_mode": recommended_mode,
        "risk_high": risk_high,
        "roles": roles,
        "model_pool": roster["pool"],
        "worker_pool": roster["pool"],
        "enabled_pool": enabled_pool,
        "runtime_health": roster["runtime_health"],
        "worker_health": roster["worker_health"],
        "provider_health": {},
        "telemetry": telemetry,
        "required_checks": checks,
        "honesty_boundary": "This is a telemetry-seeded deterministic router over local Claude Code and Codex CLI runtimes, not a trained orchestrator model.",
        "provider_policy": "No Nous Portal, OpenAI-compatible gateway, remote model provider registry, or third-party provider API is used by this router. It only plans native Claude Code and Codex CLI orchestration.",
        "environment": {"platform": platform.platform(), "python": platform.python_version(), "cwd": os.getcwd()},
    }
    return plan


def render_markdown(plan: dict[str, Any]) -> str:
    task = plan.get("task_classification", {})
    lines = [
        "# Routing Context",
        "",
        "This is a Legion Commander routing aid for multi-model, multi-agent orchestration over local Claude Code and Codex CLI sessions.",
        "It is an auditable policy seeded by repo state, governance, and telemetry.",
        "It does not use Nous Portal, an OpenAI-compatible gateway, or any remote provider registry.",
        "",
        f"- Recommended mode: **{plan.get('recommended_mode')}**",
        f"- Primary task class: **{task.get('primary')}**",
        f"- High risk: **{plan.get('risk_high')}**",
        f"- Enabled runtimes: `{', '.join(plan.get('enabled_pool', [])) or 'none detected on PATH'}`",
        f"- Required checks: `{', '.join(plan.get('required_checks', []))}`",
        "",
        "## Role Plan",
    ]
    for role in plan.get("roles", []):
        lines.append(f"- **{role['role']}**: preferred `{role['preferred']}`, fallback `{role['fallback']}` - {role['intent']}")
    lines.extend(["", "## Local Runtime Pool"])
    for name, spec in plan.get("model_pool", {}).items():
        auth = spec.get("auth_mode") if isinstance(spec.get("auth_mode"), dict) else {}
        lines.append(
            f"- `{name}`: available={spec.get('available')} runtime={spec.get('runtime')} "
            f"provider={spec.get('provider')} auth={auth.get('mode', 'unknown')}"
        )
    lines.extend(["", "## Changed Files Considered"])
    for path in plan.get("changed_files", [])[:40]:
        lines.append(f"- `{path}`")
    if not plan.get("changed_files"):
        lines.append("- No changed files detected against base ref.")
    lines.extend([
        "",
        "## Worker Instruction",
        "Before implementing roadmap work, use this router context together with ANCHORED_TRUTH.md and GOVERNANCE.md.",
        "Use native Claude Code and Codex CLI OAuth/subscription sessions only; do not add Nous Portal, OpenAI-compatible gateway, or provider API dependencies.",
        "Escalate to competing/final verification when this file marks the task high risk or when security, release, CI, dependency, or evidence files are touched.",
    ])
    return "\n".join(lines) + "\n"


def write_artifacts(context_dir: Path, plan: dict[str, Any]) -> None:
    out = context_dir / "routing-context"
    out.mkdir(parents=True, exist_ok=True)
    (out / "routing-context-report.json").write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "model-roster.json").write_text(json.dumps(plan.get("model_pool", {}), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "worker-roster.json").write_text(json.dumps(plan.get("worker_pool", {}), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "runtime-health.json").write_text(json.dumps(plan.get("runtime_health", {}), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "routing-policy.json").write_text(json.dumps({
        "recommended_mode": plan.get("recommended_mode"),
        "roles": plan.get("roles"),
        "required_checks": plan.get("required_checks"),
        "honesty_boundary": plan.get("honesty_boundary"),
        "provider_policy": plan.get("provider_policy"),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md = render_markdown(plan)
    (out / "routing-context-pack.md").write_text(md, encoding="utf-8")
    (context_dir / "ROUTING_CONTEXT.md").write_text(md, encoding="utf-8")


def refresh_routing_context(
    context_dir: Path,
    cwd: Path,
    *,
    task_prompt: str = "",
    base_ref: str = "origin/dev",
    check_auth: bool = False,
) -> dict[str, Any]:
    plan = route_plan(repo=cwd, context_dir=context_dir, task_prompt=task_prompt, base_ref=base_ref, check_auth=check_auth)
    write_artifacts(context_dir, plan)
    return plan


def summarize_routing_policy(context_dir: Path, out: Path | None = None) -> dict[str, Any]:
    """Summarize learning ledgers next to the deterministic routing rules.

    This is not a learned model: the routing rules below are fixed constants that
    mirror ``route_plan``. Telemetry is attached for review and does not alter
    the rules.
    """
    telemetry = read_learning_telemetry(context_dir)
    policy = {
        "generated_at": dt.datetime.now(UTC).isoformat(),
        "source": str(context_dir),
        "kind": "deterministic-rules-with-telemetry-summary",
        "telemetry": telemetry,
        "routing_rules": {
            "security_release_workflow_dependency_or_evidence": "competing",
            "docs_or_low_risk": "alternating",
            "normal_code": "collaborating",
            "fallback_when_no_runtime_signal": "claude thinker + codex worker + claude verifier",
            "provider_policy": "native Claude Code and Codex CLI OAuth only",
        },
    }
    path = out or context_dir / "routing-context" / "routing-policy-summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return policy


def train_policy(context_dir: Path, out: Path | None = None) -> dict[str, Any]:
    """Backwards-compatible alias for older docs/scripts."""
    return summarize_routing_policy(context_dir, out)


def render_runtime_setup_example() -> str:
    return """# Hermes Legion Commander - Claude + Codex local runtimes
#
# The routing context orchestrates two locally authenticated CLI runtimes.
# There is no API key, base URL, or remote provider to configure: each CLI
# authenticates through its own login, and the router detects availability from
# PATH, the same checks used by `doctor`.
#
# 1) Install the two CLIs and authenticate each with OAuth, per their own docs:
#      - OpenAI Codex CLI      (binary: codex)
#      - Anthropic Claude Code (binary: claude)
#
# 2) Availability/auth can be checked with:
#      codex login status
#      claude auth status
#
# 3) Verify and plan:
#      hermes-legion-commander doctor
#      hermes-legion-commander routing plan --repo . --check-auth
#
# Runtime identifiers:
#      claude -> runtime "claude-code", provider "anthropic-claude-code"
#      codex  -> runtime "codex-cli",   provider "openai-codex"
"""


def cli_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="hermes-legion-commander routing",
        description="Plan local Claude Code and Codex CLI orchestration using Commander telemetry.",
    )
    sub = p.add_subparsers(dest="command", required=True)
    pl = sub.add_parser("plan", help="Classify a task, select roles/runtimes, and write router artifacts")
    pl.add_argument("--repo", type=Path, default=Path.cwd())
    pl.add_argument("--context-dir", type=Path)
    pl.add_argument("--task", default="")
    pl.add_argument("--task-file", type=Path)
    pl.add_argument("--base-ref", default="origin/dev")
    pl.add_argument("--check-auth", action="store_true", help="Probe `codex login status` and `claude auth status` for OAuth state")
    pl.add_argument("--json", action="store_true")
    tr = sub.add_parser("train", help="Summarize learning ledgers alongside deterministic routing rules; no model training")
    tr.add_argument("--context-dir", type=Path, required=True)
    tr.add_argument("--out", type=Path)
    ce = sub.add_parser("config-example", help="Print Claude+Codex OAuth runtime setup notes")
    ce.add_argument("--out", type=Path)
    args = p.parse_args(argv)
    try:
        if args.command == "config-example":
            text = render_runtime_setup_example()
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(text, encoding="utf-8")
                print(args.out)
            else:
                print(text)
            return 0
        if args.command == "train":
            policy = summarize_routing_policy(args.context_dir, args.out)
            print(json.dumps(policy, indent=2, sort_keys=True))
            return 0
        task = args.task
        if args.task_file:
            task = args.task_file.read_text(encoding="utf-8")
        context_dir = args.context_dir or args.repo / "shared-context"
        plan = refresh_routing_context(context_dir, args.repo, task_prompt=task, base_ref=args.base_ref, check_auth=args.check_auth)
        if args.json:
            print(json.dumps(plan, indent=2, sort_keys=True))
        else:
            print(render_markdown(plan))
            print(f"Artifacts written to: {context_dir / 'routing-context'}")
        return 0
    except Exception as exc:
        print(f"routing: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(cli_main())
