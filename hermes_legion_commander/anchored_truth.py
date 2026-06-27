"""Anchored truth and current repository state preflight for worker prompts.

This module builds a small, durable truth pack before any Codex CLI / Claude Code
prompt is sent.  The pack intentionally combines stable project authorities
(roadmaps, AGENTS.md, release docs, reference manifests) with volatile state
(Git status, latest commit, GitHub workflow/Dependabot health when available).

It is best-effort for availability, but deterministic for local anchors: missing
optional files are recorded instead of silently ignored, and all included sources
carry a SHA-256 hash so later stages can tell whether the prompt was grounded in
stale context.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

UTC = dt.timezone.utc

DEFAULT_ANCHOR_PATHS = (
    "AGENTS.md",
    "README.md",
    "CHANGELOG.md",
    "docs/README.md",
    "docs/roadmap.md",
    "request/roadmap.md",
    "docs/beta-release-roadmap.md",
    "docs/alpha-release-roadmap.md",
    "docs/1.0-release-candidate.md",
    "docs/release-governance.md",
    "docs/capability-matrix.md",
    "docs/safety-case-preliminary.md",
    "docs/deployment.md",
    "docs/deployment_readiness.md",
    "docs/hardware-bom.md",
    "configs/reference-config/reference_config.v1.0.0.json",
    "configs/reference-config/reference_config.schema.json",
    "tools/reference_config_check.py",
)

AUTHORITY_KEYWORDS = (
    "non-negotiable",
    "agent editing contract",
    "safety veto",
    "command path",
    "blocked",
    "promotion status",
    "evidence-gated",
    "fieldable",
    "bvlos",
    "unattended",
    "certification",
    "host-attestation",
    "audit-before-add",
    "roadmap status",
    "source:",
    "delivered",
    "planned",
    "warning",
    "pass",
    "fail",
)

MAX_ANCHOR_BYTES = 240_000
MAX_EXCERPT_LINES = 80
MAX_PROMPT_PACK_CHARS = 18_000


def now_iso() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_json(path: Path, payload: Any) -> None:
    _atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _git(repo: Path, *args: str, timeout: int = 30) -> tuple[int, str, str]:
    try:
        cp = subprocess.run(
            ["git", *args],
            cwd=repo,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        return cp.returncode, cp.stdout.strip(), cp.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)


def _short(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


def _repo_rel(repo: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return str(path)


def git_current_state(repo: Path) -> dict[str, Any]:
    """Return the repo state that must anchor prompt decisions."""
    rc_head, head, head_err = _git(repo, "rev-parse", "HEAD")
    _, short_head, _ = _git(repo, "rev-parse", "--short=12", "HEAD")
    _, branch, _ = _git(repo, "branch", "--show-current")
    _, status, _ = _git(repo, "status", "--short")
    _, status_porcelain, _ = _git(repo, "status", "--porcelain=v1")
    _, upstream, _ = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    ahead = behind = None
    if upstream:
        _, ahead_behind, _ = _git(repo, "rev-list", "--left-right", "--count", f"{upstream}...HEAD")
        parts = ahead_behind.split()
        if len(parts) == 2:
            try:
                behind = int(parts[0])
                ahead = int(parts[1])
            except ValueError:
                ahead = behind = None
    _, remote, _ = _git(repo, "remote", "get-url", "origin")
    _, log_line, _ = _git(repo, "log", "-1", "--format=%H%x09%ci%x09%s")
    _, tracked_files, _ = _git(repo, "ls-files")
    status_lines = [line for line in status_porcelain.splitlines() if line.strip()]
    modified = [line for line in status_lines if not line.startswith("??")]
    untracked = [line for line in status_lines if line.startswith("??")]
    return {
        "available": rc_head == 0,
        "error": head_err if rc_head != 0 else "",
        "repo": str(repo.resolve()),
        "branch": branch,
        "head_sha": head,
        "head_short": short_head,
        "last_commit": log_line,
        "remote_origin": remote,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "dirty": bool(status_lines),
        "status_short": status,
        "status_counts": {
            "total": len(status_lines),
            "modified_or_deleted_or_staged": len(modified),
            "untracked": len(untracked),
        },
        "tracked_file_count": len([line for line in tracked_files.splitlines() if line.strip()]),
    }


def _interesting_lines(text: str) -> list[str]:
    rows: list[str] = []
    in_contract = False
    for raw in text.splitlines():
        line = raw.rstrip()
        low = line.lower()
        if line.startswith("#"):
            if any(word in low for word in ("status", "boundary", "contract", "phase", "roadmap", "release", "gate", "safety", "agent")):
                rows.append(line)
            in_contract = any(word in low for word in ("agent editing contract", "non-negotiable", "boundaries", "guardrail"))
            continue
        if in_contract and line.strip().startswith(("-", "*")):
            rows.append(line)
        elif any(keyword in low for keyword in AUTHORITY_KEYWORDS):
            rows.append(line)
        if len(rows) >= MAX_EXCERPT_LINES:
            break
    return rows


def load_anchor_source(repo: Path, rel_path: str) -> dict[str, Any]:
    path = repo / rel_path
    row: dict[str, Any] = {
        "path": rel_path,
        "exists": path.is_file(),
        "role": _anchor_role(rel_path),
    }
    if not path.is_file():
        return row
    try:
        data = path.read_bytes()
    except OSError as exc:
        row.update({"exists": False, "error": str(exc)})
        return row
    row.update({
        "size_bytes": len(data),
        "sha256": sha256_bytes(data),
    })
    if len(data) > MAX_ANCHOR_BYTES:
        row["excerpt"] = f"[omitted: {len(data)} bytes exceeds anchor excerpt budget]"
        row["truncated"] = True
        return row
    text = data.decode("utf-8", errors="replace")
    lines = _interesting_lines(text)
    if not lines:
        lines = [line.rstrip() for line in text.splitlines()[:24] if line.strip()]
    row["excerpt"] = "\n".join(lines[:MAX_EXCERPT_LINES]).strip()
    row["heading_count"] = sum(1 for line in text.splitlines() if line.startswith("#"))
    row["line_count"] = text.count("\n") + (1 if text else 0)
    return row


def _anchor_role(rel_path: str) -> str:
    if rel_path == "AGENTS.md":
        return "agent_rules"
    if rel_path in {"docs/beta-release-roadmap.md", "docs/roadmap.md", "request/roadmap.md"}:
        return "roadmap_authority"
    if rel_path == "docs/alpha-release-roadmap.md":
        return "alpha_boundary"
    if "reference-config" in rel_path or rel_path == "docs/hardware-bom.md":
        return "hardware_reference_gate"
    if "release" in rel_path or "capability" in rel_path or "safety" in rel_path:
        return "release_or_safety_truth"
    return "supporting_context"


def load_anchor_sources(repo: Path, extra_paths: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    ordered: list[str] = []
    seen: set[str] = set()
    for rel in (*DEFAULT_ANCHOR_PATHS, *extra_paths):
        rel = rel.replace("\\", "/").strip("/")
        if rel and rel not in seen:
            seen.add(rel)
            ordered.append(rel)
    return [load_anchor_source(repo, rel) for rel in ordered]


def _summarize_truth(anchor_sources: list[dict[str, Any]], repo_state: dict[str, Any], github: dict[str, Any] | None) -> dict[str, Any]:
    existing = [row for row in anchor_sources if row.get("exists")]
    missing = [row.get("path") for row in anchor_sources if not row.get("exists")]
    boundary_lines: list[dict[str, str]] = []
    for row in existing:
        excerpt = str(row.get("excerpt") or "")
        for line in excerpt.splitlines():
            low = line.lower()
            if any(term in low for term in ("blocked", "do not", "must", "never", "non-negotiable", "safety", "field", "bvlos", "unattended", "certification")):
                boundary_lines.append({"path": str(row.get("path")), "line": line.strip()})
            if len(boundary_lines) >= 30:
                break
        if len(boundary_lines) >= 30:
            break
    github_summary: dict[str, Any] = {"available": False}
    if github:
        workflow = github.get("workflow_gate", {}) if isinstance(github.get("workflow_gate"), dict) else {}
        dep = github.get("dependabot_gate", {}) if isinstance(github.get("dependabot_gate"), dict) else {}
        github_summary = {
            "available": not bool(github.get("error")),
            "ok": github.get("ok"),
            "workflow_ok": workflow.get("ok"),
            "pending_workflows": len(workflow.get("pending_runs", []) or []),
            "failed_workflows": len(workflow.get("failed_runs", []) or []),
            "dependabot_ok": dep.get("ok"),
            "open_dependabot_alerts": dep.get("open_alert_count"),
            "blocking_dependabot_alerts": len(dep.get("blocking_alerts", []) or []),
            "error": github.get("error", ""),
        }
    return {
        "repo_head": repo_state.get("head_short"),
        "repo_branch": repo_state.get("branch"),
        "repo_dirty": repo_state.get("dirty"),
        "repo_status_counts": repo_state.get("status_counts", {}),
        "anchor_count": len(existing),
        "missing_anchor_paths": missing,
        "boundary_lines": boundary_lines,
        "github": github_summary,
    }


def _read_repo_workflow_names(repo: Path) -> tuple[str, ...]:
    workflows = repo / ".github" / "workflows"
    if not workflows.is_dir():
        return ()
    names: list[str] = []
    for path in sorted((*workflows.glob("*.yml"), *workflows.glob("*.yaml"))):
        name = path.stem
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[:20]:
                if line.strip().startswith("name:"):
                    candidate = line.split(":", 1)[1].strip().strip('"\'')
                    if candidate:
                        name = candidate
                    break
        except OSError:
            pass
        names.append(name)
    return tuple(dict.fromkeys(names))


def collect_github_state(repo: Path, repo_state: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Collect non-blocking GitHub status for prompt anchoring.

    This does not enforce a pass/fail gate; it is context for the model. The
    standalone ``github-health wait`` command remains the blocking gate.
    """
    try:
        from .github_health import GitHubHealthError, check_health, find_gh
    except ImportError:  # pragma: no cover - direct file loading fallback
        from hermes_legion_commander.github_health import GitHubHealthError, check_health, find_gh

    gh = find_gh()
    if not gh:
        return {"available": False, "error": "gh not found", "generated_at": now_iso()}
    require = _read_repo_workflow_names(repo)
    # Avoid marking local-only repos as hard failures: no required workflow list
    # means the report still captures any visible runs and Dependabot alerts.
    try:
        report = check_health(
            repo=repo,
            gh_path=gh,
            branch=str(repo_state.get("branch") or "") or None,
            head_sha=str(repo_state.get("head_sha") or "") or None,
            wait=False,
            timeout_seconds=60,
            interval_seconds=10,
            run_limit=20,
            require_workflows=require,
            out_dir=out_dir / "github-health",
        )
        report["available"] = True
        report["required_workflows_inferred"] = require
        return report
    except Exception as exc:  # GitHubHealthError or API/auth failures must not block prompt assembly.
        return {
            "available": False,
            "generated_at": now_iso(),
            "error": str(exc),
            "gh_path": str(gh),
            "required_workflows_inferred": require,
        }


