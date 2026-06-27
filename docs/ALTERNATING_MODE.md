# Alternating quota-failover mode

Alternating mode executes the same full council stages as council mode. On a configured worker-availability failure, the other worker receives the identical immutable context snapshot and completes the stage.

Supported failure classes are `quota`, `entitlement`, and `authentication`. Task/tool failures are not automatically handed off because they may indicate a repository or implementation defect rather than worker availability.

Only one worker writes to the candidate worktree at a time. Every handoff is persisted in `state.json` and canonical shared memory.
