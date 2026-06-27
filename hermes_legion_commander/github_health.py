"""GitHub workflow and Dependabot health gates for Hermes Legion Commander.

This module intentionally shells out to the GitHub CLI instead of using API keys directly. That keeps
Commander compatible with the user's existing ``gh auth login`` OAuth/keyring session and avoids
storing GitHub tokens in Commander state.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

UTC = dt.timezone.utc
DEFAULT_BLOCK_SEVERITIES = ("low", "medium", "high", "critical")
SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


@dataclasses.dataclass(frozen=True)
class GitHubCommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class GitHubHealthError(RuntimeError):
    pass


def now_iso() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _candidate_gh_paths() -> tuple[Path, ...]:
    candidates: list[Path] = []
    found = shutil.which("gh")
    if found:
        candidates.append(Path(found))
    env_names = ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
    for name in env_names:
        root = os.environ.get(name)
        if not root:
            continue
        candidates.extend([
            Path(root) / "GitHub CLI" / "gh.exe",
            Path(root) / "Programs" / "GitHub CLI" / "gh.exe",
            Path(root) / "GitHubCLI" / "gh.exe",
        ])
    # Common fallback observed on Windows GitHub CLI installs; harmless on POSIX.
    candidates.append(Path(r"C:\Program Files\GitHub CLI\gh.exe"))
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return tuple(deduped)


def find_gh(explicit: str | Path | None = None) -> Path | None:
    """Find GitHub CLI, including the default Windows install path when not on PATH."""
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    for path in _candidate_gh_paths():
        if path.is_file():
            return path
    return None


def run_gh(gh: Path, args: list[str], *, cwd: Path | None = None, timeout: int = 60) -> GitHubCommandResult:
    command = [str(gh), *args]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=timeout,
        )
        return GitHubCommandResult(tuple(command), completed.returncode, completed.stdout, completed.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GitHubCommandResult(tuple(command), 127, "", str(exc))


def _json_or_empty(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    return json.loads(text)


def git_value(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise GitHubHealthError((completed.stderr or completed.stdout or "git command failed").strip())
    return completed.stdout.strip()


def current_branch(repo: Path) -> str:
    return git_value(repo, "branch", "--show-current")


def current_head(repo: Path) -> str:
    return git_value(repo, "rev-parse", "HEAD")


def remote_url(repo: Path, remote: str = "origin") -> str:
    return git_value(repo, "remote", "get-url", remote)


def owner_repo_from_remote(url: str) -> str | None:
    """Extract owner/repo from common GitHub HTTPS or SSH remote URLs."""
    url = url.strip()
    if not url:
        return None
    # git@github.com:owner/repo.git
    m = re.match(r"git@[^:]+:([^/]+)/(.+?)(?:\.git)?$", url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    parsed = urlparse(url)
    if parsed.netloc and "github" in parsed.netloc.lower():
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if len(parts) >= 2:
            repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
            return f"{parts[0]}/{repo}"
    # owner/repo shorthand.
    if re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", url):
        return url
    return None


def resolve_owner_repo(repo: Path, explicit: str | None = None, remote: str = "origin") -> str:
    if explicit:
        parsed = owner_repo_from_remote(explicit)
        if not parsed:
            raise GitHubHealthError(f"could not parse GitHub repository from --github-repo={explicit!r}")
        return parsed
    parsed = owner_repo_from_remote(remote_url(repo, remote))
    if not parsed:
        raise GitHubHealthError(f"could not parse GitHub repository from git remote {remote!r}")
    return parsed


def gh_auth_status(gh: Path, repo: Path) -> dict[str, Any]:
    version = run_gh(gh, ["--version"], cwd=repo)
    auth = run_gh(gh, ["auth", "status"], cwd=repo)
    return {
        "gh_path": str(gh),
        "version_ok": version.returncode == 0,
        "version": (version.stdout or version.stderr).splitlines()[0] if (version.stdout or version.stderr) else "",
        "auth_ok": auth.returncode == 0,
        "auth_status": (auth.stdout or auth.stderr).strip(),
    }


def list_workflow_runs(
    gh: Path,
    *,
    repo: Path,
    owner_repo: str,
    branch: str,
    limit: int,
) -> list[dict[str, Any]]:
    fields = "databaseId,headSha,status,conclusion,name,displayTitle,workflowName,event,createdAt,updatedAt,url"
    result = run_gh(
        gh,
        ["run", "list", "--repo", owner_repo, "--branch", branch, "--limit", str(limit), "--json", fields],
        cwd=repo,
        timeout=90,
    )
    if result.returncode != 0:
        raise GitHubHealthError((result.stderr or result.stdout or "gh run list failed").strip())
    data = _json_or_empty(result.stdout)
    return data if isinstance(data, list) else []


def filter_runs_for_head(runs: list[dict[str, Any]], head_sha: str | None) -> list[dict[str, Any]]:
    if not head_sha:
        return runs
    short = head_sha[:7]
    return [run for run in runs if str(run.get("headSha", "")).startswith(head_sha) or str(run.get("headSha", "")).startswith(short)]


def workflow_gate(runs: list[dict[str, Any]], *, require_workflows: tuple[str, ...] = ()) -> dict[str, Any]:
    by_name = {str(run.get("name") or run.get("workflowName") or ""): run for run in runs}
    missing = [name for name in require_workflows if name not in by_name]
    pending = [run for run in runs if run.get("status") != "completed"]
    failed = [run for run in runs if run.get("status") == "completed" and run.get("conclusion") != "success"]
    ok = not missing and not pending and not failed and bool(runs)
    return {
        "ok": ok,
        "run_count": len(runs),
        "missing_required_workflows": missing,
        "pending_runs": pending,
        "failed_runs": failed,
        "successful_runs": [run for run in runs if run.get("status") == "completed" and run.get("conclusion") == "success"],
    }


def dependabot_alerts(
    gh: Path,
    *,
    repo: Path,
    owner_repo: str,
    state: str = "open",
    per_page: int = 100,
) -> list[dict[str, Any]]:
    result = run_gh(
        gh,
        [
            "api",
            f"repos/{owner_repo}/dependabot/alerts",
            "-H", "Accept: application/vnd.github+json",
            "-H", "X-GitHub-Api-Version: 2022-11-28",
            "--method", "GET",
            "-f", f"state={state}",
            "-f", f"per_page={per_page}",
            "--paginate",
        ],
        cwd=repo,
        timeout=120,
    )
    if result.returncode != 0:
        raise GitHubHealthError((result.stderr or result.stdout or "gh api dependabot alerts failed").strip())
    data = _json_or_empty(result.stdout)
    return data if isinstance(data, list) else []


def _alert_severity(alert: dict[str, Any]) -> str:
    security = alert.get("security_advisory") if isinstance(alert.get("security_advisory"), dict) else {}
    severity = str(security.get("severity") or alert.get("severity") or "").lower()
    return severity if severity in SEVERITY_ORDER else "unknown"


def dependabot_gate(alerts: list[dict[str, Any]], *, block_severities: tuple[str, ...]) -> dict[str, Any]:
    block = {s.lower() for s in block_severities}
    blocking: list[dict[str, Any]] = []
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "unknown": 0}
    for alert in alerts:
        sev = _alert_severity(alert)
        counts[sev] = counts.get(sev, 0) + 1
        if sev in block:
            dep = alert.get("dependency") if isinstance(alert.get("dependency"), dict) else {}
            pkg = dep.get("package") if isinstance(dep.get("package"), dict) else {}
            blocking.append({
                "number": alert.get("number"),
                "state": alert.get("state"),
                "severity": sev,
                "package": pkg.get("name"),
                "ecosystem": pkg.get("ecosystem"),
                "manifest_path": dep.get("manifest_path"),
                "fixed_in": alert.get("fixed_in"),
                "html_url": alert.get("html_url"),
            })
    return {
        "ok": not blocking,
        "open_alert_count": len(alerts),
        "counts_by_severity": counts,
        "block_severities": sorted(block, key=lambda s: SEVERITY_ORDER.get(s, -1)),
        "blocking_alerts": blocking,
    }


def render_markdown(report: dict[str, Any]) -> str:
    verdict = "PASS" if report.get("ok") else "FAIL"
    lines = [
        f"# GitHub Health Gate — {verdict}",
        "",
        f"- Repository: `{report.get('github_repo')}`",
        f"- Branch: `{report.get('branch')}`",
        f"- Head SHA: `{str(report.get('head_sha') or '')[:12]}`",
        f"- Generated: `{report.get('generated_at')}`",
        "",
        "## Workflow runs",
        "",
    ]
    workflow = report.get("workflow_gate", {})
    lines.extend([
        f"- Status: `{'PASS' if workflow.get('ok') else 'FAIL'}`",
        f"- Runs checked: `{workflow.get('run_count', 0)}`",
        f"- Pending: `{len(workflow.get('pending_runs', []))}`",
        f"- Failed: `{len(workflow.get('failed_runs', []))}`",
        "",
    ])
    for run in workflow.get("failed_runs", [])[:20]:
        lines.append(f"- FAIL `{run.get('name')}`: status={run.get('status')} conclusion={run.get('conclusion')} url={run.get('url')}")
    for run in workflow.get("pending_runs", [])[:20]:
        lines.append(f"- PENDING `{run.get('name')}`: status={run.get('status')} url={run.get('url')}")
    if workflow.get("missing_required_workflows"):
        lines.append(f"- Missing required workflows: `{', '.join(workflow.get('missing_required_workflows', []))}`")
    lines.extend(["", "## Dependabot alerts", ""])
    dep = report.get("dependabot_gate", {})
    lines.extend([
        f"- Status: `{'PASS' if dep.get('ok') else 'FAIL'}`",
        f"- Open alerts: `{dep.get('open_alert_count', 0)}`",
        f"- Counts by severity: `{dep.get('counts_by_severity', {})}`",
        "",
    ])
    for alert in dep.get("blocking_alerts", [])[:50]:
        lines.append(
            f"- {str(alert.get('severity')).upper()} #{alert.get('number')}: "
            f"`{alert.get('package')}` ({alert.get('ecosystem')}) in `{alert.get('manifest_path')}` {alert.get('html_url') or ''}"
        )
    lines.append("")
    return "\n".join(lines)


def write_report(out_dir: Path, report: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "github-health-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "github-health-summary.md").write_text(render_markdown(report), encoding="utf-8")
    (out_dir / "workflow-runs.json").write_text(json.dumps(report.get("workflow_runs", []), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "dependabot-alerts.json").write_text(json.dumps(report.get("dependabot_alerts", []), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check_health(
    *,
    repo: Path,
    gh_path: Path | None = None,
    github_repo: str | None = None,
    remote: str = "origin",
    branch: str | None = None,
    head_sha: str | None = None,
    wait: bool = False,
    timeout_seconds: int = 1800,
    interval_seconds: int = 30,
    run_limit: int = 20,
    require_workflows: tuple[str, ...] = (),
    block_severities: tuple[str, ...] = DEFAULT_BLOCK_SEVERITIES,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    repo = repo.resolve()
    gh = find_gh(gh_path)
    if not gh:
        raise GitHubHealthError("GitHub CLI 'gh' was not found on PATH or known Windows install locations")
    owner_repo = resolve_owner_repo(repo, github_repo, remote)
    branch = branch or current_branch(repo)
    head_sha = head_sha or current_head(repo)
    started = time.monotonic()
    attempts = 0
    runs: list[dict[str, Any]] = []
    workflow = {"ok": False, "run_count": 0, "pending_runs": [], "failed_runs": [], "missing_required_workflows": []}
    while True:
        attempts += 1
        listed = list_workflow_runs(gh, repo=repo, owner_repo=owner_repo, branch=branch, limit=run_limit)
        runs = filter_runs_for_head(listed, head_sha)
        workflow = workflow_gate(runs, require_workflows=require_workflows)
        if not wait or not workflow.get("pending_runs"):
            break
        if time.monotonic() - started >= timeout_seconds:
            break
        time.sleep(max(1, interval_seconds))
    alerts = dependabot_alerts(gh, repo=repo, owner_repo=owner_repo)
    dep_gate = dependabot_gate(alerts, block_severities=block_severities)
    auth = gh_auth_status(gh, repo)
    report = {
        "ok": bool(workflow.get("ok")) and bool(dep_gate.get("ok")) and bool(auth.get("auth_ok")),
        "generated_at": now_iso(),
        "repo": str(repo),
        "github_repo": owner_repo,
        "branch": branch,
        "head_sha": head_sha,
        "wait": wait,
        "poll_attempts": attempts,
        "gh": auth,
        "workflow_runs": runs,
        "workflow_gate": workflow,
        "dependabot_alerts": alerts,
        "dependabot_gate": dep_gate,
    }
    if out_dir is not None:
        write_report(out_dir, report)
    return report


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-legion-commander github-health",
        description="Gate a patch on GitHub Actions workflow success and open Dependabot alerts using the GitHub CLI OAuth session.",
    )
    p.add_argument("command", choices=("check", "status", "wait"), nargs="?", default="check")
    p.add_argument("--repo", type=Path, default=Path.cwd(), help="Local Git repository to inspect")
    p.add_argument("--github-repo", help="GitHub repo as owner/name, or a GitHub remote URL; defaults to git remote origin")
    p.add_argument("--remote", default="origin")
    p.add_argument("--branch", help="Branch to inspect; defaults to current branch")
    p.add_argument("--head-sha", help="Commit SHA to gate; defaults to HEAD")
    p.add_argument("--gh", type=Path, help="Path to gh executable; auto-detects PATH and common Windows installs")
    p.add_argument("--wait", action="store_true", help="Poll until matching workflow runs finish or timeout")
    p.add_argument("--timeout-seconds", type=int, default=1800)
    p.add_argument("--interval-seconds", type=int, default=30)
    p.add_argument("--run-limit", type=int, default=20)
    p.add_argument("--require-workflow", action="append", default=[], help="Workflow name that must be present and successful; repeatable")
    p.add_argument("--block-severity", default=",".join(DEFAULT_BLOCK_SEVERITIES), help="Comma-separated Dependabot severities that fail the gate")
    p.add_argument("--out", type=Path, help="Directory for github-health-report.json, markdown summary, and raw API snapshots")
    p.add_argument("--json", action="store_true")
    return p


def cli_main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    wait = args.wait or args.command == "wait"
    out_dir = args.out
    if out_dir is None:
        out_dir = args.repo / "shared-context" / "github-health"
    block_severities = tuple(s.strip().lower() for s in args.block_severity.split(",") if s.strip())
    try:
        report = check_health(
            repo=args.repo,
            gh_path=args.gh,
            github_repo=args.github_repo,
            remote=args.remote,
            branch=args.branch,
            head_sha=args.head_sha,
            wait=wait,
            timeout_seconds=args.timeout_seconds,
            interval_seconds=args.interval_seconds,
            run_limit=args.run_limit,
            require_workflows=tuple(args.require_workflow),
            block_severities=block_severities,
            out_dir=out_dir,
        )
    except GitHubHealthError as exc:
        print(f"github-health: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_markdown(report))
        print(f"Artifacts written to: {out_dir}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(cli_main())
