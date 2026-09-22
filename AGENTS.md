# revit-mcp-skillopt — AGENTS.md

Parent: [../AGENTS.md](../AGENTS.md)

## Purpose

Trains a compact, deployable "operator skill" document teaching AI clients how to use RevitMCP's tools effectively, using Microsoft's SkillOpt text-space optimizer (rollout → reflect → validation-gated edit loop). Fork of [microsoft/SkillOpt](https://github.com/microsoft/SkillOpt); all RevitMCP-specific work lives on the `revit-mcp-skillopt` branch, not `main` (which tracks upstream).

## Ownership

Own git repo (`origin` = `github.com/neelshah-starline/SkillOpt`, `upstream` = `github.com/microsoft/SkillOpt`), `.gitignore`d at the workspace root like the other four components. Python 3.10+, not part of the C#/TS build chain the other four components share — no build-order dependency on them.

## Local Contracts

- Full design: [docs/plans/SKILLOPT_INTEGRATION_PLAN.md](../docs/plans/SKILLOPT_INTEGRATION_PLAN.md) — **not yet implemented**, treat as a proposal until that doc is updated to say otherwise.
- Planned custom environment lives at `skillopt/envs/revitmcp/` (not created yet), modeled on the upstream `skillopt/envs/officeqa/` reference (closest existing analog — a tool-calling benchmark).
- Training backend: `claude_code_exec` in CLI mode (`use_sdk: false`) — uses the existing Claude Code subscription via the local `claude` binary, not a separate `ANTHROPIC_API_KEY`. See the plan doc for the tradeoffs (subscription rate limits, `bypassPermissions` safety implications) accepted for this choice.
- Rollouts execute real RevitMCP tools against a live Revit + plugin session with no per-action confirmation — the target model path must pass a safety allowlist check before any rollout batch starts (design only, not built).

## Work Guidance

Don't run a training loop against a real/production Revit model — rollouts run unattended with bypassed tool-call permissions. Follow the safety-check requirement in the plan doc once `tool_runtime.py` exists; don't bypass it even for a quick manual test.

## Verification

None yet — no environment, dataloader, or rollout code exists in this repo yet. Update this section once `skillopt/envs/revitmcp/` has something runnable (upstream's own test suite, plus whatever scoring the RevitMCP rollout adds).
