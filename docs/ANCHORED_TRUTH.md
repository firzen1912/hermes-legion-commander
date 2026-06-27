# Anchored Truth Preflight

Hermes Legion Commander now refreshes an anchored truth pack before every native
Codex CLI or Claude Code prompt. The pack is injected before the current roadmap
implementation task so workers start from the same current facts instead of
reasoning from stale conversation memory.

## What is captured

For each worker stage, Commander writes:

```text
<stage>/worker-context/ANCHORED_TRUTH.md
<stage>/worker-context/anchored-truth/anchored-truth.json
<stage>/worker-context/anchored-truth/current-repo-state.json
<stage>/worker-context/anchored-truth/anchor-sources.jsonl
<stage>/worker-context/anchored-truth/prompt-anchor-pack.md
<stage>/worker-context/anchored-truth/github-health/        # when gh is available
```

The pack includes:

- current Git branch, HEAD, upstream, dirty-state, tracked-file count, and status counts;
- hashed anchor-source excerpts from files such as `AGENTS.md`, `README.md`,
  `docs/beta-release-roadmap.md`, `docs/alpha-release-roadmap.md`, release
  governance docs, safety/capability docs, hardware BOM, and reference-config artifacts;
- extracted hard-boundary lines such as blocked promotion status, evidence gates,
  safety veto constraints, fieldability limitations, BVLOS/unattended/certification
  limits, and audit-before-add rules;
- non-blocking GitHub workflow and Dependabot health when `gh` is installed and
  authenticated.

## Relationship to `github-health`

Anchored truth is prompt context, not a release gate. It records GitHub health so
Codex and Claude know whether the current commit is already red, pending, or
blocked by Dependabot alerts.

Use the blocking gate when accepting or merging work:

```powershell
hermes-legion-commander github-health wait `
  --repo C:\path\to\target-repo `
  --branch dev `
  --require-workflow ci `
  --require-workflow release-qualification `
  --block-severity low,medium,high,critical
```

## Why this matters

Roadmap implementation prompts should never be based only on stale prompt text or
model memory. The worker receives a fresh repo-state snapshot and the latest
hashed anchor sources immediately before execution, which reduces accidental
version drift, safety-boundary drift, repeated context loading, and token waste.
