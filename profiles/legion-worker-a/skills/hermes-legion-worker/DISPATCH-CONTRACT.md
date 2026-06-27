# Generic worker dispatch contract

```json
{
  "schema_version": 1,
  "dispatch_id": "",
  "run_id": "",
  "mode": "council | competition | alternating",
  "stage": "",
  "role": "roadmap_plan_reviewer | researcher | literature_reviewer | prototyper | code_polisher | security_assurance | validation_artifacts | judge | converger | iteration_documenter | evidence_reconciler",
  "profile": "legion-worker-a | legion-worker-b",
  "native_runtime": "codex | claude",
  "permission": "read-only | workspace-write",
  "workspace": "",
  "shared_context": "",
  "prompt_file": "",
  "output_file": "",
  "candidate": "",
  "model": "",
  "effort": "low | medium | high",
  "allow_runtime_fallback": false,
  "objective": "",
  "constraints": [],
  "acceptance_criteria": [],
  "forbidden_actions": [],
  "required_checks": [],
  "required_handoff_fields": []
}
```

The profile must fail closed when mode, role, runtime, permission, workspace,
shared context, or objective is missing.
