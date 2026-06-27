"""Pull-request workflow helpers for Legion Commander runs.

The helpers in this module keep implementation work off the protected/base branch.
They deliberately use local Git and the user's GitHub CLI OAuth session rather than
raw API tokens so Commander works with the same desktop-authenticated setup used by
Codex CLI and Claude Code users.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import subprocess
from pathlib import Path
from typing import Any

try:
    from .github_health import find_gh, owner_repo_from_remote, remote_url, run_gh
except ImportError:  # pragma: no cover - direct-file fixture imports.
    from hermes_legion_commander.github_health import find_gh, owner_repo_from_remote, remote_url, run_gh

UTC = dt.timezone.utc
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")


class PRWorkflowError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class PRWorkflowOptions:
    enabled: bool = False
    base_branch: str = "dev"
    remote: str = "origin"
    actor: str = "commander"
    mode: str = "collaborating"
    slug: str | None = None
    push: bool = False
    open_pr: bool = False
    draft: bool = False
    title: str | None = None
    body_file: Path | None = None
    gh: Path | None = None

    @property
    def active(self) -> bool:
        return self.enabled or self.push or self.open_pr


def now_stamp() -> str:
    return dt.datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def git(repo: Path, *args: str, timeout: int = 120) -> str:
    cp = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if cp.returncode != 0:
        raise PRWorkflowError((cp.stderr or cp.stdout or "git command failed").strip())
    return cp.stdout.strip()


def git_result(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def slugify(value: str, *, default: str = "work") -> str:
    value = value.strip().lower().replace("/", "-")
    value = _SAFE_SEGMENT.sub("-", value).strip("-._")
    value = re.sub(r"[-_]{2,}", "-", value)
    return value[:80] or default


def actor_from_worker(name: str, runtime: str = "", provider: str = "") -> str:
    text = " ".join([name, runtime, provider]).lower()
    if "claude" in text or "anthropic" in text:
        return "claude"
    if "codex" in text or "openai" in text or "gpt" in text:
        return "codex"
    return slugify(name, default="worker")


def branch_name(*, actor: str, mode: str, slug: str, stamp: str | None = None) -> str:
    """Return a review branch name following the requested convention.

    Examples:
    - legion-commander-codex-competitive/target-repo-v101-20260627-120000
    - legion-commander-commander-collaborating/target-repo-v101-20260627-120000
    - legion-commander-claude-alternating/target-repo-v110-20260627-120000
    """
    actor_part = slugify(actor, default="commander")
    mode_part = slugify(mode, default="collaborating")
    slug_part = slugify(slug, default="work")
    stamp_part = slugify(stamp or now_stamp(), default="run")
    return f"legion-commander-{actor_part}-{mode_part}/{slug_part}-{stamp_part}"


def ensure_clean(repo: Path) -> None:
    status = git(repo, "status", "--porcelain")
    if status:
        raise PRWorkflowError("repository must be clean before creating a Legion Commander review branch")


def fetch_base(repo: Path, *, remote: str = "origin", base_branch: str = "dev") -> dict[str, str]:
    git(repo, "fetch", remote, base_branch, timeout=300)
    remote_ref = f"{remote}/{base_branch}"
    sha = git(repo, "rev-parse", remote_ref)
    return {"remote": remote, "base_branch": base_branch, "base_ref": remote_ref, "base_sha": sha}


def ensure_branch_available(repo: Path, branch: str) -> None:
    local = git_result(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    if local.returncode == 0:
        raise PRWorkflowError(f"branch already exists locally: {branch}")
    remote = git_result(repo, "ls-remote", "--exit-code", "--heads", "origin", branch)
    if remote.returncode == 0:
        raise PRWorkflowError(f"branch already exists on origin: {branch}")


def add_worktree_from_base(repo: Path, *, worktree: Path, branch: str, base_ref: str) -> None:
    ensure_branch_available(repo, branch)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(worktree), base_ref, timeout=300)


def commit_all_if_changed(worktree: Path, *, message: str) -> dict[str, Any]:
    before = git(worktree, "rev-parse", "HEAD")
    git(worktree, "add", "-A")
    staged = git_result(worktree, "diff", "--cached", "--quiet")
    if staged.returncode == 0:
        return {"committed": False, "before": before, "after": before, "message": message}
    cp = git_result(worktree, "commit", "-m", message, timeout=300)
    if cp.returncode != 0:
        raise PRWorkflowError((cp.stderr or cp.stdout or "git commit failed").strip())
    after = git(worktree, "rev-parse", "HEAD")
    return {"committed": True, "before": before, "after": after, "message": message}


def push_branch(worktree: Path, *, branch: str, remote: str = "origin") -> str:
    git(worktree, "push", "-u", remote, branch, timeout=600)
    return branch


def _github_owner_repo(repo: Path, remote: str) -> str | None:
    try:
        return owner_repo_from_remote(remote_url(repo, remote))
    except Exception:
        return None


def build_pr_body(
    *,
    mode: str,
    branch: str,
    base_branch: str,
    run_id: str,
    summary: str,
    validation: str,
    artifacts: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    lines = [
        "## Summary",
        "",
        summary.strip() or "Legion Commander generated this branch for human review before merging to dev.",
        "",
        "## Branch and mode",
        "",
        f"- Mode: `{mode}`",
        f"- Review branch: `{branch}`",
        f"- Base branch: `{base_branch}`",
        f"- Run ID: `{run_id}`",
        "",
        "## Validation",
        "",
        validation.strip() or "- Commander completed the run and recorded validation artifacts. Review the run directory before merge.",
    ]
    if artifacts:
        lines.extend(["", "## Commander artifacts", ""])
        for path in artifacts[:20]:
            lines.append(f"- `{path}`")
    if extra:
        lines.extend(["", "## Metadata", "", "```json", json.dumps(extra, indent=2, sort_keys=True), "```"])
    lines.extend([
        "",
        "## Review note",
        "",
        "This PR is intentionally opened for repository-owner review. Do not squash/rebase if preserving detailed branch history matters; merge according to the repository policy after CI and Dependabot gates are green.",
        "",
    ])
    return "\n".join(lines)


def create_or_view_pr(
    repo: Path,
    *,
    branch: str,
    base_branch: str,
    title: str,
    body: str,
    draft: bool = False,
    gh_path: Path | None = None,
    remote: str = "origin",
) -> dict[str, Any]:
    gh = find_gh(gh_path)
    if gh is None:
        raise PRWorkflowError("GitHub CLI 'gh' was not found; cannot open a pull request")
    owner_repo = _github_owner_repo(repo, remote)
    args = ["pr", "create", "--base", base_branch, "--head", branch, "--title", title, "--body", body]
    if owner_repo:
        args.extend(["--repo", owner_repo])
    if draft:
        args.append("--draft")
    created = run_gh(gh, args, cwd=repo, timeout=120)
    if created.returncode == 0:
        url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else ""
        return {"created": True, "url": url, "stdout": created.stdout, "stderr": created.stderr}
    # Existing PRs are common after resume. Fall back to view rather than failing.
    view_args = ["pr", "view", branch, "--json", "url,number,state,title,baseRefName,headRefName"]
    if owner_repo:
        view_args.extend(["--repo", owner_repo])
    viewed = run_gh(gh, view_args, cwd=repo, timeout=60)
    if viewed.returncode == 0:
        try:
            data = json.loads(viewed.stdout)
        except json.JSONDecodeError:
            data = {"url": viewed.stdout.strip()}
        return {"created": False, "existing": True, "pr": data, "stdout": viewed.stdout, "stderr": created.stderr}
    raise PRWorkflowError((created.stderr or created.stdout or viewed.stderr or viewed.stdout or "gh pr create failed").strip())


def write_pr_artifacts(run_dir: Path, payload: dict[str, Any]) -> None:
    out = run_dir / "pull-request"
    out.mkdir(parents=True, exist_ok=True)
    (out / "pull-request.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    body = payload.get("body")
    if isinstance(body, str):
        (out / "pull-request-body.md").write_text(body, encoding="utf-8")
