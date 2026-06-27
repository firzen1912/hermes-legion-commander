# Competitive cross-validation

Competitive mode now inserts an explicit cross-validation loop between independent candidate construction and final judging.

## Flow

1. Codex and Claude build isolated candidates from the same baseline.
2. Deterministic checks run for both candidates.
3. The supervisor publishes candidate evidence into `shared-context/artifacts/candidates/`.
4. Codex reviews Claude's candidate and Claude reviews Codex's candidate using the `cross_reviewer` role.
5. Each owner receives the opponent's findings and runs `cross_polisher` in its own worktree.
6. Deterministic checks run again and candidate evidence is refreshed.
7. Dual judges score the polished candidates while considering cross-validation findings and owner responses.
8. The provisional winner seeds the convergence worktree.
9. Codex and Claude run sequential convergence passes.
10. Both workers run `final_verifier` as a read-only adversarial verification of the converged implementation.

## Artifacts

The supervisor writes:

- `cross-validation/<reviewer>-reviews-<target>.json`
- `shared-context/artifacts/cross-validation/<reviewer>-reviews-<target>.json`
- `cross-validation-summary.json`
- `cross-polish-result.json`
- `shared-context/artifacts/converged/converged.json`
- `final-verification/<agent>.json`
- `shared-context/artifacts/final-verification/<agent>.json`
- `final-verification-summary.json`

## Finding schema

Cross-validation findings are normalized with:

- `severity`: `critical`, `high`, `medium`, `low`, or `info`
- `category`: `security`, `correctness`, `regression`, `test_quality`, `performance`, `maintainability`, `documentation`, `roadmap_scope`, or `evidence_truthfulness`
- `code`: stable short snake-case identifier
- `file`: relative path when known
- `evidence`: exact reasoning, diff, file, or command evidence
- `recommended_fix`: bounded remediation
- `confidence`: 0.0 through 1.0
- `blocking`: derived by the supervisor for critical/high security, correctness, regression, or evidence-truthfulness findings

## Commands

```powershell
hermes-legion-commander checkpoint --config config/checkpoint_competition.example.toml cross-validate --from-version 51 --to-version 60
hermes-legion-commander checkpoint --config config/checkpoint_competition.example.toml cross-polish --from-version 51 --to-version 60
hermes-legion-commander checkpoint --config config/checkpoint_competition.example.toml final-verify --from-version 51 --to-version 60
```

The full `run` command executes these automatically.

## Safety properties

Cross-review and final verification are intended to be read-only. The supervisor hashes candidate/converged patches before and after these stages and raises an error if a read-only stage mutates the reviewed worktree.

The loop does not merge, push, deploy, publish, tag, alter credentials, or claim release readiness. It produces evidence for human review.
