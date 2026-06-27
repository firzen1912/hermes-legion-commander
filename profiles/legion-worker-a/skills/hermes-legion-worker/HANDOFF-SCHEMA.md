# Generic worker handoff schema

```json
{
  "status": "PASS | BLOCKED | NEEDS_HUMAN | QUOTA_PAUSED",
  "dispatch_id": "",
  "run_id": "",
  "mode": "",
  "stage": "",
  "role": "",
  "profile": "",
  "requested_runtime": "",
  "executed_runtime": "",
  "model": "",
  "effort": "",
  "session_mode": "",
  "workspace": "",
  "shared_context": "",
  "objective_addressed": [],
  "acceptance_criteria_addressed": [],
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
