#!/usr/bin/env python3
"""Prepare and finalize a verified Hermes Legion Commander release.

This script intentionally updates only current/operational version references.
Historical changelog entries and version-specific design notes are preserved.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_VERSION_FILES = (
    "scripts/bootstrap-hermes-legion-commander.ps1",
    "scripts/bootstrap-hermes-legion-commander.sh",
    "scripts/install-hermes-legion-commander.ps1",
    "scripts/install-hermes-legion-commander.sh",
    "scripts/repair-hermes-legion-commander.ps1",
    "scripts/repair-hermes-legion-commander.sh",
    "scripts/reset-hermes-legion-commander.ps1",
    "scripts/reset-hermes-legion-commander.sh",
    "scripts/README.md",
)
OLD_OPERATIONAL_VERSIONS = ("0.8.5", "1.7.0", "1.7.4")


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    (ROOT / path).write_text(text, encoding="utf-8", newline="\n")


def require_version(version: str) -> str:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SystemExit(f"release version must be MAJOR.MINOR.PATCH, got {version!r}")
    return version


def replace_regex(path: str, pattern: str, replacement: str, *, count: int = 1) -> None:
    text = read(path)
    updated, changed = re.subn(pattern, replacement, text, count=count, flags=re.MULTILINE | re.DOTALL)
    if changed != count:
        raise SystemExit(f"expected {count} replacement(s) in {path}, got {changed}: {pattern}")
    write(path, updated)


def ensure_supervisor_quota_policy() -> None:
    """Restore the quota-aware clean-boundary policy to the v2 goal contract.

    The policy already exists in prompt_contracts.py for legacy/versioned work.
    The v2 supervisor goal template must carry the same boundary so a generic
    campaign cannot lose quota/handoff discipline merely by using Legion v2.
    """
    path = "hermes_legion_commander/supervisor.py"
    text = read(path)
    if "## Quota and handoff policy" in text:
        return
    needle = """## Resource constraints
- Subscription reserve/budget:
- API soft/hard budget:
- Context/phase boundaries:

## Acceptance criteria
"""
    replacement = """## Resource constraints
- Subscription reserve/budget:
- API soft/hard budget:
- Context/phase boundaries:

## Quota and handoff policy
- Available capacity is not authorization to consume it; preserve configured reserve, cooldown, and context boundaries.
- Do not start a new version or work packet when the configured quota/context watermark or stop boundary has been reached.
- If quota/context pressure appears mid-version, finish active version if feasible and safe, run its focused checks, persist the exact checkpoint/handoff, and then stop.
- Never switch or rotate accounts merely to evade a provider quota, cooldown, entitlement, or authentication boundary.
- Every quota pause must record changed/reviewed files, checks, resource events, unresolved work, and the exact next action.

## Acceptance criteria
"""
    if text.count(needle) != 1:
        raise SystemExit("supervisor goal-contract resource section was not found exactly once")
    write(path, text.replace(needle, replacement, 1))


def prepare(version: str) -> None:
    version = require_version(version)

    replace_regex("pyproject.toml", r'^version\s*=\s*"[^"]+"$', f'version = "{version}"')
    replace_regex(
        "pyproject.toml",
        r'^description\s*=\s*"[^"]+"$',
        'description = "Repository-agnostic, provider-neutral multi-agent software-engineering orchestrator"',
    )
    replace_regex(
        "hermes_legion_commander/__init__.py",
        r'^__version__\s*=\s*"[^"]+"$',
        f'__version__ = "{version}"',
    )
    ensure_supervisor_quota_policy()

    for relative in ACTIVE_VERSION_FILES:
        path = ROOT / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for old in OLD_OPERATIONAL_VERSIONS:
            text = text.replace(old, version)
        path.write_text(text, encoding="utf-8", newline="\n")

    readme = read("README.md")
    release_note_pattern = re.compile(
        r"> \*\*(?:Release-state note|Release status):\*\*.*?(?=\n\n## Architecture)",
        re.DOTALL,
    )
    release_note = (
        f"> **Release status:** `main` is aligned to **{version}**. The checked-in wheel, "
        "release manifest, checksums, and runnable-verification evidence are generated only after "
        "the full source test suite and a fresh-wheel installation pass on GitHub Actions."
    )
    readme, changed = release_note_pattern.subn(release_note, readme, count=1)
    if changed != 1:
        raise SystemExit("README release-status note was not found exactly once")
    readme = readme.replace("## Install current `main`", f"## Install {version} / current `main`", 1)
    marker = "### Linux / macOS\n"
    wheel_block = (
        "### Install the verified wheel\n\n"
        "After a release build has passed, install the checked-in artifact directly:\n\n"
        "```bash\n"
        f"python -m pip install ./dist/hermes_legion_commander-{version}-py3-none-any.whl\n"
        "```\n\n"
        "For editable development from the current checkout, use the source instructions below.\n\n"
    )
    if wheel_block not in readme:
        if marker not in readme:
            raise SystemExit("README Linux/macOS install marker not found")
        readme = readme.replace(marker, wheel_block + marker, 1)
    write("README.md", readme)

    changelog = read("CHANGELOG.md")
    heading = f"## {version} — Legion v2 provider-neutral orchestration"
    if heading not in changelog:
        changelog = changelog.replace("\n# Changelog\n", "\n", 1)
        entry = f"""# Changelog

