# Versioned Iteration Notes

Hermes Legion Commander writes version-level engineering records into the target repository's configured iteration directory, which defaults to `docs/iterations`.

## Naming

The filename is deterministic:

```text
<version>-<core-feature-slug>.md
```

Examples:

```text
0.0.1-initial-supervised-prototype.md
54-cooperative-mapping-and-recovery.md
```

The core feature is derived from the matching roadmap heading. If a note for the same version already exists, Hermes Legion Commander preserves the manual content and updates only its marked generated block.

## Required structure

Each generated note connects evidence to delivery using these sections:

```markdown
# v<version> — <Core feature>

<concise version summary>

## Roadmap alignment
## Literature review
## Core feature
## Items
## Security and quality assurance
## Verification
## Acceptance criteria — status
## Honest scope
## Files changed
## Next
## References
```

The literature reviewer must not invent citations, metrics, filenames, test counts, or completed work. Missing evidence is recorded as incomplete or unverified.

## Lifecycle

1. The roadmap reviewer bounds the research questions.
2. The researcher gathers current evidence.
3. The literature reviewer produces a version-specific review.
4. The prototyper implements the applicable version phase.
5. The assurance role reviews security and quality.
6. The literature reviewer synthesizes the version note.
7. The supervisor writes the note in the isolated candidate worktree and updates `docs/iterations/README.md`.
8. Deterministic check status is appended by the supervisor, not invented by a model.

Iteration files remain on the candidate branch until the repository owner reviews and merges them. Hermes Legion Commander does not push or merge them automatically.


## Per-version validation artifacts

For every campaign version, Hermes Legion Commander now records a deterministic
validation result in both the external campaign state and the candidate
worktree.

Expected repository conventions:

```text
tests/test_v<version>_<feature>.py
experiments/run_v<version>_<feature>.py
results/iterations/v<version>/
├── campaign-result.json
├── campaign-result.md
└── <experiment-generated evidence>
```

Tests are mandatory when the active campaign stage changes or assures code.
Experiments are generated only when the roadmap calls for measurable
integration, simulation, benchmarking, fault injection, performance,
interoperability, or evidence behavior that can run safely on a development
host. Physical, HIL, and field evidence is never fabricated.

The supervisor appends two idempotent blocks to each iteration note:

- `HERMES-LEGION-COMMANDER VALIDATION` for focused tests, experiments, and
  gathered result files;
- `HERMES-LEGION-COMMANDER VERIFY` for repository-wide deterministic checks.

The campaign also writes `campaign-summary.json` and `campaign-summary.md` in
the external run directory.
