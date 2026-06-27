"""Reusable prompt contracts derived from successful manual roadmap runs.

These templates turn ad-hoc Codex/Claude handoffs into durable, provider-neutral
contracts that Hermes Legion Commander can pass to generic worker profiles or
native CLI workers.
"""
from __future__ import annotations

MANUAL_RUN_LESSONS = """# Roadmap execution lessons from manual target-repository runs

The following rules are derived from successful Codex/Claude manual runs against
a long versioned roadmap. They are intentionally repository-agnostic unless the
caller supplies a project-specific contract.

## Version-boundary discipline

- Work strictly version-by-version.
- Finish the active version before starting the next one.
- A version is not complete until source, tests, safe evidence experiment,
  iteration documentation, roadmap status, Makefile/task entry, version bump,
  and verification evidence are all coherent.
- Commit or hand off only at a clean version boundary when the dispatch contract
  permits commits. In Commander-managed isolated worktrees, leave changes
  uncommitted unless `commit_policy` explicitly says otherwise.

## Quota/context pause rule

- Do not start a new version when the native worker reports quota/context risk,
  or when the dispatch contract says the quota watermark is reached.
- If quota pressure appears mid-version, complete the active version's focused
  tests, experiment, evidence summary, and handoff before stopping when safe.
- Never stop mid-edit without reporting exact changed files and what remains.

## Evidence honesty

- Do not fabricate physical, HIL, field, independent-audit, jurisdictional,
  hardware, publication, tag, or release evidence.
- Host-side/operator-attested gates must be represented as blocked, deferred, or
  requiring human approval unless real signed inputs are present.
- A zero exit code alone is not evidence; semantic pillars, tests, artifacts,
  and known limitations must agree.

## Generated artifact policy

- Commit only generated evidence that belongs to the current version's declared
  evidence directory.
- Treat unrelated `results/evidence`, `results/runs`, `viz/data`, cache, or
  provider-session churn as generated noise unless the contract explicitly says
  to preserve it.
- If a signer regenerates keys/signatures for an older version, do not mix that
  churn into the current version commit without explicit approval.

## Phantom-diff policy

- If Git reports a file modified but `git hash-object <file>` equals
  `git rev-parse HEAD:<file>`, treat it as an index/line-ending phantom and run
  `git update-index --refresh` or document it. Do not commit a phantom change.

## Exact handoff rule

When pausing, emit a compact handoff containing:

`HANDOFF: v<START>..v<DONE> committed (HEAD <hash>, version <version>, branch <branch>, tree <clean|dirty>). Resume at v<NEXT>. Remaining: v<NEXT>..v<END>.`

Add one short gotcha line. If the requested range is complete, report final test
and evidence counts instead of a handoff.
"""


def version_execution_contract() -> str:
    return MANUAL_RUN_LESSONS


def per_version_recipe(version_token: str = "<NN>") -> str:
    return f"""# Per-version implementation recipe

For v{version_token}, complete all applicable items before moving on:

1. Read repository instructions and the exact roadmap section.
2. Implement stdlib source under the existing architecture; do not add runtime
   dependencies unless the contract explicitly allows them.
3. Add focused tests named `tests/test_v{version_token}_*.py` and keep the
   command/safety boundary guard green when present.
4. Add a deterministic safe experiment named
   `experiments/run_v{version_token}_*.py` when evidence, scenario, performance,
   migration, reliability, or qualification behavior is required.
5. Write machine-readable results, Markdown summary, signed trace, detached
   signature, and public key under the version's evidence directory.
6. Add or update `docs/iterations/{version_token}-*.md` with DONE items,
   verification, known limitations, and roadmap status.
7. Bump package version and add the version evidence target/task.
8. Flip the roadmap entry to delivered with a source link to the iteration note.
9. Run focused tests plus required boundary tests; run the experiment and verify
   all software pillars are true.
10. If commit policy permits commits, commit exactly this version's files and no
    unrelated generated churn.
"""


def quota_handoff_template() -> str:
    return """# Quota-aware handoff template

When quota/context pressure approaches the configured watermark:

- finish the active version if feasible;
- run focused tests and experiment;
- commit only if the contract permits commits;
- do not start the next version;
- emit exactly:

HANDOFF: v<START>..v<DONE> committed (HEAD <hash>, version <version>, branch <branch>, tree <clean|dirty>). Resume at v<NEXT>. Remaining: v<NEXT>..v<END>.

Then add one short gotcha/incomplete-host-gate line.
"""


def subagent_delegation_contract(cap: int = 5) -> str:
    return f"""# Subagent delegation for grunt work

You may spawn weaker, cheaper subagents to parallelize mechanical grunt work and
keep token/API usage low, under a hard limit:

- Spawn at most {cap} subagents. Never exceed {cap}, and prefer fewer.
- Use subagents only for parallelizable, low-judgement grunt work: mechanical
  edits across many files, scaffolding repetitive tests, scanning/collecting
  references, formatting, or independent checks. Keep design, security, and
  final verification on yourself.
- Assign each subagent the cheapest model that can do its task; do not put a
  strong model on grunt work. Running several cheap subagents in parallel should
  cost less and finish faster than doing it all yourself on a strong model.
- You remain accountable for everything a subagent produces; review their output.
- Report usage so the supervisor can measure and optimize. End with a block:

SUBAGENTS: <number you spawned, 0 if none>
- <subagent task and the model used>
- <subagent task and the model used>

Use 0 honestly when none were warranted. Do not fabricate subagent activity.
"""


def host_side_evidence_boundary() -> str:
    return """# Host-side evidence boundary

Physical field tests, hardware-in-loop runs, RF/network hardware qualification,
independent audit sign-off, jurisdictional authorization, publication, tagging,
and production release are never machine-awarded. Build fail-closed tooling,
verification harnesses, manifests, and approval gates, but mark those items
NEEDS_HUMAN/BLOCKED until real signed evidence exists.
"""