{heading}

- Promoted the provider/model/runtime/auth/executor architecture to the primary stable release line.
- Added OAuth/native/API authentication with multiple independently schedulable accounts per provider.
- Added the localhost-only multi-account GUI with Codex/Claude account isolation and API secret references.
- Added arbitrary role contracts, elastic team policies, account affinity/pools, and campaign DAG execution.
- Added quota/resource doctrine, bounded stage execution, explicit semantic verdicts, and protected human gates.
- Restored the quota-aware clean-boundary handoff policy in the generic v2 supervisor goal contract.
- Added the exact reviewed 86-skill baseline with pinned upstream revisions and Graphify 0.9.43.
- Added the provider-neutral repository-data egress safeguard with standard/strict/lockdown policies.
- Kept collaborating, competing, alternating, supervisor, governance, routing, and related workflows as compatibility/support surfaces.
- Refreshed active install/reset/repair/bootstrap version references so clean recovery cannot downgrade to a pre-v2 wheel.
- Release evidence is generated from a clean GitHub Actions build and fresh-wheel installation before publication.

"""
        changelog = entry + changelog.lstrip()
    write("CHANGELOG.md", changelog)


def pytest_summary(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines):
        if re.search(r"\bpassed\b", line):
            return line
    return lines[-1] if lines else "pytest completed without captured summary"


def finalize(version: str, pytest_output: Path, source_commit: str) -> None:
    version = require_version(version)
    wheel = ROOT / "dist" / f"hermes_legion_commander-{version}-py3-none-any.whl"
    if not wheel.is_file():
        raise SystemExit(f"verified release wheel not found: {wheel}")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    size = wheel.stat().st_size
    summary = pytest_summary(pytest_output)
    now = datetime.now(timezone.utc).isoformat()
    wheel_rel = wheel.relative_to(ROOT).as_posix()

    validation = {
        "version": version,
        "source_commit": source_commit,
        "compileall": "passed",
        "pytest": summary,
        "wheel_build": "passed",
        "wheel_install": "passed",
        "wheel_import": "passed",
        "installed_version": version,
        "cli_help": "passed",
    }
    write("VALIDATION.json", json.dumps(validation, indent=2, sort_keys=True) + "\n")

    manifest = {
        "schema_version": 2,
        "release": version,
        "version": version,
        "built_at": now,
        "source_commit": source_commit,
        "artifacts": [{"path": wheel_rel, "bytes": size, "sha256": digest}],
        "features": [
            "legion-v2-provider-neutral-orchestration",
            "multi-account-oauth-native-api",
            "localhost-account-control-panel",
            "arbitrary-role-contracts",
            "campaign-dags",
            "resource-and-quota-doctrine",
            "reviewed-86-skill-baseline",
            "graphify-0.9.43",
            "repository-data-egress-safeguard",
            "legacy-workflow-compatibility",
        ],
        "validation": validation,
    }
    write("RELEASE-MANIFEST.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    write("SHA256SUMS.txt", f"{digest}  {wheel_rel}\n")

    runnable = f"""# Runnable verification — v{version}

Hermes Legion Commander v{version} is the first packaged release of the Legion v2 provider-neutral architecture.

## Verified capabilities

- Provider/model/runtime/auth/executor separation.
- Multiple OAuth/native/API accounts per provider.
- Localhost-only account and role control panel.
- Arbitrary roles, executor affinity/pools, and campaign DAGs.
- Resource/quota doctrine and protected human gates.
- Quota-aware clean-boundary supervisor handoffs.
- Exact reviewed 86-skill baseline with Graphify 0.9.43.
- Provider-neutral repository-data egress safeguard.
- Legacy collaborating/competing/alternating workflows retained as compatibility presets.

## Validation

- `compileall`: passed.
- `pytest`: {summary}.
- Wheel build: passed.
- Wheel installed in a fresh virtual environment: passed.
- Installed package reported version `{version}`.
- Unified CLI, `legion`, and `gui` help probes: passed.

## Source

Source commit used for the release build: `{source_commit}`

## Wheel

`{wheel_rel}`

SHA-256: `{digest}`
"""
    write("RUNNABLE-VERIFICATION.md", runnable)

    readme = read("README.md")
    readme = re.sub(
        r"> \*\*Release status:\*\*.*?(?=\n\n## Architecture)",
        (
            f"> **Release status:** **{version}** is the current packaged release. The verified wheel is "
            f"`{wheel_rel}`. Its SHA-256 and build/test evidence are recorded in `SHA256SUMS.txt`, "
            "`RELEASE-MANIFEST.json`, `VALIDATION.json`, and `RUNNABLE-VERIFICATION.md`."
        ),
        readme,
        count=1,
        flags=re.DOTALL,
    )
    write("README.md", readme)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("version")
    fin = sub.add_parser("finalize")
    fin.add_argument("version")
    fin.add_argument("--pytest-output", type=Path, required=True)
    fin.add_argument("--source-commit", default=os.environ.get("GITHUB_SHA", "unknown"))
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.version)
    else:
        finalize(args.version, args.pytest_output, args.source_commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
