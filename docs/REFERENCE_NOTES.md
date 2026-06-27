# Reference notes

This supervisor design was informed by the public workflow examples in:

- https://github.com/nemanjadotcom/goal-video-resources
- https://www.youtube.com/watch?v=O-PEeD7fymo

The adapted ideas are:

- Hermes acts as a harness/operator rather than silently doing the delegated worker's job.
- A task becomes a persistent goal contract with objective, constraints, acceptance criteria, forbidden actions, required checks, and handoff evidence.
- Builder self-report is provisional until independent review and validation complete.
- Reviewer failure produces a scoped fix contract with exact findings, followed by delta re-review.
- Handoffs include commands, files, checks, findings, compromises, and approval state.
- Worker unavailability is reported as a precise blocker instead of being hidden.

Hermes Legion Commander extends those ideas with repository-independent roadmap campaigns, canonical shared memory, isolated worktrees, council/competition/alternating modes, quota-aware failover, per-version tests and experiments, and explicit human approval gates.
