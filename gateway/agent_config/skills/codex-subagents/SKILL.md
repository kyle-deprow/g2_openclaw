---
name: codex-subagents
description:
  Delegating implementation, review, and research work from OpenClaw to Codex subagents. Use when running the autonomous Quantipy research loop, splitting work into implementation/review/fix phases, monitoring long-running Codex tasks, or recovering stuck Codex sessions.
---

# Codex Subagent Delegation

OpenClaw is the PM and conversation surface. Codex subagents are the execution
surface for target-repo research, implementation, review, and fixes.

## Delegation Contract

Every delegated Codex task needs:

1. Target repo and working directory.
2. Agent name from `.codex/agents/`.
3. Exact task objective.
4. Files or directories to inspect first.
5. Verification commands.
6. Expected return summary.

Use Codex subagents only when a task benefits from isolated context or parallel
work. For small edits in this repo, a single Codex turn is usually enough.

## Quantipy Loop

Use these agent roles from `/home/dev/repos/quantipy/.codex/agents/`:

| Agent | Role |
|---|---|
| `researcher` | Multi-perspective ideation and proposal synthesis |
| `orchestrator` | Implementation/review/fix coordination |
| `backend-python` | Python services, tests, schemas, repositories, migrations |
| `reviewer` | Adversarial statistical review |
| `explorer` | Recent research and alternative-data search |
| `theorist` | Theory-grounded experiment framing |
| `contrarian` | Harsh critique and falsification pressure |

## Implementation/Review/Fix Pattern

1. Spawn an implementation subagent with a narrow prompt and required tests.
2. Wait for completion and inspect the returned summary, changed files, and
   verification output.
3. Spawn a reviewer subagent against the diff.
4. If findings exist, spawn a fixer subagent with only those findings.
5. Repeat review/fix until the reviewer reports no must-fix issues.
6. Run final verification from the parent context.

## Status Markers

Long-running tasks should report concise markers that the gateway can surface
back to the G2 glasses:

```text
[TASK:started] <short objective>
[TASK:progress] <current phase>
[TASK:blocked] <blocking condition and needed action>
[TASK:done] <summary and verification>
```

## Recovery

- If a subagent exits after planning only, resume with: "Skip exploration.
  Execute the implementation plan now."
- If auth fails, run `openclaw models auth login --provider openai` for
  OpenClaw-routed Codex work, or `codex login` for direct local Codex CLI work.
- If a session is silent for several minutes, inspect process state and logs
  before killing it.
- Do not retry through another runtime.
