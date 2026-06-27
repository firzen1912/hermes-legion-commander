# Prompt token preflight and shadow API cost

Hermes Legion Commander records prompt size before each Codex CLI or Claude Code invocation. This is designed for subscription/OAuth workflows where the native CLI may not expose billing-grade usage before execution.

## Artifacts

Each stage writes:

- `prompt-preflight.json` — full preflight estimate before the worker is invoked.
- `prompt-cost-estimate.json` — compact API-equivalent USD estimate.
- `state.json.prompt_preflight` — the same estimate embedded in stage state.

The canonical run context writes:

- `shared-context/prompt-preflight-ledger.jsonl`
- `shared-context/prompt-preflights/*.json`
- `shared-context/prompt-cost-summary.json`
- `shared-context/prompt-cost-summary.md`

Completed events also embed `runtime_metadata.prompt_preflight` and `runtime_metadata.usage_reconciliation` when the CLI emits observed token fields.

## What is estimated

The preflight includes:

- local estimated input tokens for the final assembled prompt,
- expected output token range using prior learning rows when available,
- inferred auth mode, such as ChatGPT OAuth, Claude subscription OAuth, or API key,
- API-equivalent USD estimate for OpenAI/Codex and Anthropic/Claude models,
- estimator version, prompt hash, and confidence metadata.

The estimator is offline and does not call OpenAI or Anthropic. It uses a deterministic char/byte/code-density heuristic and reconciles later with observed CLI token fields when present.

## Subscription/OAuth behavior

For Codex CLI, Commander treats no `OPENAI_API_KEY` plus native Codex login/access token as ChatGPT/Codex subscription entitlement. If `OPENAI_API_KEY` is present, the auth inference warns that API-key usage normally bills the OpenAI Platform account.

For Claude Code, Commander follows Claude Code credential precedence: cloud-provider credentials, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, then subscription OAuth login. If `ANTHROPIC_API_KEY` is present, Commander marks the stage as API-key billing rather than subscription OAuth.

## Shadow API cost

`estimated_api_cost_usd` answers: “What would this prompt approximately cost if I sent it through the API at public token pricing instead of using the native subscription/OAuth CLI?”

It is not a charge, invoice, or authoritative bill. It is a local estimate for routing and learning. For Claude subscriptions, usage credits can switch over to standard API-rate consumption after included plan limits, so the shadow estimate is also useful as a risk indicator.

## Updating prices

Vendor prices can change. The package contains a public-pricing snapshot, and you can override rates without editing code by setting:

```powershell
$env:HERMES_TOKEN_PRICE_OVERRIDES = "C:\path\to\token-prices.json"
```

Example override file:

```json
{
  "openai:gpt-5.3-codex": {"input": 1.75, "cached_input": 0.175, "output": 14.0, "source": "manual override"},
  "anthropic:claude-sonnet-4-6": {"input": 3.0, "cached_input": 0.3, "output": 15.0, "source": "manual override"}
}
```

## Manual estimator command

```powershell
hermes-legion-commander token-cost --runtime codex-cli --model gpt-5.3-codex --prompt-file .\prompt.md
hermes-legion-commander token-cost --runtime claude-code --model claude-sonnet-4-6 --prompt-file .\prompt.md
```

Use this before a manual run to compare approximate API-equivalent cost across models without making an API call.
