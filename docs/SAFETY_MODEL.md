# Safety and approval model

The toolkit treats the target repository as an external system. Candidate changes occur only in isolated Git worktrees and dedicated branches.

Mandatory approval phases:

1. `dangerous-intent`: required before model mutation when the requested roadmap includes credentials, authorization, cryptography, MAVLink command authority, arming, failsafe, firmware, production deployment, release, or destructive operations.
2. `massive-diff`: required after candidate creation when a diff reaches the configured file or line threshold.

Approvals are phase-specific and campaign-specific. They do not authorize merge, push, deployment, release, tags, credential changes, or hardware operation.

Provider quota errors create durable `quota_paused` stage state. `resume` reuses completed stage outputs and retries only incomplete work.
