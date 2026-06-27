# Prompt improvements from manual roadmap runs

This document captures prompt rules extracted from successful manual target-repository Codex/Claude runs and converts them into reusable Hermes Legion Commander
contracts.

## Extracted patterns

1. **Version-boundary discipline.** Each roadmap version is treated as an
   atomic unit: source, tests, safe evidence experiment, iteration note,
   version bump, Makefile/task target, roadmap status, focused verification,
   and one clean boundary.
2. **Quota-aware stopping.** Workers stop only after finishing the active
   version. They do not start another version when near the quota/context
   watermark.
3. **Copy-paste handoff.** The handoff line contains HEAD, package version,
   branch, tree state, next version, remaining range, and a gotcha line.
4. **Evidence honesty.** Physical/HIL/field/audit/publication gates are not
   machine-awarded. Tooling can be implemented, but those gates require signed
   host-side input.
5. **Generated-artifact discipline.** Commit current-version evidence only.
   Ignore unrelated `results/evidence`, `results/runs`, `viz/data`, provider
   state, and signer churn unless explicitly in scope.
6. **Phantom-diff guard.** If a file appears modified but its worktree hash
   equals the HEAD blob, treat it as line-ending/index noise and do not commit
   it.

## Generic dispatch additions

Every generic Hermes worker dispatch now carries these fields:

```json
{
  "commit_policy": "commander_uncommitted | commit_per_version | no_commits",
  "quota_watermark": "80%",
  "stop_policy": "finish_active_version_then_handoff",
  "generated_artifact_policy": "commit_current_version_evidence_only",
  "host_side_evidence_policy": "never_machine_award_physical_or_independent_gates"
}
```

## Version prompt contract

```text
Work version-by-version. A version is not complete until source, focused tests,
safe deterministic evidence experiment, iteration documentation, package version,
Makefile/task target, roadmap delivered/source status, and required verification
are coherent. When quota/context pressure is near the configured watermark, do
not start the next version. Finish the current version if safe, then hand off.
Do not fabricate physical/HIL/field/audit/release evidence; mark those gates
NEEDS_HUMAN or BLOCKED unless real signed evidence exists.
```

## Handoff line

```text
HANDOFF: v<START>..v<DONE> committed (HEAD <hash>, version <version>, branch <branch>, tree <clean|dirty>). Resume at v<NEXT>. Remaining: v<NEXT>..v<END>.
Gotcha: <one short gotcha, deferred host-side gate, or incomplete non-blocking note>.
```

## Commander-managed vs direct manual mode

Hermes Legion Commander’s default isolated worktree mode still leaves changes
uncommitted unless the dispatch contract explicitly sets `commit_policy` to
`commit_per_version`. The prompt text therefore says “commit when policy permits”
rather than silently committing during every run.
