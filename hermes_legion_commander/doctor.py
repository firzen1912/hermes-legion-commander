"""Cross-platform installation, executor, authentication, and skill diagnostics."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
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
            command, cwd=cwd, text=True, encoding="utf-8", errors="replace",
            capture_output=True, check=False, timeout=timeout,
        )
        return completed.returncode, (completed.stdout or completed.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def _tool_check(name: str, *, required: bool = True) -> Check:
    executable = shutil.which(name)
    if not executable:
        return Check(f"tool:{name}", False, "not found on PATH", required=required)
    code, output = _run([executable, "--version"])
    return Check(f"tool:{name}", code == 0, output.splitlines()[0] if output else executable, required=required)


def _toml_check(name: str, path: Path) -> tuple[Check, dict[str, Any] | None]:
    if not path.is_file():
        return Check(name, False, f"missing: {path}"), None
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        return Check(name, True, str(path.resolve())), data
    except Exception as exc:
        return Check(name, False, f"{path}: {exc}"), None


def _profile_check(profile: str) -> Check:
    executable = shutil.which("hermes")
    if not executable:
        return Check(f"profile:{profile}", False, "Hermes is not installed", required=False)
    code, output = _run([executable, "profile", "show", profile])
    return Check(f"profile:{profile}", code == 0, output or f"exit code {code}", required=False)


def _legion_checks(path: Path, *, skip_auth: bool) -> list[Check]:
    checks: list[Check] = []
    try:
        from .legion_config import load
        from .skill_profile import roots_from_registry, verify_roots
        config = load(path)
    except Exception as exc:
        return [Check("config:legion", False, f"{path}: {exc}")]
    checks.append(Check("config:legion", True, str(path.resolve())))

    # Every executor's runtime is checked independently. No provider/runtime is
    # globally mandatory; an installation is ready when its configured pool is.
    for runtime_id, runtime in config.registry.runtimes.items():
        if runtime.transport in {"cli", "local", "custom", "mcp"}:
            binary = runtime.command[0] if runtime.command else ""
            if binary and "{" not in binary:
                found = shutil.which(binary)
                checks.append(Check(f"runtime:{runtime_id}", bool(found), found or f"executable not found: {binary}"))
            else:
                checks.append(Check(f"runtime:{runtime_id}", True, "custom command template"))
        elif runtime.transport == "api":
            checks.append(Check(f"runtime:{runtime_id}", bool(runtime.endpoint), runtime.endpoint or "missing endpoint"))

    for executor_id, executor in config.registry.executors.items():
        profile = config.registry.auth_profiles[executor.auth_profile]
        runtime = config.registry.runtimes[executor.runtime]
        if skip_auth:
            checks.append(Check(f"auth:{executor_id}", True, f"skipped ({profile.kind.value})", required=False))
            continue
        if profile.kind.value in {"api_key", "oauth"} and profile.secret_ref:
            if profile.secret_ref.startswith("env:"):
                name = profile.secret_ref[4:]
                checks.append(Check(f"auth:{executor_id}", name in os.environ, f"environment reference {name} {'present' if name in os.environ else 'missing'}"))
            else:
                checks.append(Check(f"auth:{executor_id}", True, f"credential reference configured via {profile.secret_ref.split(':',1)[0]}", required=False))
        elif profile.kind.value in {"oauth", "native"}:
            if runtime.auth_status_command:
                try:
                    from .executor_runtime import run_account_action
                    result = run_account_action(
                        config.registry, executor_id, "status", timeout=30, interactive=False
                    )
                    label = profile.account_label or profile.id
                    email = f" ({profile.email})" if profile.email else ""
                    detail = result.get("output") or f"{label}{email}: exit code {result.get('returncode')}"
                    checks.append(Check(f"auth:{executor_id}", bool(result.get("ok")), detail))
                except Exception as exc:
                    checks.append(Check(f"auth:{executor_id}", False, str(exc)))
            else:
                checks.append(Check(
                    f"auth:{executor_id}", True,
                    "native/OAuth credential ownership delegated to runtime; no auth_status_command configured",
                    required=False,
                ))
        else:
            checks.append(Check(f"auth:{executor_id}", True, "no credential required", required=False))

    skill_checks = verify_roots(roots_from_registry(config.registry))
    if not skill_checks:
        checks.append(Check("skills:baseline", False, "no runtime skill roots declared"))
    for row in skill_checks:
        detail = "86-skill reviewed baseline" if row.ok else f"missing={len(row.missing)} unexpected={len(row.unexpected)} forbidden_hooks={len(row.forbidden_hooks)}"
        checks.append(Check(f"skills:{row.root}", row.ok, detail))
    return checks


def collect(
    *,
    repo_root: Path,
    target_repo: Path | None,
    council_config: Path | None,
    checkpoint_config: Path | None,
    skip_auth: bool,
    legion_config: Path | None = None,
) -> dict[str, Any]:
    checks: list[Check] = []
    try:
        version = importlib.metadata.version("hermes-legion-commander")
        checks.append(Check("package:hermes-legion-commander", True, version))
    except importlib.metadata.PackageNotFoundError:
        # Running from a checkout is valid for development.
        checks.append(Check("package:hermes-legion-commander", False, "not installed", required=False))

    checks.append(_tool_check("git"))
    checks.append(_tool_check("hermes", required=False))
    checks.append(_tool_check("uv", required=False))

    if not repo_root.is_dir():
        checks.append(Check("commander-repository", False, f"missing: {repo_root}"))
    else:
        required = [repo_root / "pyproject.toml"]
        missing = [str(path) for path in required if not path.is_file()]
        checks.append(Check("commander-repository", not missing, str(repo_root.resolve()) if not missing else "missing: " + ", ".join(missing)))

    if legion_config is not None:
        checks.extend(_legion_checks(legion_config, skip_auth=skip_auth))

    # Legacy configs remain diagnosable but no longer make Codex or Claude
    # globally required for the product.
    if council_config is not None:
        check, _ = _toml_check("config:council-legacy", council_config)
        checks.append(check)
    if checkpoint_config is not None:
        check, _ = _toml_check("config:checkpoint-legacy", checkpoint_config)
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
            # Roadmaps are useful context, not a generic product prerequisite.
            roadmaps = list((target_repo / "docs").rglob("*roadmap*.md")) if (target_repo / "docs").is_dir() else []
            checks.append(Check("target-roadmap", bool(roadmaps), ", ".join(str(p) for p in roadmaps[:8]) if roadmaps else "none found", required=False))

    for profile in ("legion-supervisor", "legion-worker-a", "legion-worker-b"):
        checks.append(_profile_check(profile))

    ok = all(check.ok or not check.required for check in checks)
    return {
        "ok": ok,
        "checks": [asdict(check) for check in checks],
        "repo_root": str(repo_root),
        "target_repo": str(target_repo) if target_repo else None,
        "legion_config": str(legion_config) if legion_config else None,
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hermes-legion-commander doctor",
        description="Verify Commander plus every runtime/auth/skill resource declared by an optional Legion config.",
    )
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    p.add_argument("--target-repo", type=Path)
    p.add_argument("--legion-config", type=Path)
    p.add_argument("--council-config", type=Path, help="legacy compatibility config")
    p.add_argument("--checkpoint-config", type=Path, help="legacy compatibility config")
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
        legion_config=args.legion_config.resolve() if args.legion_config else None,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for check in result["checks"]:
            marker = "PASS" if check["ok"] else ("WARN" if not check["required"] else "FAIL")
            print(f"[{marker}] {check['name']}: {check['detail']}")
        print("READY" if result["ok"] else "NOT READY")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