def render_prompt_pack(report: dict[str, Any], max_chars: int = MAX_PROMPT_PACK_CHARS) -> str:
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    repo = report.get("repo_state", {}) if isinstance(report.get("repo_state"), dict) else {}
    github = summary.get("github", {}) if isinstance(summary.get("github"), dict) else {}
    lines = [
        "# Anchored Truth Preflight",
        "",
        "Read this section before implementing roadmap work. Treat it as the current local truth extracted immediately before this prompt was built.",
        "",
        "## Current repository state",
        "",
        f"- Branch: `{repo.get('branch') or '<unknown>'}`",
        f"- HEAD: `{repo.get('head_short') or repo.get('head_sha') or '<unknown>'}`",
        f"- Dirty working tree: `{'yes' if repo.get('dirty') else 'no'}`",
        f"- Status counts: `{repo.get('status_counts', {})}`",
        f"- Upstream: `{repo.get('upstream') or '<none>'}` ahead=`{repo.get('ahead')}` behind=`{repo.get('behind')}`",
        "",
        "## GitHub health snapshot",
        "",
        f"- Available: `{github.get('available')}`",
        f"- Overall OK: `{github.get('ok')}`",
        f"- Workflows OK: `{github.get('workflow_ok')}` pending=`{github.get('pending_workflows')}` failed=`{github.get('failed_workflows')}`",
        f"- Dependabot OK: `{github.get('dependabot_ok')}` open=`{github.get('open_dependabot_alerts')}` blocking=`{github.get('blocking_dependabot_alerts')}`",
    ]
    if github.get("error"):
        lines.append(f"- GitHub health error: `{_short(str(github.get('error')), 500)}`")
    lines.extend(["", "## Hard boundaries extracted from anchors", ""])
    for item in summary.get("boundary_lines", [])[:30]:
        if not isinstance(item, dict):
            continue
        lines.append(f"- `{item.get('path')}`: {item.get('line')}")
    lines.extend(["", "## Anchor sources", ""])
    for source in report.get("anchor_sources", []):
        if not isinstance(source, dict) or not source.get("exists"):
            continue
        lines.append(f"### {source.get('path')} ({source.get('role')})")
        lines.append(f"sha256: `{source.get('sha256')}`; lines: `{source.get('line_count', '<n/a>')}`")
        excerpt = str(source.get("excerpt") or "").strip()
        if excerpt:
            lines.append("")
            lines.append("```text")
            lines.append(_short(excerpt, 3500))
            lines.append("```")
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n\n[Anchored truth prompt pack truncated. See shared-context/anchored-truth/ for full artifacts.]\n"
    return text


