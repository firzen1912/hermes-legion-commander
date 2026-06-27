"""Cross-platform installation and configuration diagnostics."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import sys
import subprocess
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 30) -> tuple[int, str]:
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
        text = (completed.stdout or completed.stderr or "").strip()
        return completed.returncode, text
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)




def _github_cli_path() -> str | None:
    found = shutil.which("gh")
    if found:
        return found
    if sys.platform.startswith("win"):
        candidates = []
        for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            root = __import__("os").environ.get(name)
            if root:
                candidates.extend([
                    Path(root) / "GitHub CLI" / "gh.exe",
                    Path(root) / "Programs" / "GitHub CLI" / "gh.exe",
                    Path(root) / "GitHubCLI" / "gh.exe",
                ])
        candidates.append(Path(r"C:\\Program Files\\GitHub CLI\\gh.exe"))
        for path in candidates:
            if path.is_file():
                return str(path)
    return None


def _github_cli_check() -> Check:
    executable = _github_cli_path()
    if not executable:
        return Check("tool:gh", False, "not found on PATH or common Windows install paths", required=False)
    code, output = _run([executable, "--version"])
    return Check("tool:gh", code == 0, output.splitlines()[0] if output else executable, required=False)


def _github_auth_check() -> Check:
    executable = _github_cli_path()
    if not executable:
        return Check("auth:gh", False, "GitHub CLI is not installed", required=False)
    code, output = _run([executable, "auth", "status"])
    return Check("auth:gh", code == 0, output or f"exit code {code}", required=False)

def _tool_check(name: str) -> Check:
    executable = shutil.which(name)
    if not executable:
        return Check(f"tool:{name}", False, "not found on PATH")
    code, output = _run([executable, "--version"])
    return Check(
        f"tool:{name}",
        code == 0,
        output.splitlines()[0] if output else executable,
    )


def _auth_check(tool: str, command: list[str]) -> Check:
    executable = shutil.which(tool)
    if not executable:
        return Check(f"auth:{tool}", False, "tool not installed")
    code, output = _run([executable, *command])
    return Check(f"auth:{tool}", code == 0, output or f"exit code {code}")


def _toml_check(name: str, path: Path) -> tuple[Check, dict[str, Any] | None]:
    if not path.is_file():
        return Check(name, False, f"missing: {path}"), None
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        return Check(name, True, str(path.resolve())), data
    except Exception as exc:  # diagnostic boundary
        return Check(name, False, f"{path}: {exc}"), None


def _profile_check(profile: str) -> Check:
    executable = shutil.which("hermes")
    if not executable:
        return Check(f"profile:{profile}", False, "Hermes is not installed")
    code, output = _run([executable, "profile", "show", profile])
    return Check(f"profile:{profile}", code == 0, output or f"exit code {code}")


def collect(
    *,
    repo_root: Path,
    target_repo: Path | None,
    council_config: Path | None,
    checkpoint_config: Path | None,
    skip_auth: bool,
) -> dict[str, Any]:
    checks: list[Check] = []

    try:
        version = importlib.metadata.version("hermes-legion-commander")
        checks.append(Check("package:hermes-legion-commander", True, version))
    except importlib.metadata.PackageNotFoundError:
        checks.append(Check("package:hermes-legion-commander", False, "not installed"))

    for tool in ("git", "uv", "hermes", "codex", "claude"):
        checks.append(_tool_check(tool))
    checks.append(_github_cli_check())

    if not skip_auth:
        checks.append(_auth_check("codex", ["login", "status"]))
        checks.append(_auth_check("claude", ["auth", "status"]))
        checks.append(_github_auth_check())
        hermes = shutil.which("hermes")
        if hermes:
            code, output = _run([hermes, "config", "check"])
            checks.append(Check("auth:hermes-config", code == 0, output or f"exit code {code}"))
        else:
            checks.append(Check("auth:hermes-config", False, "Hermes is not installed"))

    for profile in ("legion-supervisor", "legion-worker-a", "legion-worker-b"):
        checks.append(_profile_check(profile))

    if not repo_root.is_dir():
        checks.append(Check("commander-repository", False, f"missing: {repo_root}"))
    else:
        required = [
            repo_root / "pyproject.toml",
            repo_root / "config" / "model_council.example.toml",
            repo_root / "config" / "checkpoint_competition.example.toml",
        ]
        missing = [str(path) for path in required if not path.is_file()]
        checks.append(Check(
            "commander-repository",
            not missing,
            str(repo_root.resolve()) if not missing else "missing: " + ", ".join(missing),
        ))

    council_data = None
    checkpoint_data = None
    if council_config is not None:
        check, council_data = _toml_check("config:council", council_config)
        checks.append(check)
    if checkpoint_config is not None:
        check, checkpoint_data = _toml_check("config:checkpoint", checkpoint_config)
        checks.append(check)

    if target_repo is not None:
        if not target_repo.is_dir():
            checks.append(Check("target-repository", False, f"missing: {target_repo}"))
        else:
            git = shutil.which("git")
            if git:
                code, output = _run([git, "-C", str(target_repo), "rev-parse", "--is-inside-work-tree"])
                checks.append(Check("target-repository", code == 0 and output == "true", output or f"exit code {code}"))
            else:
                checks.append(Check("target-repository", False, "git not installed"))
            docs = target_repo / "docs"
            roadmaps = list(docs.glob("*roadmap*.md")) if docs.is_dir() else []
            checks.append(Check(
                "target-roadmap",
                bool(roadmaps),
                ", ".join(str(path) for path in roadmaps) if roadmaps else f"none under {docs}",
            ))

    # Verify config target paths are not placeholders.
    if council_data is not None:
        repo = str(council_data.get("council", {}).get("repo", ""))
        checks.append(Check(
            "config:council-repo",
            bool(repo) and "absolute/path/to" not in repo.replace("\\", "/"),
            repo or "missing",
        ))
    if checkpoint_data is not None:
        repo = str(checkpoint_data.get("competition", {}).get("repo", ""))
        checks.append(Check(
            "config:checkpoint-repo",
            bool(repo) and "absolute/path/to" not in repo.replace("\\", "/"),
            repo or "missing",
        ))

    ok = all(check.ok or not check.required for check in checks)
    return {
        "ok": ok,
        "checks": [asdict(check) for check in checks],
        "repo_root": str(repo_root),
        "target_repo": str(target_repo) if target_repo else None,
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-legion-commander doctor",
        description="Verify installation, authentication, profiles, configs, Git repository, and roadmap.",
    )
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    p.add_argument("--target-repo", type=Path)
    p.add_argument("--council-config", type=Path)
    p.add_argument("--checkpoint-config", type=Path)
    p.add_argument("--skip-auth", action="store_true")
    p.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    result = collect(
        repo_root=args.repo_root.resolve(),
        target_repo=args.target_repo.resolve() if args.target_repo else None,
        council_config=args.council_config.resolve() if args.council_config else None,
        checkpoint_config=args.checkpoint_config.resolve() if args.checkpoint_config else None,
        skip_auth=args.skip_auth,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for check in result["checks"]:
            marker = "PASS" if check["ok"] else "FAIL"
            print(f"[{marker}] {check['name']}: {check['detail']}")
        print("READY" if result["ok"] else "NOT READY")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
