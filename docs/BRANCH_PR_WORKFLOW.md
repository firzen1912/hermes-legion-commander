# Branch and Pull Request Workflow

Hermes Legion Commander can keep all model-generated work off `dev` until a human reviews it. When PR workflow flags are enabled, Commander fetches the latest base branch, creates an isolated worktree branch, commits the generated changes, pushes the branch, and opens a pull request back to the base branch.

## Naming convention

Review branches use this pattern:

```text
legion-commander-<actor>-<mode>/<slug>-<timestamp>
```

Examples:

```text
legion-commander-commander-collaborating/target-repo-v101-v101-20260627-120000
legion-commander-codex-alternating/target-repo-v101-20260627-120000
legion-commander-codex-competitive/target-repo-v101-v110-gpt-v101-v110-gpt
legion-commander-claude-competitive/target-repo-v101-v110-claude-v101-v110-claude
legion-commander-commander-competitive/target-repo-v101-v110-v101-v110-converged
```

Competitive mode keeps per-worker candidate branches identifiable by actor. The final converged branch is the branch Commander pushes and opens as the review PR.

## Collaborating mode

```powershell
hermes-legion-commander collaborating `
  --config .\config\model_council.local.toml `
  --roadmap C:\path\to\repo\docs\beta-release-roadmap.md `
  campaign `
  --from-version 101 `
  --to-version 101 `
  --strategy alternating `
  --open-pr `
  --pr-base dev
```

## Alternating mode

```powershell
hermes-legion-commander alternating `
  --config .\config\model_council.local.toml `
  --repo C:\path\to\repo `
  --roadmap C:\path\to\repo\docs\beta-release-roadmap.md `
  --version 101 `
  --worker gpt `
  --handoff-to claude `
  --open-pr `
  --pr-base dev
```

## Competing mode

```powershell
hermes-legion-commander competing `
  --config .\config\checkpoint_competition.local.toml `
  --repo C:\path\to\repo `
  --roadmap C:\path\to\repo\docs\beta-release-roadmap.md `
  run `
  --from-version 101 `
  --to-version 101 `
  --open-pr `
  --pr-base dev
```

## Flags

- `--pr`: create a local review branch from latest `origin/dev` without pushing.
- `--push-branch`: commit and push the review branch.
- `--open-pr`: commit, push, and open a GitHub pull request.
- `--pr-base dev`: base branch to fetch and target in the PR.
- `--pr-remote origin`: remote used for fetch/push.
- `--pr-slug`: custom branch slug.
- `--draft-pr`: open the pull request as draft.
- `--pr-title`: custom PR title.
- `--gh`: explicit path to the GitHub CLI executable.

Commander writes PR metadata under the run directory:

```text
pull-request/pull-request.json
pull-request/pull-request-body.md
```

The generated PR body includes a concise summary, branch/mode metadata, validation notes, and links to Commander run artifacts.

## After opening the PR

Use the GitHub health gate before merge:

```powershell
hermes-legion-commander github-health wait `
  --repo C:\path\to\repo `
  --branch <review-branch> `
  --require-workflow ci `
  --require-workflow release-qualification `
  --block-severity low,medium,high,critical
```

The PR workflow creates the review request; the GitHub health gate decides whether the branch is safe to merge.