def refresh_anchored_truth(
    context_dir: Path,
    repo: Path,
    *,
    task_prompt: str = "",
    extra_anchor_paths: tuple[str, ...] = (),
    include_github: bool = True,
) -> dict[str, Any]:
    """Refresh anchored truth artifacts and return the full report."""
    repo = repo.resolve()
    out = context_dir / "anchored-truth"
    repo_state = git_current_state(repo)
    anchor_sources = load_anchor_sources(repo, extra_anchor_paths)
    github = collect_github_state(repo, repo_state, out) if include_github else {"available": False, "error": "disabled", "generated_at": now_iso()}
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "repo": str(repo),
        "task_prompt_sha256": hashlib.sha256(task_prompt.encode("utf-8", errors="replace")).hexdigest() if task_prompt else "",
        "repo_state": repo_state,
        "anchor_sources": anchor_sources,
        "github_health": github,
    }
    report["summary"] = _summarize_truth(anchor_sources, repo_state, github)
    pack = render_prompt_pack(report)
    report["prompt_pack_sha256"] = hashlib.sha256(pack.encode("utf-8")).hexdigest()
    out.mkdir(parents=True, exist_ok=True)
    _atomic_json(out / "anchored-truth.json", report)
    _atomic_json(out / "current-repo-state.json", repo_state)
    _atomic_write(out / "anchor-sources.jsonl", "".join(json.dumps(row, sort_keys=True) + "\n" for row in anchor_sources))
    _atomic_write(out / "prompt-anchor-pack.md", pack)
    _atomic_write(context_dir / "ANCHORED_TRUTH.md", pack)
    _atomic_json(context_dir / "anchored-truth-summary.json", report["summary"])
    return report
