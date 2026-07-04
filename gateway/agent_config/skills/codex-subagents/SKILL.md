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
2. OpenClaw stage agent name.
3. Exact task objective.
4. Files or directories to inspect first.
5. Verification commands.
6. Expected return summary.

Use Codex subagents only when a task benefits from isolated context or parallel
work. For small edits in this repo, a single Codex turn is usually enough.

## Quantipy Loop

Use the OpenClaw stage agents from the `autoresearch` skill. Existing target
repo Codex instructions can inform prompt content, but the stage names below
are authoritative:

| OpenClaw stage | Role |
|---|---|
| `context-curator` | Read-only MemPalace and `RESEARCH_LOG.md` context packet |
| `debater-microstructure` | Market mechanics theory |
| `debater-data` | Data availability, coverage, and target construction |
| `debater-skeptic` | Leakage, overfit, and cherry-picking pressure |
| `debater-theory` | Statistical and finance rationale |
| `debater-implementation` | Buildability and verification cost |
| `consensus-arbiter` | 3-of-5 majority decision and implementation brief |
| `implementer` | End-to-end implementation |
| `reviewer` | Single GPT-5.5 high methodology review |
| `fixer` | Concrete fixes only |

## Implementation/Review/Fix Pattern

1. Spawn an implementation subagent with a narrow prompt and required tests.
2. Wait for completion and inspect the returned summary, changed files, and
   verification output.
3. Spawn exactly one reviewer subagent against the diff.
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
