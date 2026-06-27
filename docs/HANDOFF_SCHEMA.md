# Worker handoff schema

A handoff should normalize to the following fields:

```json
{
  "status": "PASS | BLOCKED | NEEDS_HUMAN",
  "role": "",
  "requested_worker": "",
  "executed_worker": "",
  "runtime": "",
  "model": "",
  "effort": "",
  "session_mode": "",
  "objective_addressed": [],
  "changed_files": [],
  "reviewed_files": [],
  "commands_run": [],
  "checks": [
    {"command": "", "status": "passed | failed | skipped | deferred", "evidence": ""}
  ],
  "findings": [
    {"severity": "", "path": "", "issue": "", "evidence": "", "required_fix": ""}
  ],
  "compromises": [],
  "unresolved_risks": [],
  "next_actions": [],
  "human_approval_required": false
}
```

Do not omit blockers or convert missing evidence into success.
